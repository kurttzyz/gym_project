import calendar
import hashlib
import secrets
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from gyms.models import Membership
from .models import MemberInvitation, MemberProfile
from gym_core.memberships.models import MembershipApplication, MembershipEvent, MembershipPlan, MemberSubscription


class EnrollmentError(Exception):
    pass


def end_of_term(start, period):
    try:
        if period == "day":
            return start + timedelta(days=1)
        months = {"month": 1, "quarter": 3, "year": 12}.get(period)
        if months is None:
            raise EnrollmentError("Unsupported membership term")
        year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
        return start.replace(year=year, month=month + 1,
                             day=min(start.day, calendar.monthrange(year, month + 1)[1]))
    except (ValueError, OverflowError):
        raise EnrollmentError("Membership dates are outside the supported range.") from None


def record_event(subscription, actor_id, action, note):
    MembershipEvent.objects.create(subscription=subscription, actor_id=actor_id, action=action, note=note)


def enroll_application(alias, application_id, actor_id):
    with transaction.atomic(using=alias):
        application = MembershipApplication.objects.select_for_update().get(pk=application_id)
        existing = MemberSubscription.objects.filter(application=application).select_related("member").first()
        if existing:
            return existing.member, existing
        if application.status != "approved":
            raise EnrollmentError("Approve the application before enrolling it.")
        member, _ = MemberProfile.objects.get_or_create(
            email__iexact=application.email,
            defaults={"email": application.email, "full_name": application.full_name, "phone": application.phone},
        )
        member = MemberProfile.objects.select_for_update().get(pk=member.pk)
        if not member.is_active:
            raise EnrollmentError("This member is disabled. Review their profile before enrolling.")
        start = timezone.localdate()
        subscription = MemberSubscription.objects.create(
            member=member, plan=application.plan, application=application,
            plan_name=application.quoted_plan_name, price=application.quoted_price,
            period=application.quoted_period, starts_on=start,
            expires_on=end_of_term(start, application.quoted_period),
        )
        record_event(subscription, actor_id, "enrolled", "Enrolled from an approved application; pending activation.")
        return member, subscription


def assign_plan(alias, member_id, plan_id, start, actor_id):
    with transaction.atomic(using=alias):
        member = MemberProfile.objects.select_for_update().get(pk=member_id)
        if not member.is_active:
            raise EnrollmentError("Enable this member before assigning a plan.")
        plan = MembershipPlan.objects.get(pk=plan_id, is_active=True)
        subscription = MemberSubscription.objects.filter(
            member=member, plan=plan, starts_on=start, status="pending",
        ).first()
        if subscription:
            return subscription
        subscription = MemberSubscription.objects.create(
            member=member, plan=plan, plan_name=plan.name, price=plan.price, period=plan.period,
            starts_on=start, expires_on=end_of_term(start, plan.period),
        )
        record_event(subscription, actor_id, "assigned", "Assigned a plan; pending activation.")
        return subscription


