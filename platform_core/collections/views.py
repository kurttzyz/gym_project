from uuid import uuid4
from decimal import Decimal
from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import DatabaseError, IntegrityError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from gym_core.permissions import owner_required
from platform_core.views import platform_required
from platform_core.models import GymWallet, WalletEntry, CentralCollection, PayoutDestination, GymWithdrawal
from payments.gateway_adapters.copay import CopayError, CopayConfigurationError
from . import ledger, gateway


class DestinationForm(forms.Form):
    bank = forms.CharField(max_length=100, label='Bank or payout provider')
    account_name = forms.CharField(max_length=150)
    account_number = forms.CharField(max_length=80)


class WithdrawalForm(forms.Form):
    amount = forms.DecimalField(min_value=Decimal('.01'),max_digits=12,decimal_places=2)
    destination = forms.ModelChoiceField(queryset=PayoutDestination.objects.none())
    request_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self,*args,gym,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['destination'].queryset = PayoutDestination.objects.filter(organization=gym,is_active=True,verified_at__isnull=False)
        self.fields['destination'].label_from_instance = lambda row: row.bank+' · '+row.account_number[-4:]
        self.fields['request_key'].initial = uuid4()


def environment(request):
    value = request.POST.get('environment',request.GET.get('environment','sandbox'))
    return value if value in {'sandbox','production'} else 'sandbox'


@owner_required
@never_cache
def owner_wallet(request):
    request.platform_billing_only = True
    env = environment(request)
    wallet = ledger.wallet_for(request.tenant,env)
    form = WithdrawalForm(request.POST if request.method == 'POST' else None,gym=request.tenant)
    if request.method == 'POST' and form.is_valid():
        try:
            if env == 'production' and not settings.CENTRAL_COLLECTIONS_PRODUCTION_APPROVED:
                raise ledger.LedgerError('Production withdrawals are disabled pending platform approval.')
            ledger.request_withdrawal(request.tenant,env,form.cleaned_data['destination'].pk,
                form.cleaned_data['amount'],request.user.pk,form.cleaned_data['request_key'])
        except (ledger.LedgerError, IntegrityError) as error:
            form.add_error(None,str(error) if isinstance(error,ledger.LedgerError) else 'This withdrawal request was already processed. Refresh the page.')
        else:
            messages.success(request,'Withdrawal requested. Funds are reserved; no automatic transfer was made.')
            return redirect('/manage/wallet/?environment='+env)
    return render(request,'collections/owner.html',{
        'wallet':wallet,'environment':env,'form':form,'destination_form':DestinationForm(),
        'destinations':PayoutDestination.objects.filter(organization=request.tenant),
        'withdrawals':GymWithdrawal.objects.filter(wallet=wallet)[:30],
        'page':Paginator(wallet.entries.all(),25).get_page(request.GET.get('page'))})


@owner_required
@require_POST
def add_destination(request):
    form = DestinationForm(request.POST)
    if form.is_valid():
        PayoutDestination.objects.create(organization=request.tenant,created_by=request.user,**form.cleaned_data)
        messages.success(request,'Destination submitted for platform verification. Changes require a new destination and verification.')
    else:
        messages.error(request,'Enter the bank/provider, account name and account number.')
    return redirect('owner-wallet')


@owner_required
@require_POST
def cancel_withdrawal(request,withdrawal_id):
    row = get_object_or_404(GymWithdrawal,pk=withdrawal_id,wallet__organization=request.tenant)
    try:
        ledger.transition(row.pk,'cancel',request.user.pk,'Cancelled by gym owner',gym=request.tenant)
    except ledger.LedgerError as error:
        messages.error(request,str(error))
    return redirect('/manage/wallet/?environment='+row.wallet.environment)


