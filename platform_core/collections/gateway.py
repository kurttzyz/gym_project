"""One central COPAY merchant, immutable gym/order routing, durable tenant sync."""
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from config.database_router import gym_database
from gyms.databases import ready_database
from payments.gateway_adapters.copay import (
    API_ORIGINS, PHP_METHODS, CopayConfig, CopayConfigurationError, CopayError,
    CopayGateway, json_object, load_key, money,
)
from platform_core.models import CentralCollection, GymWallet
from .ledger import wallet_for, post


def central_config():
    try:
        path = Path(settings.CENTRAL_COPAY_CONFIG_FILE)
        data = json_object(path.read_text())
        environment = data.get('environment', 'sandbox')
        origin = data['public_origin'].rstrip('/')
        url = urlsplit(origin)
        if (environment not in API_ORIGINS or data['payment_method'] not in PHP_METHODS or
                url.scheme != 'https' or url.hostname not in settings.PLATFORM_HOSTS or url.path or url.query or url.fragment or
                url.username or url.password or url.port not in (None,443) or not data['merchant_code'] or
                len(data['merchant_code']) > 100 or not data['aes_key']):
            raise ValueError
        if settings.COPAY_CONFIG_FILE:
            entries = json_object(Path(settings.COPAY_CONFIG_FILE).read_text())
            if any(row.get('merchant_code') == data['merchant_code'] and row.get('environment','sandbox') == environment for row in entries.values()):
                raise ValueError
        def key(name):
            value = Path(data[name])
            return (value if value.is_absolute() else path.parent/value).read_bytes()
        hosts = data.get('cashier_hosts', [])
        if not isinstance(hosts,list) or any(not isinstance(h,str) or any(c in h for c in '/:*\\\r\n') for h in hosts):
            raise ValueError
        legacy = settings.CENTRAL_COPAY_ALLOW_LEGACY_1024
        return CopayConfig(data['merchant_code'],data['aes_key'],load_key(key('merchant_private_key_file'),private=True,allow_legacy_1024=legacy),
            load_key(key('copay_public_key_file'),allow_legacy_1024=legacy),environment,data['payment_method'],origin,tuple(hosts))
    except (OSError,ValueError,KeyError,TypeError,AttributeError,CopayError):
        raise CopayConfigurationError('Central COPAY collections are not configured. Contact the platform administrator.') from None


def config_for_central_gym(gym, *, new=False):
    config = central_config()
    if new and config.environment == 'production' and not settings.CENTRAL_COLLECTIONS_PRODUCTION_APPROVED:
        raise CopayConfigurationError('Central production collections are disabled pending COPAY approval. Use sandbox for testing.')
    domain = gym.domains.filter(is_active=True,verified_at__isnull=False).order_by('hostname').first()
    if not domain:
        raise CopayConfigurationError('A verified gym domain is required for member checkout.')
    return replace(config, public_origin='https://'+domain.hostname)


def register_collection(gym, order):
    collection, created = CentralCollection.objects.get_or_create(organization=gym,tenant_order_id=order.pk,defaults={
        'merchant_reference':order.merchant_reference,'merchant_code':order.merchant_code,'environment':order.environment,
        'payment_method':order.payment_method,'amount':order.amount,'currency':order.currency})
    if (collection.merchant_reference,collection.merchant_code,collection.environment,collection.amount,collection.payment_method) != (
            order.merchant_reference,order.merchant_code,order.environment,order.amount,order.payment_method):
        raise CopayError('Central collection mapping does not match the gym order.')
    return collection


class CentralGateway(CopayGateway):
    def __init__(self,config,gym):
        super().__init__(config)
        self.gym = gym

    def submit(self,order,member,client_ip):
        register_collection(self.gym,order)
        return self.request('/api/Deposit/SubmitDeposit',{
            'MerchantTransactionId':order.merchant_reference,'MerchantClientId':member.pk.hex,
            'CurrencyCode':order.currency,'Amount':order.amount,'PaymentMethodCode':order.payment_method,
            'NotifyUrl':central_config().public_origin+'/collections/copay/notify/',
            'ReturnUrl':self.config.public_origin+f'/payments/{order.pk}/return/','IPAddress':client_ip})


