"""Invoice automation, never unattended collection or a recurring-debit mandate."""

from datetime import timedelta
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone
from config.database_router import gym_database
from gyms.models import Membership
from gym_core.members.models import MemberProfile
from gym_core.memberships.models import MembershipPlan, MemberSubscription
from gym_core.members.services import EnrollmentError, end_of_term, record_event
from gym_core.self_service.models import RenewalRequest
from gym_core.notifications.services import queue_email
from payments.models import PaymentOrder
from payments.gateway_adapters.copay import CopayError
from .models import RenewalBillingSettings, RenewalPreference, RenewalConsentEvent, RenewalInvoice

SALT = "gym.automatic-renewal-consent.v1"


def plan_quote(plan, policy):
    return signing.dumps(
        {
            "id": str(plan.pk),
            "name": plan.name,
            "price": str(plan.price),
            "period": plan.period,
            "policy": policy,
        },
        salt=SALT,
    )


def read_quote(token):
    try:
        data = signing.loads(token, salt=SALT, max_age=86400)
        plan = MembershipPlan.objects.get(pk=data["id"], is_active=True)
        if (plan.name, str(plan.price), plan.period) != (
            data["name"],
            data["price"],
            data["period"],
        ):
            raise ValueError
        return plan, data["policy"]
    except (signing.BadSignature, KeyError, ValueError, TypeError, MembershipPlan.DoesNotExist):
        raise EnrollmentError(
            "This plan quote changed or expired. Reload and confirm the current price and policy."
        ) from None


def member_access(gym, member):
    return bool(
        member.is_active
        and member.user_id
        and Membership.objects.filter(
            organization=gym,
            user_id=member.user_id,
            user__is_active=True,
            role="member",
            is_active=True,
        ).exists()
    )


def opt_in(alias, gym, member_id, user_id, token):
    with gym_database(alias), transaction.atomic(using=alias):
        member = MemberProfile.objects.select_for_update().get(
            pk=member_id, user_id=user_id, is_active=True
        )
        config = RenewalBillingSettings.objects.get(pk=1)
        plan, policy = read_quote(token)
        if not config.enabled or policy != config.late_policy or not member_access(gym, member):
            raise EnrollmentError(
                "Automatic invoicing is unavailable or the gym policy changed. Reload and review it."
            )
        if plan.period not in {"month", "quarter", "year"} or plan.price <= 0:
            raise EnrollmentError("Choose a paid monthly, quarterly or yearly plan.")
        if not member.subscriptions.filter(status="active").exists():
            raise EnrollmentError(
                "Your gym must activate an initial membership before automatic renewal invoicing can start."
            )
        preference = RenewalPreference.objects.filter(member=member).first()
        revision = preference.revision + 1 if preference else 1
        preference, _ = RenewalPreference.objects.update_or_create(
            member=member,
            defaults=dict(
                enabled=True,
                plan=plan,
                plan_name=plan.name,
                price=plan.price,
                period=plan.period,
                late_policy=policy,
                consented_at=timezone.now(),
                revision=revision,
                last_note="Consent recorded. No automatic charge is authorized.",
            ),
        )
        RenewalConsentEvent.objects.create(
            member=member,
            actor_id=user_id,
            action="enabled",
            snapshot={
                "plan": str(plan.pk),
                "name": plan.name,
                "price": str(plan.price),
                "period": plan.period,
                "late_policy": policy,
                "revision": revision,
                "terms": "Generate renewal invoices only. Each payment requires my own COPAY checkout approval.",
            },
        )
        return preference


def cancel_invoice(alias, invoice_id, actor_id, note):
    preview = RenewalInvoice.objects.get(pk=invoice_id)
    with transaction.atomic(using=alias):
        MemberProfile.objects.select_for_update().get(pk=preview.member_id)
        term = MemberSubscription.objects.select_for_update().get(pk=preview.subscription_id)
        row = RenewalInvoice.objects.select_for_update().get(pk=invoice_id)
        order = PaymentOrder.objects.select_for_update().filter(subscription=term).first()
        if row.status == "cancelled":
            return row
        if term.status != "pending" or order and order.status != "created":
            raise EnrollmentError(
                "Payment was submitted or membership changed. Recheck payment and contact the gym; no funds or access were reversed."
            )
        term.status = "cancelled"
        term.save(update_fields=["status"])
        row.status, row.note = "cancelled", note[:500]
        row.save(update_fields=["status", "note"])
        record_event(term, actor_id, "renewal_cancel", note)
        return row