def change_subscription(alias, member_id, subscription_id, action, note, actor_id):
    if not note.strip():
        raise EnrollmentError("A reason or offline reference is required.")
    with transaction.atomic(using=alias):
        member = MemberProfile.objects.select_for_update().get(pk=member_id)
        subscription = MemberSubscription.objects.select_for_update().get(pk=subscription_id, member=member)
        today = timezone.localdate()
        if action in {"activate", "resume"} and not member.is_active:
            raise EnrollmentError("Enable this member before activating or resuming access.")
        if action == "activate":
            if not member.is_active or subscription.status != "pending":
                raise EnrollmentError("Only a pending membership for an enabled member can be activated.")
            if subscription.expires_on <= today:
                raise EnrollmentError("This term has already expired. Assign a new term instead.")
            overlaps = MemberSubscription.objects.filter(member=member).exclude(pk=subscription.pk).filter(
                status="frozen"
            ).exists() or MemberSubscription.objects.filter(
                member=member, status="active", starts_on__lt=subscription.expires_on,
                expires_on__gt=subscription.starts_on,
            ).exclude(pk=subscription.pk).exists()
            if overlaps:
                raise EnrollmentError("Another active or frozen membership overlaps this term.")
            subscription.status = "active"
        elif action == "freeze":
            if subscription.status != "active" or not subscription.starts_on <= today < subscription.expires_on:
                raise EnrollmentError("Only a currently active membership can be frozen.")
            subscription.status, subscription.frozen_on = "frozen", today
        elif action == "resume":
            if subscription.status != "frozen" or subscription.frozen_on is None:
                raise EnrollmentError("Only a frozen membership can be resumed.")
            extension = max((today - subscription.frozen_on).days, 0)
            new_expiry = subscription.expires_on + timedelta(days=extension)
            if MemberSubscription.objects.filter(
                member=member, status="active", starts_on__lt=new_expiry, expires_on__gt=today
            ).exclude(pk=subscription.pk).exists():
                raise EnrollmentError("Resuming would overlap another scheduled membership.")
            subscription.expires_on = new_expiry
            subscription.status, subscription.frozen_on = "active", None
        elif action == "cancel":
            if subscription.status == "cancelled":
                raise EnrollmentError("This membership is already cancelled.")
            subscription.status, subscription.frozen_on = "cancelled", None
        else:
            raise EnrollmentError("Unknown membership action.")
        subscription.save()
        record_event(subscription, actor_id, action, note.strip())
        return subscription


def issue_invitation(alias, member_id, actor_id):
    with transaction.atomic(using=alias):
        member = MemberProfile.objects.select_for_update().get(pk=member_id)
        if not member.is_active or member.user_id:
            raise EnrollmentError("Invitations are only available for enabled members without a linked login.")
        MemberInvitation.objects.filter(member=member, used_at__isnull=True, revoked_at__isnull=True).update(revoked_at=timezone.now())
        token = secrets.token_urlsafe(32)
        MemberInvitation.objects.create(
            member=member, token_digest=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(hours=48), created_by_id=actor_id,
        )
        return token


def invitation_for_token(token, *, lock=False):
    if len(token) > 128:
        raise EnrollmentError("This invitation is unavailable or expired.")
    query = MemberInvitation.objects.select_for_update() if lock else MemberInvitation.objects
    invitation = query.select_related("member").filter(token_digest=hashlib.sha256(token.encode()).hexdigest()).first()
    if not invitation or not invitation.available or not invitation.member.is_active or invitation.member.user_id:
        raise EnrollmentError("This invitation is unavailable or expired.")
    return invitation


def claim_invitation(alias, gym, token, form, *, existing=False, waiver_text=""):
    # The central commit occurs before the tenant commit. A failure after that
    # leaves an account that can retry via existing-account login. Member access
    # also requires the committed tenant profile, so partial central grants fail closed.
    with transaction.atomic(using=alias):
        preview = invitation_for_token(token)
        member = MemberProfile.objects.select_for_update().get(pk=preview.member_id)
        invitation = invitation_for_token(token, lock=True)
        with transaction.atomic(using="default"):
            if existing:
                user = form.get_user()
            else:
                user = form.save(commit=False)
                user.email = member.email
                user.save(using="default")
            if MemberProfile.objects.filter(user_id=user.pk).exclude(pk=member.pk).exists():
                raise EnrollmentError("This account is already linked to another member profile in this gym.")
            access, _ = Membership.objects.get_or_create(organization=gym, user=user, defaults={"role": "member"})
            if not access.is_active:
                raise EnrollmentError("This account's gym access is disabled. Contact the gym owner.")
            member.user_id = user.pk
            if waiver_text:
                member.waiver_text, member.waiver_accepted_at = waiver_text, timezone.now()
            member.save()
            invitation.used_at = timezone.now()
            invitation.save(update_fields=["used_at"])
        return user
