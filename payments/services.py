from uuid import uuid4
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from gym_core.members.models import MemberProfile
from gym_core.memberships.models import MemberSubscription
from gym_core.members.services import change_subscription, EnrollmentError
from .models import PaymentOrder
from .gateway_adapters.copay import CopayError, CopayGateway, config_for_gym, money


def gateway_for_order(gym, order):
    if order.central_collection:
        from platform_core.collections.gateway import config_for_central_gym, CentralGateway
        config = config_for_central_gym(gym)
    else:
        config = config_for_gym(gym) if not settings.CENTRAL_COLLECTIONS_ENABLED else None
        if config is None:
            raise CopayError("This existing payment uses a direct gym merchant. Restore direct mode to reconcile it.")
    if (config.merchant_code, config.environment) != (order.merchant_code, order.environment):
        raise CopayError("This payment belongs to a different COPAY merchant configuration. Contact the platform administrator.")
    return CentralGateway(config, gym) if order.central_collection else CopayGateway(config)


def create_order(alias, gym, subscription_id):
    config = config_for_gym(gym)
    preview = MemberSubscription.objects.get(pk=subscription_id)
    with transaction.atomic(using=alias):
        member = MemberProfile.objects.select_for_update().get(pk=preview.member_id)
        subscription = MemberSubscription.objects.select_for_update().get(pk=subscription_id)
        existing = PaymentOrder.objects.filter(subscription=subscription).first()
        if existing:
            return existing
        if not member.is_active or subscription.status != "pending" or subscription.expires_on <= timezone.localdate():
            raise CopayError("Only a pending, unexpired membership for an enabled member can be charged.")
        return PaymentOrder.objects.create(
            subscription=subscription, merchant_reference="G" + uuid4().hex,
            merchant_code=config.merchant_code, environment=config.environment,
            payment_method=config.payment_method, amount=money(subscription.price),
            central_collection=settings.CENTRAL_COLLECTIONS_ENABLED,
        )


def start_checkout(alias, gym, order_id, client_ip):
    preview = PaymentOrder.objects.select_related("subscription").get(pk=order_id)
    gateway = gateway_for_order(gym, preview)
    with transaction.atomic(using=alias):
        member = MemberProfile.objects.select_for_update().get(pk=preview.subscription.member_id)
        subscription = MemberSubscription.objects.select_for_update().get(pk=preview.subscription_id)
        order = PaymentOrder.objects.select_for_update().get(pk=order_id)
        if order.status in {"pending", "submitting"}:
            # Unknown outcomes are reconciled, never blindly resubmitted.
            return order
        if order.status != "created":
            raise CopayError("This payment cannot start another checkout.")
        if not member.is_active or subscription.status != "pending" or subscription.expires_on <= timezone.localdate():
            raise CopayError("This membership cannot accept a new checkout.")
        if order.central_collection and order.environment == "production" and not settings.CENTRAL_COLLECTIONS_PRODUCTION_APPROVED:
            raise CopayError("Central production checkout is disabled pending COPAY approval.")
        order.status = "submitting"
        order.save(update_fields=["status"])
    # Persist the attempt before network I/O. On timeout use Recheck status.
    result = gateway.submit(order, member, client_ip)
    reference = result.get("transactionId")
    if not isinstance(reference, str) or not reference or len(reference) > 100:
        raise CopayError("COPAY did not return a valid transaction reference. Recheck status.")
    checkout_url = gateway.config.validate_url(result.get("url"))
    if result.get("depositActualAmount") is not None and money(result["depositActualAmount"]) != order.amount:
        raise CopayError("COPAY returned a different payable amount. Contact the gym before paying.")
    with transaction.atomic(using=alias):
        current = PaymentOrder.objects.select_for_update().get(pk=order.pk)
        if current.provider_reference and current.provider_reference != reference:
            raise CopayError("COPAY transaction reference mismatch.")
        current.provider_reference = reference
        if current.status not in {"paid", "failed"}:
            current.status = "pending"
            current.checkout_url = checkout_url
        current.save(update_fields=["provider_reference", "checkout_url", "status"])
        return current