def opt_out(alias, member_id, user_id):
    with gym_database(alias), transaction.atomic(using=alias):
        member = MemberProfile.objects.select_for_update().get(
            pk=member_id, user_id=user_id, is_active=True
        )
        preference = RenewalPreference.objects.select_for_update().filter(member=member).first()
        if not preference or not preference.enabled:
            return
        preference.enabled = False
        preference.last_note = (
            "Automatic invoicing stopped. Submitted payments still require reconciliation."
        )
        preference.save(update_fields=["enabled", "last_note"])
        RenewalConsentEvent.objects.create(
            member=member,
            actor_id=user_id,
            action="disabled",
            snapshot={"revision": preference.revision},
        )
        for invoice in member.renewal_invoices.filter(status="issued"):
            try:
                cancel_invoice(
                    alias, invoice.pk, user_id, "Member stopped automatic renewal invoicing."
                )
            except EnrollmentError:
                pass


def generate_invoice(alias, gym, member_id):
    from payments.services import create_order

    with gym_database(alias), transaction.atomic(using=alias):
        member = MemberProfile.objects.select_for_update().get(pk=member_id)
        preference = (
            RenewalPreference.objects.select_for_update()
            .select_related("plan")
            .filter(member=member, enabled=True)
            .first()
        )
        config = RenewalBillingSettings.objects.filter(pk=1).first()
        if not preference or not config or not config.enabled or not member_access(gym, member):
            return None

        def pause(message):
            if preference.last_note != message:
                preference.last_note = message
                preference.save(update_fields=["last_note"])
            return None

        plan = preference.plan
        if not plan.is_active or (plan.name, plan.price, plan.period, config.late_policy) != (
            preference.plan_name,
            preference.price,
            preference.period,
            preference.late_policy,
        ):
            queue_email(
                alias,
                key=f"auto-consent:{preference.pk}:{preference.revision}:review",
                category="membership_updates",
                title="Review your renewal preferences",
                message="Your gym plan or late-payment policy changed. Review the current terms before enabling future renewal invoices.",
                member=member,
                path="/portal/automatic-renewals/",
            )
            return pause(
                "Plan price, period, availability or policy changed. Reconfirm consent for future invoices."
            )
        if settings.CENTRAL_COLLECTIONS_ENABLED:
            return pause("Per-gym merchant mode must be configured before automatic invoicing.")
        if member.subscriptions.filter(status="frozen").exists():
            return pause("Frozen membership requires gym review.")
        base = (
            member.subscriptions.filter(status="active")
            .order_by("-expires_on", "-created_at")
            .first()
        )
        today = timezone.localdate()
        if not base or base.expires_on > today + timedelta(days=config.lead_days):
            return None
        existing = RenewalInvoice.objects.filter(previous_subscription=base).first()
        if existing:
            return existing
        if (
            member.subscriptions.filter(status="pending", expires_on__gt=today).exists()
            or RenewalRequest.objects.filter(member=member, status="pending").exists()
        ):
            return pause(
                "An existing pending membership or renewal request needs completion first."
            )
        start = (
            max(base.expires_on, today) if preference.late_policy == "restart" else base.expires_on
        )
        end = end_of_term(start, plan.period)
        if end <= today:
            return pause("The unpaid cycle ended. Contact your gym for a new term.")
        if member.subscriptions.filter(
            status="active", starts_on__lt=end, expires_on__gt=start
        ).exists():
            return pause("Another active or scheduled term overlaps this renewal.")
        term = MemberSubscription.objects.create(
            member=member,
            plan=plan,
            plan_name=plan.name,
            price=plan.price,
            period=plan.period,
            starts_on=start,
            expires_on=end,
        )
        # Loading credentials and creating the order performs no provider network I/O.
        order = create_order(alias, gym, term.pk)
        invoice = RenewalInvoice.objects.create(
            member=member,
            previous_subscription=base,
            subscription=term,
            due_on=base.expires_on,
            late_policy=preference.late_policy,
            consent_revision=preference.revision,
        )
        record_event(
            term,
            None,
            "auto_invoice",
            f"Automatic renewal invoice {invoice.number}; member consent revision {preference.revision}; payment required.",
        )
        preference.last_note = (
            "Renewal invoice generated. Authorize a one-time COPAY checkout to pay."
        )
        preference.save(update_fields=["last_note"])
        return invoice


