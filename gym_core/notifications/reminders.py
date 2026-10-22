from datetime import timedelta
from django.utils import timezone
from config.database_router import gym_database
from gym_core.memberships.models import MemberSubscription
from gym_core.classes.models import ClassBooking
from gym_core.renewal_billing.models import RenewalBillingSettings
from .models import NotificationSettings
from .services import queue_email


def queue_reminders(alias):
    now, today = timezone.now(), timezone.localdate()
    with gym_database(alias):
        config = NotificationSettings.objects.filter(pk=1).first()
        if not config or not config.email_enabled:
            return
        automatic=RenewalBillingSettings.objects.filter(pk=1,enabled=True).exists()
        if config.membership_reminders:
            for days in [7,1,0]:
                expiry = today+timedelta(days=days)
                terms = MemberSubscription.objects.filter(status='active',expires_on=expiry,
                    member__is_active=True,member__email_notifications=True).select_related('member')
                if automatic:
                    terms=terms.exclude(next_renewal_invoice__status='issued',member__renewal_preference__enabled=True)
                for term in terms.iterator(chunk_size=100):
                    if MemberSubscription.objects.filter(member=term.member,status='active',starts_on__lte=expiry,
                            expires_on__gt=expiry).exclude(pk=term.pk).exists():
                        continue
                    title = 'Membership expired' if days == 0 else f'Membership expires in {days} day'+('s' if days != 1 else '')
                    queue_email(alias,key=f'membership:{term.pk}:expiry:{expiry}:{days}',category='membership_reminders',
                        title=title,message=f'Your {term.plan_name} membership expires at the start of {expiry:%d %b %Y}. Contact your gym to renew. No payment has been collected.',
                        member=term.member,source_kind='membership',source_id=term.pk,source_version=expiry.isoformat(),
                        expires_at=now+timedelta(hours=24))
        if config.class_reminders:
            bookings = ClassBooking.objects.filter(status='confirmed',session__cancelled_at__isnull=True,
                session__starts_at__gt=now,session__starts_at__lte=now+timedelta(hours=24),
                member__is_active=True,member__email_notifications=True).select_related('member','session__location','session__trainer')
            for booking in bookings.iterator(chunk_size=100):
                session = booking.session
                queue_email(alias,key=f'class-reminder:{booking.pk}:{booking.queued_at.isoformat()}',category='class_reminders',
                    title='Your upcoming class',message=f'{session.title}\n{timezone.localtime(session.starts_at):%d %b %Y, %I:%M %p %Z}\n{session.location.name}\nTrainer: {session.trainer.name}',
                    member=booking.member,path='/portal/classes/',source_kind='booking',source_id=booking.pk,
                    source_version=booking.queued_at.isoformat(),expected_status='confirmed',expires_at=session.starts_at)