def reconcile(alias, gym, order_id):
    order = PaymentOrder.objects.get(pk=order_id)
    gateway = gateway_for_order(gym, order)
    if order.central_collection:
        from platform_core.collections.gateway import register_collection, reconcile_collection
        # No provider query before a durable submit attempt exists.
        if order.status == "created":
            raise CopayError("Checkout has not been submitted.")
        collection = register_collection(gym, order)
        reconcile_collection(collection.pk)
        return PaymentOrder.objects.get(pk=order.pk)
    result = gateway.requery(order)
    return apply_result(alias, order.pk, result)


def apply_result(alias, order_id, result):
    """Only call with an authenticated server-to-server requery result."""
    preview = PaymentOrder.objects.select_related("subscription").get(pk=order_id)
    with transaction.atomic(using=alias):
        MemberProfile.objects.select_for_update().get(pk=preview.subscription.member_id)
        subscription = MemberSubscription.objects.select_for_update().get(pk=preview.subscription_id)
        order = PaymentOrder.objects.select_for_update().get(pk=order_id)
        reference, status = result.get("transactionId"), result.get("statusId")
        if (result.get("merchantTransactionId") != order.merchant_reference or
                not isinstance(reference, str) or not reference or len(reference) > 100 or
                type(status) is not int or money(result.get("amount")) != order.amount or
                result.get("paymentMethodCode") != order.payment_method or
                result.get("currencyCode", order.currency) != order.currency or
                (order.provider_reference and order.provider_reference != reference)):
            raise CopayError("COPAY payment details do not match this order.")
        if order.status == "created":
            raise CopayError("Checkout was not submitted for this payment.")
        order.provider_reference = reference
        if order.status == "paid":
            return order  # Never repeat activation or downgrade a settled payment.
        order.provider_status = status
        if status == 6:
            order.net_amount = money(result.get("netAmount"))
            order.status, order.paid_at = "paid", timezone.now()
            if order.net_amount != order.amount:
                order.access_note = "Payment reported successful, but the received amount differs from the quote. Owner review required; membership not activated."
                order.save()
                return order
            if order.environment == "sandbox":
                order.access_note = "Sandbox payment recorded. No membership access was activated."
                order.save()
                return order
            try:
                change_subscription(alias, subscription.member_id, subscription.pk, "activate",
                                    f"Automatically activated after verified COPAY payment {order.merchant_reference}.", None)
            except EnrollmentError:
                order.access_note = "Payment received. Owner review required: the term is no longer eligible for automatic activation."
            else:
                order.access_note = "Membership activated after verified COPAY payment."
        elif status == 7:
            order.status = "failed"
        elif order.status != "failed":
            order.status = "pending"
        order.save()
        return order


def process_callback(alias, gym, body):
    if settings.CENTRAL_COLLECTIONS_ENABLED:
        raise CopayError("Central collections use the platform callback endpoint.")
    config = config_for_gym(gym)
    data = CopayGateway(config).verified_callback(body)
    reference = data.get("MerchantTransactionId")
    order = PaymentOrder.objects.filter(merchant_reference=reference).first()
    if not order or (order.merchant_code, order.environment) != (config.merchant_code, config.environment):
        raise CopayError("Unknown COPAY order.")
    if (data.get("CurrencyCode") != order.currency or money(data.get("Amount")) != order.amount or
            data.get("PaymentMethodCode") != order.payment_method or type(data.get("Status")) is not int):
        raise CopayError("COPAY callback details do not match this order.")
    # TransactionId is outside the signed Data in this guide. Bind it through
    # authenticated requery instead; never trust outer fields or browser returns.
    result = reconcile(alias, gym, order.pk)
    if data["Status"] in {6, 7} and result.status not in {"paid", "failed"}:
        raise CopayError("COPAY requery has not confirmed the final callback status yet.")
    if data["Status"] == 6 and result.status != "paid":
        raise CopayError("COPAY requery has not confirmed the successful callback yet.")
    return result
