"""Tenant-scoped durable email worker. SMTP I/O runs outside database transactions."""
import smtplib
import uuid
from datetime import timedelta
from email.utils import formataddr
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from config.database_router import gym_database
from gym_core.classes.models import ClassBooking
from gym_core.memberships.models import MemberSubscription
from payments.models import PaymentOrder
from platform_core.billing.services import access_for_gym
from .models import NotificationSettings, EmailNotification, EmailAttempt
from .services import validated_origin


def transport_ready():
    try:
        validate_email(settings.DEFAULT_FROM_EMAIL)
    except ValidationError:
        return False
    return bool(settings.NOTIFICATION_DELIVERY_ENABLED and settings.EMAIL_HOST
                and not settings.DEFAULT_FROM_EMAIL.endswith('.invalid')
                and not (settings.EMAIL_USE_TLS and settings.EMAIL_USE_SSL))


def suppression(gym, notification, now):
    config = NotificationSettings.objects.filter(pk=1).first()
    if not config or not config.email_enabled or not getattr(config, notification.category, False):
        return 'gym_or_category_disabled'
    if notification.expires_at <= now:
        return 'notification_expired'
    access = access_for_gym(gym)
    if not access.allowed or not access.modules.get(notification.module, False):
        return 'subscription_or_module_unavailable'
    try:
        if not notification.origin or validated_origin(gym, notification.origin) != config.public_origin.rstrip('/'):
            return 'gym_domain_changed'
    except ValidationError:
        return 'gym_domain_unverified'
    if notification.member_id:
        member = notification.member
        if not member.is_active or not member.email_notifications or member.email != notification.recipient:
            return 'member_disabled_opted_out_or_email_changed'
    elif notification.recipient != config.owner_email:
        return 'owner_inbox_changed'
    if notification.source_kind == 'booking':
        booking = ClassBooking.objects.select_related('session__location', 'session__trainer').filter(pk=notification.source_id).first()
        if (not booking or booking.status != notification.expected_status
                or booking.queued_at.isoformat() != notification.source_version):
            return 'booking_changed'
        if notification.category == 'class_reminders':
            from gym_core.classes.services import eligible
            if booking.session.cancelled_at or not booking.session.location.is_active or not booking.session.trainer.is_active or not eligible(booking.member,booking.session):
                return 'class_no_longer_eligible'
    if notification.source_kind == 'membership':
        term = MemberSubscription.objects.filter(pk=notification.source_id).first()
        if not term or term.status != 'active' or term.expires_on.isoformat() != notification.source_version:
            return 'membership_changed'
        from gym_core.renewal_billing.models import RenewalInvoice,RenewalBillingSettings
        if (RenewalBillingSettings.objects.filter(pk=1,enabled=True).exists()
                and RenewalInvoice.objects.filter(previous_subscription=term,status='issued',member__renewal_preference__enabled=True).exists()):
            return 'renewal_invoice_available'
        today = timezone.localdate(now)
        if MemberSubscription.objects.filter(member_id=term.member_id,status='active',
                starts_on__lte=term.expires_on, expires_on__gt=max(today,term.expires_on)).exclude(pk=term.pk).exists():
            return 'membership_already_renewed'
    if notification.source_kind == 'renewal_invoice':
        from gym_core.renewal_billing.models import RenewalInvoice,RenewalPreference,RenewalBillingSettings
        from gym_core.renewal_billing.services import member_access
        invoice=RenewalInvoice.objects.select_related('subscription','member').filter(pk=notification.source_id).first()
        preference=RenewalPreference.objects.filter(member_id=notification.member_id,enabled=True).first()
        if (not invoice or invoice.status!='issued' or invoice.subscription.status!='pending' or not preference
                or not access.modules.get('payments',False) or not member_access(gym,invoice.member)
                or str(preference.revision)!=notification.source_version
                or not RenewalBillingSettings.objects.filter(pk=1,enabled=True).exists()
                or PaymentOrder.objects.filter(subscription_id=invoice.subscription_id,status__in=['paid','failed']).exists()):
            return 'renewal_invoice_changed'
    if notification.source_kind == 'payment':
        order = PaymentOrder.objects.filter(pk=notification.source_id).first()
        if not order or order.environment != 'production' or order.status != notification.expected_status:
            return 'payment_changed'
    return ''


def deliver_batch(gym, alias, limit=50):
    if not transport_ready():
        return 0
    delivered = 0
    for _ in range(limit):
        now = timezone.now()
        with gym_database(alias), transaction.atomic(using=alias):
            row = EmailNotification.objects.select_for_update(skip_locked=True).filter(
                Q(status='queued', available_at__lte=now) | Q(status='sending', lease_until__lte=now)
            ).order_by('available_at','created_at','id').first()
            if row is None:
                break
            if row.status == 'sending':
                EmailAttempt.objects.filter(notification=row,number=row.attempts,finished_at__isnull=True).update(
                    outcome='worker_lease_expired',finished_at=now)
            reason = suppression(gym,row,now)
            if reason or row.attempts >= row.attempt_limit:
                row.status, row.outcome = ('skipped',reason) if reason else ('failed','attempt_limit_reached')
                row.lease_until, row.claim_token = None, None
                row.save(update_fields=['status','outcome','lease_until','claim_token'])
                continue
            token = uuid.uuid4()
            row.status, row.claim_token, row.lease_until = 'sending', token, now+timedelta(minutes=5)
            row.attempts += 1
            row.outcome = ''
            row.save(update_fields=['status','claim_token','lease_until','attempts','outcome'])
            EmailAttempt.objects.create(notification=row,number=row.attempts,outcome='sending',started_at=now)
            message = EmailMultiAlternatives(row.subject,row.text_body,
                formataddr((row.sender_name,settings.DEFAULT_FROM_EMAIL)),[row.recipient],
                reply_to=[row.reply_to] if row.reply_to else [],
                headers={'Message-ID':f'<{row.pk}.{gym.database_key}@{settings.DEFAULT_FROM_EMAIL.rsplit("@",1)[-1]}>',
                         'Auto-Submitted':'auto-generated'})
            message.attach_alternative(row.html_body,'text/html')
        # No database locks across network I/O. Each message has one recipient.
        try:
            accepted = message.send(fail_silently=False)
            if accepted != 1:
                raise RuntimeError('mail_not_accepted')
            outcome = 'accepted_by_mail_server'
        except Exception as error:
            # Store a bounded code only: SMTP exceptions may contain passwords,
            # recipients, or full provider responses. Never persist their text.
            outcome = 'recipient_rejected' if isinstance(error,smtplib.SMTPRecipientsRefused) else 'transport_error'
        with gym_database(alias), transaction.atomic(using=alias):
            current = EmailNotification.objects.select_for_update().get(pk=row.pk)
            if current.claim_token != token:
                continue
            success = outcome == 'accepted_by_mail_server'
            terminal = current.attempts >= current.attempt_limit or outcome == 'recipient_rejected'
            current.status = 'sent' if success else 'failed' if terminal else 'queued'
            current.sent_at = timezone.now() if success else None
            current.outcome, current.lease_until, current.claim_token = outcome, None, None
            if not success:
                current.available_at = timezone.now()+timedelta(minutes=min(60,2**current.attempts))
            current.save(update_fields=['status','sent_at','outcome','lease_until','claim_token','available_at'])
            EmailAttempt.objects.filter(notification=current,number=current.attempts).update(outcome=outcome,finished_at=timezone.now())
            delivered += int(success)
    return delivered
