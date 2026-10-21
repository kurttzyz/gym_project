import calendar
from dataclasses import dataclass
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from gyms.models import Organization
from platform_core.models import SaaSPlan, GymSubscription, PlatformInvoice, PlatformPayment, SubscriptionEvent


class BillingError(Exception):
    pass


@dataclass(frozen=True)
class SubscriptionAccess:
    state: str
    allowed: bool
    modules: dict
    until: object = None


ALL_MODULES = {key: True for key in ("websites", "memberships", "attendance", "payments", "pos")}


def access_for_gym(gym, today=None):
    today = today or timezone.localdate()
    subscription = GymSubscription.objects.filter(organization=gym).first()
    if subscription is None:
        # Existing gyms remain unchanged until explicitly enrolled by Powerhouse.
        return SubscriptionAccess("Unmanaged", True, ALL_MODULES.copy())
    current = subscription.invoices.filter(status="paid", period_start__lte=today, period_end__gt=today).first()
    previous = subscription.invoices.filter(status="paid", period_end__lte=today).order_by("-period_end").first()
    modules = current.modules if current else previous.modules if previous else subscription.trial_modules
    if subscription.cancelled_at:
        return SubscriptionAccess("Cancelled", False, modules)
    if subscription.suspended_at:
        return SubscriptionAccess("Suspended", False, modules)
    if current:
        return SubscriptionAccess("Active", True, current.modules, current.period_end)
    if subscription.started_on <= today < subscription.trial_ends_on:
        return SubscriptionAccess("Trial", True, subscription.trial_modules, subscription.trial_ends_on)
    if previous and today < previous.period_end + timedelta(days=subscription.grace_days):
        return SubscriptionAccess("Grace period", True, previous.modules, previous.period_end + timedelta(days=subscription.grace_days))
    return SubscriptionAccess("Payment required", False, modules)


def end_of_period(start, period, anchor_day):
    months = {"month": 1, "year": 12}.get(period)
    if months is None:
        raise BillingError("Unsupported platform billing period.")
    try:
        year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
        return start.replace(year=year, month=month + 1, day=min(anchor_day, calendar.monthrange(year, month + 1)[1]))
    except (ValueError, OverflowError):
        raise BillingError("Billing dates exceed the supported range.") from None


def snapshot(subscription, plan):
    subscription.plan = plan
    subscription.plan_name, subscription.price, subscription.period = plan.name, plan.price, plan.period
    subscription.modules = plan.modules


def issue_next(subscription):
    existing = subscription.invoices.filter(status="issued").first()
    if existing:
        return existing
    last = subscription.invoices.filter(status="paid").order_by("-period_end").first()
    start = last.period_end if last else subscription.trial_ends_on
    return PlatformInvoice.objects.create(subscription=subscription, customer_name=subscription.organization.name,
        plan_name=subscription.plan_name, amount=subscription.price, period=subscription.period, modules=subscription.modules,
        period_start=start, period_end=end_of_period(start, subscription.period, subscription.billing_anchor_day), due_on=start)


def assign_plan(gym_id, plan_id, trial_days, grace_days, actor_id):
    if not 0 <= trial_days <= 90 or not 0 <= grace_days <= 30:
        raise BillingError("Invalid trial or grace period.")
    with transaction.atomic(using="default"):
        gym = Organization.objects.select_for_update().get(pk=gym_id)
        if GymSubscription.objects.filter(organization=gym).exists():
            raise BillingError("This gym already has a subscription. Manage its existing subscription instead.")
        plan = SaaSPlan.objects.get(pk=plan_id, is_active=True)
        today = timezone.localdate()
        trial_end = today + timedelta(days=trial_days)
        subscription = GymSubscription(organization=gym, started_on=today, trial_ends_on=trial_end,
            billing_anchor_day=trial_end.day, grace_days=grace_days, trial_modules=plan.modules)
        snapshot(subscription, plan)
        subscription.save()
        invoice = issue_next(subscription)
        SubscriptionEvent.objects.create(subscription=subscription, actor_id=actor_id, action="assigned",
            note=f"Assigned {plan.name}; {trial_days} trial days, {grace_days} grace days. Invoice {invoice.number} issued.")
        return subscription