@platform_required
@never_cache
def dashboard(request):
    env = environment(request)
    return render(request,'collections/platform.html',{
        'recheck_page':Paginator(CentralCollection.objects.filter(environment=env).filter(Q(status__in=['submitting','pending','failed'])|Q(tenant_synced=False)).select_related('organization').order_by('-created_at'),25).get_page(request.GET.get('collection_page')),
        'ledger_page':Paginator(WalletEntry.objects.filter(wallet__environment=env).select_related('wallet__organization','actor'),25).get_page(request.GET.get('ledger_page')),
        'environment':env,'wallets':GymWallet.objects.filter(environment=env).select_related('organization'),
        'collections':CentralCollection.objects.filter(environment=env,status='paid',released_at__isnull=True).select_related('organization').order_by('created_at')[:100],
        'destinations':PayoutDestination.objects.filter(verified_at__isnull=True,is_active=True).select_related('organization')[:100],
        'page':Paginator(GymWithdrawal.objects.filter(wallet__environment=env).select_related('wallet__organization'),25).get_page(request.GET.get('page'))})


@platform_required
@require_POST
def verify_destination(request,destination_id):
    row = get_object_or_404(PayoutDestination,pk=destination_id,is_active=True)
    note = request.POST.get('note','').strip()
    if not note or len(note)>500:
        messages.error(request,'Record how account ownership was verified.')
    else:
        PayoutDestination.objects.filter(pk=row.pk,verified_at__isnull=True).update(verified_at=timezone.now(),
            verified_by_id=request.user.pk,verification_note=note)
        messages.success(request,'Payout destination verified.')
    return redirect('platform-collections')


@platform_required
@require_POST
def release(request,collection_id):
    row = get_object_or_404(CentralCollection,pk=collection_id)
    try:
        ledger.release(row.pk,request.user.pk,request.POST.get('reference',''))
    except ledger.LedgerError as error:
        messages.error(request,str(error))
    return redirect('/collections/?environment='+row.environment)


@platform_required
@require_POST
def withdrawal_action(request,withdrawal_id):
    row = get_object_or_404(GymWithdrawal,pk=withdrawal_id)
    try:
        if row.wallet.environment == 'production' and not settings.CENTRAL_COLLECTIONS_PRODUCTION_APPROVED:
            raise ledger.LedgerError('Production payouts are disabled pending approval.')
        ledger.transition(row.pk,request.POST.get('action'),request.user.pk,request.POST.get('note',''),request.POST.get('reference',''))
    except (ledger.LedgerError,IntegrityError) as error:
        messages.error(request,str(error) if isinstance(error,ledger.LedgerError) else 'Payout reference is already recorded.')
    return redirect('/collections/?environment='+row.wallet.environment)


@platform_required
@require_POST
def adjustment(request,wallet_id):
    wallet = get_object_or_404(GymWallet,pk=wallet_id)
    try:
        ledger.adjust(wallet.pk,request.POST.get('bucket'),request.POST.get('amount'),request.user.pk,
            request.POST.get('reference',''),request.POST.get('note',''))
    except ledger.LedgerError as error:
        messages.error(request,str(error))
    return redirect('/collections/?environment='+wallet.environment)


@platform_required
@require_POST
def recheck(request,collection_id):
    row = get_object_or_404(CentralCollection,pk=collection_id)
    try:
        gateway.reconcile_collection(row.pk)
    except (CopayError,DatabaseError,OSError):
        messages.error(request,'Collection could not be reconciled. Check gateway settings and gym database availability.')
    return redirect('/collections/?environment='+row.environment)


@csrf_exempt
@require_POST
@never_cache
def notify(request):
    if request.tenant is not None:
        return HttpResponse('unknown host',status=404)
    if request.content_type != 'application/json' or len(request.body)>32768:
        return HttpResponse('invalid request',status=400,content_type='text/plain')
    try:
        gateway.process_callback(request.body)
    except CopayConfigurationError:
        return HttpResponse('gateway unavailable',status=503,content_type='text/plain')
    except CopayError:
        return HttpResponse('not confirmed',status=400,content_type='text/plain')
    except Exception:
        # A central credit may already be committed. Retrying safely repairs tenant sync.
        return HttpResponse('temporarily unavailable',status=503,content_type='text/plain')
    return HttpResponse('success',content_type='text/plain')