def prepare_term(alias, term, order, *, confirmed=False, gym=None):
    """Caller owns member/term/order locks; never mutate dates for an ordinary term."""
    invoice = RenewalInvoice.objects.select_for_update().filter(subscription=term).first()
    if not invoice:
        return
    if term.status != "pending":
        raise EnrollmentError("This renewal term is no longer pending.")
    if not confirmed:
        preference = RenewalPreference.objects.filter(member=term.member, enabled=True).first()
        config = RenewalBillingSettings.objects.filter(pk=1, enabled=True).first()
        if (
            invoice.status != "issued"
            or not preference
            or not config
            or not term.plan.is_active
            or gym is None
            or not member_access(gym, term.member)
        ):
            raise EnrollmentError(
                "Automatic renewal invoice stopped or unavailable. Contact your gym."
            )
    start = term.starts_on
    end = term.expires_on
    if invoice.late_policy == "restart" and start < timezone.localdate():
        start = timezone.localdate()
        end = end_of_term(start, term.period)
    conflicts = MemberSubscription.objects.filter(member=term.member).exclude(pk=term.pk)
    if (
        conflicts.filter(status="frozen").exists()
        or conflicts.filter(
            status__in=["active", "pending"], starts_on__lt=end, expires_on__gt=start
        ).exists()
    ):
        raise EnrollmentError(
            "Renewal dates conflict with an existing membership. Owner review required."
        )
    if start != term.starts_on:
        term.starts_on, term.expires_on = start, end
        term.save(update_fields=["starts_on", "expires_on"])
        record_event(
            term,
            None,
            "renewal_dates",
            f"Late renewal dates moved to {start} through {end}, per agreed restart policy.",
        )


def sync_invoice_status(alias, order):
    invoice = RenewalInvoice.objects.filter(subscription_id=order.subscription_id).first()
    if not invoice:
        return
    if order.status == "paid":
        status = (
            "sandbox"
            if order.environment == "sandbox"
            else "paid" if order.subscription.status == "active" else "review"
        )
        RenewalInvoice.objects.filter(pk=invoice.pk).update(
            status=status, note=order.access_note[:500]
        )
    elif order.status == "failed":
        RenewalInvoice.objects.filter(pk=invoice.pk).update(
            status="review",
            note="COPAY reported a failed payment. Contact the gym; do not create a duplicate checkout.",
        )


def queue_invoice_reminders(alias, invoice):
    term = invoice.subscription
    order = PaymentOrder.objects.filter(subscription=term).first()
    preference = RenewalPreference.objects.filter(member=invoice.member, enabled=True).first()
    if (
        invoice.status != "issued"
        or term.status != "pending"
        or not preference
        or not order
        or order.status in {"paid", "failed"}
    ):
        return
    today = timezone.localdate()
    days = (invoice.due_on - today).days
    if days not in {7, 1, 0} and timezone.localdate(invoice.created_at) != today:
        return
    reminder = str(days) if days in {7, 1, 0} else "issued"
    policy = (
        "Late payment restarts the term from confirmed payment."
        if invoice.late_policy == "restart"
        else "The original service dates remain in effect after late payment."
    )
    queue_email(
        alias,
        key=f"auto-invoice:{invoice.pk}:reminder:{reminder}",
        category="membership_reminders",
        title="Membership renewal payment due",
        message=f"Invoice {invoice.number}\n{term.plan_name}: PHP {term.price:.2f}\nDue: {invoice.due_on}\n{policy}\nOpen your portal and authorize a one-time COPAY payment. No automatic debit will occur.",
        member=invoice.member,
        path=f"/payments/{order.pk}/",
        source_kind="renewal_invoice",
        source_id=invoice.pk,
        source_version=str(preference.revision),
        expires_at=timezone.now() + timedelta(hours=24),
    )


def sweep_invoices(alias, gym):
    """Per registered gym; caller verifies SaaS entitlements before invoking."""
    with gym_database(alias):
        config = RenewalBillingSettings.objects.filter(pk=1, enabled=True).first()
        if not config:
            return
        for member_id in (
            RenewalPreference.objects.filter(enabled=True, member__is_active=True)
            .values_list("member_id", flat=True)
            .iterator(chunk_size=100)
        ):
            try:
                generate_invoice(alias, gym, member_id)
            except (CopayError, EnrollmentError):
                # Keep per-member errors generic. No credentials, plaintext or gateway calls.
                RenewalPreference.objects.filter(member_id=member_id).update(
                    last_note="Invoice setup needs gym review. Check merchant configuration and membership eligibility."
                )
        for invoice in (
            RenewalInvoice.objects.filter(status="issued")
            .select_related("subscription", "member")
            .iterator(chunk_size=100)
        ):
            if invoice.subscription.status == "active":
                RenewalInvoice.objects.filter(pk=invoice.pk).update(
                    status="fulfilled",
                    note="Membership activated manually; review offline payment evidence.",
                )
            elif invoice.subscription.status == "cancelled":
                RenewalInvoice.objects.filter(pk=invoice.pk).update(
                    status="cancelled", note="Membership term cancelled by the gym."
                )
            else:
                queue_invoice_reminders(alias, invoice)
        RenewalBillingSettings.objects.filter(pk=1).update(last_worker_check=timezone.now())