def record_payment(invoice_id, actor_id, reference, method, note):
    reference, note = reference.strip(), note.strip()
    if not reference or len(reference) > 120 or not note or len(note) > 500 or method not in {"bank", "cash", "other", "complimentary"}:
        raise BillingError("A valid payment method, unique reference, and note are required.")
    preview = PlatformInvoice.objects.get(pk=invoice_id)
    with transaction.atomic(using="default"):
        subscription = GymSubscription.objects.select_for_update().get(pk=preview.subscription_id)
        invoice = PlatformInvoice.objects.select_for_update().get(pk=invoice_id)
        if invoice.status == "paid":
            existing = invoice.payment_record
            if existing.reference.casefold() != reference.casefold() or existing.method != method:
                raise BillingError("This invoice is already paid. No additional payment was recorded.")
            return invoice
        if invoice.status != "issued" or subscription.cancelled_at:
            raise BillingError("Only an issued invoice on an uncancelled subscription can be paid.")
        if (invoice.amount == 0) != (method == "complimentary"):
            raise BillingError("Use No charge only for zero-priced invoices; paid invoices require a payment method.")
        if PlatformPayment.objects.filter(reference__iexact=reference).exists():
            raise BillingError("This payment reference is already recorded on another invoice.")
        PlatformPayment.objects.create(invoice=invoice, amount=invoice.amount, method=method, reference=reference,
            note=note, recorded_by_id=actor_id)
        invoice.status, invoice.paid_at = "paid", timezone.now()
        invoice.save(update_fields=["status", "paid_at"])
        issue_next(subscription)
        SubscriptionEvent.objects.create(subscription=subscription, actor_id=actor_id, action="payment_recorded",
            note=f"Recorded payment for {invoice.number}; reference {reference}.")
        # A payment never silently removes an explicit suspension.
        return invoice


def change_plan(subscription_id, plan_id, actor_id, note):
    if not note.strip() or len(note.strip()) > 500:
        raise BillingError("Provide a reason for the plan change (maximum 500 characters).")
    with transaction.atomic(using="default"):
        subscription = GymSubscription.objects.select_for_update().select_related("organization").get(pk=subscription_id)
        if subscription.cancelled_at:
            raise BillingError("A cancelled subscription cannot change plans.")
        plan = SaaSPlan.objects.get(pk=plan_id, is_active=True)
        pending = subscription.invoices.filter(status="issued").first()
        if (pending and subscription.plan_id == plan.pk and pending.plan_name == plan.name
                and pending.amount == plan.price and pending.period == plan.period and pending.modules == plan.modules):
            return pending
        # Keep paid term entitlements and the original trial immutable. Replace
        # only the unpaid invoice; retained void invoices preserve the quote.
        subscription.invoices.filter(status="issued").update(status="void")
        snapshot(subscription, plan)
        subscription.save()
        invoice = issue_next(subscription)
        SubscriptionEvent.objects.create(subscription=subscription, actor_id=actor_id, action="plan_changed",
            note=f"Next unpaid term: {plan.name}. {note.strip()}")
        return invoice


def change_state(subscription_id, action, actor_id, note):
    if not note.strip() or len(note.strip()) > 500:
        raise BillingError("Provide a reason (maximum 500 characters).")
    with transaction.atomic(using="default"):
        subscription = GymSubscription.objects.select_for_update().get(pk=subscription_id)
        if subscription.cancelled_at:
            raise BillingError("This subscription is cancelled. Contact platform support for a new agreement.")
        if action == "suspend":
            if subscription.suspended_at:
                return subscription
            subscription.suspended_at = timezone.now()
        elif action == "resume":
            subscription.suspended_at = None
        elif action == "cancel":
            subscription.cancelled_at = timezone.now()
            subscription.invoices.filter(status="issued").update(status="void")
        else:
            raise BillingError("Unknown subscription action.")
        subscription.save()
        SubscriptionEvent.objects.create(subscription=subscription, actor_id=actor_id, action=action, note=note.strip())
        return subscription