def apply_result(collection_id,result):
    preview = CentralCollection.objects.get(pk=collection_id)
    wallet = wallet_for(preview.organization,preview.environment)
    with transaction.atomic(using='default'):
        wallet = GymWallet.objects.select_for_update().get(pk=wallet.pk)
        row = CentralCollection.objects.select_for_update().get(pk=collection_id)
        reference,status = result.get('transactionId'),result.get('statusId')
        if (result.get('merchantTransactionId') != row.merchant_reference or type(status) is not int or
                not isinstance(reference,str) or not reference or len(reference)>100 or
                money(result.get('amount')) != row.amount or result.get('paymentMethodCode') != row.payment_method or
                result.get('currencyCode',row.currency) != row.currency or row.provider_reference and row.provider_reference != reference):
            raise CopayError('COPAY result does not match the central collection.')
        if row.status == 'paid':
            return row
        row.provider_reference = reference
        row.authenticated_result = {key:str(value) if key in {'amount','netAmount'} else value for key,value in result.items()
            if key in {'transactionId','merchantTransactionId','statusId','amount','netAmount','paymentMethodCode','currencyCode'}}
        row.tenant_synced = False
        if status == 6:
            row.net_amount = money(result.get('netAmount'))
            if row.net_amount > row.amount:
                raise CopayError('Received amount exceeds the quoted amount; platform review required.')
            row.status = 'paid'
            post(wallet,'collection:'+str(row.pk),'collection','Verified COPAY collection '+row.merchant_reference,pending=row.net_amount)
        elif status == 7:
            row.status = 'failed'
        elif row.status != 'failed':
            row.status = 'pending'
        row.save()
        return row


def sync_tenant(collection_id):
    from payments.models import PaymentOrder
    from payments.services import apply_result as tenant_apply
    row = CentralCollection.objects.select_related('organization').get(pk=collection_id)
    if not row.authenticated_result:
        return
    alias = ready_database(row.organization)
    with gym_database(alias):
        order = PaymentOrder.objects.get(pk=row.tenant_order_id,central_collection=True,
            merchant_reference=row.merchant_reference,merchant_code=row.merchant_code,environment=row.environment)
        tenant_apply(alias,order.pk,row.authenticated_result)
    CentralCollection.objects.filter(pk=row.pk,authenticated_result=row.authenticated_result).update(tenant_synced=True)


def reconcile_collection(collection_id):
    row = CentralCollection.objects.get(pk=collection_id)
    config = central_config()
    if (config.merchant_code,config.environment,config.payment_method) != (row.merchant_code,row.environment,row.payment_method):
        raise CopayError('Restore the original central merchant configuration to reconcile this collection.')
    result = CopayGateway(config).requery(row)
    row = apply_result(row.pk,result)
    sync_tenant(row.pk)
    return row


def process_callback(body):
    config = central_config()
    data = CopayGateway(config).verified_callback(body)
    row = CentralCollection.objects.filter(merchant_reference=data.get('MerchantTransactionId'),
        merchant_code=config.merchant_code,environment=config.environment).first()
    if not row or (data.get('CurrencyCode') != row.currency or money(data.get('Amount')) != row.amount or
                   data.get('PaymentMethodCode') != row.payment_method or type(data.get('Status')) is not int):
        raise CopayError('Callback does not match a central collection.')
    row = reconcile_collection(row.pk)
    if data['Status'] in {6,7} and row.status not in {'paid','failed'} or data['Status'] == 6 and row.status != 'paid':
        raise CopayError('COPAY has not confirmed this final callback.')
    return row


@shared_task(ignore_result=True)
def retry_tenant_sync():
    import logging
    for pk in CentralCollection.objects.filter(tenant_synced=False).exclude(authenticated_result={}).values_list('pk',flat=True)[:50]:
        try:
            sync_tenant(pk)
        except Exception:
            # No member/provider payloads or credentials are logged.
            logging.getLogger(__name__).warning('Central collection tenant sync pending: %s',pk)
