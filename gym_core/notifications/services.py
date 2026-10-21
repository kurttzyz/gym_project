import re
from datetime import timedelta
from urllib.parse import urlsplit
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.template.loader import render_to_string
from django.utils import timezone
from config.database_router import gym_database
from gyms.models import GymDomain
from websites.models import WebsiteSettings
from .models import NotificationSettings, EmailNotification


def validated_origin(gym, value):
    try:
        parts = urlsplit(value)
        parts.port
    except ValueError:
        raise ValidationError('Enter a valid website origin.') from None
    if (parts.scheme not in ({'https','http'} if settings.DEBUG else {'https'}) or not parts.hostname
            or parts.username or parts.password or parts.path not in {'','/'} or parts.query or parts.fragment
            or not GymDomain.objects.filter(organization=gym, hostname=parts.hostname.lower(),
                is_active=True, verified_at__isnull=False).exists()):
        raise ValidationError('Use an active verified domain belonging to this gym, with HTTPS in production and no path.')
    return value.rstrip('/')


def queue_email(alias, *, key, category, title, message, member=None, path='/portal/', module='memberships',
                expires_at=None, source_kind='', source_id=None, source_version='', expected_status=''):
    """Persist alongside the originating tenant transaction; never perform network I/O."""
    with gym_database(alias):
        config = NotificationSettings.objects.filter(pk=1).first()
        if not config or not config.email_enabled or not getattr(config, category):
            return None
        if member is not None:
            if not member.is_active or not member.email_notifications:
                return None
            recipient = member.email
        else:
            recipient = config.owner_email
        if not recipient:
            return None
        try:
            validate_email(recipient)
        except ValidationError:
            return None
        site = WebsiteSettings.objects.get(pk=1)
        name = site.display_name.replace('\r',' ').replace('\n',' ')[:200]
        context = {'gym_name':name, 'color':site.primary_color if re.fullmatch(r'#[0-9a-fA-F]{6}',site.primary_color) else '#245ac5', 'title':title, 'message':message,
                   'url':config.public_origin.rstrip('/')+path if config.public_origin else '',
                   'preferences_url':config.public_origin.rstrip('/')+'/portal/notifications/' if member and config.public_origin else ''}
        try:
            validate_email(site.contact_email)
            reply_to = site.contact_email
        except ValidationError:
            reply_to = ''
        return EmailNotification.objects.get_or_create(deduplication_key=key, defaults=dict(
            category=category, module=module, member=member, recipient=recipient, sender_name=name,
            reply_to=reply_to, subject=f'{name}: {title}'[:255],
            text_body=render_to_string('notifications/email.txt',context),
            html_body=render_to_string('notifications/email.html',context),
            source_kind=source_kind, source_id=source_id, source_version=source_version,
            expected_status=expected_status, origin=config.public_origin.rstrip('/'),
            available_at=timezone.now(), expires_at=expires_at or timezone.now()+timedelta(days=7)))[0]


def booking_event(alias, event):
    booking = event.booking
    session = booking.session
    labels = {'confirmed':'Class booking confirmed','waitlisted':'You joined a class waitlist',
        'promoted':'A class place is now confirmed','cancelled':'Your class booking was cancelled',
        'session_cancelled':'Your gym cancelled a class'}
    if event.action not in labels:
        return
    message = f'{session.title}\n{timezone.localtime(session.starts_at):%d %b %Y, %I:%M %p %Z}\n{session.location.name}\nTrainer: {session.trainer.name}'
    if event.action == 'session_cancelled':
        message += '\nReason: '+session.cancellation_reason
    queue_email(alias, key=f'booking-event:{event.pk}', category='bookings', title=labels[event.action],
        message=message, member=booking.member, path='/portal/classes/', source_kind='booking', source_id=booking.pk,
        source_version=booking.queued_at.isoformat(), expected_status=booking.status,
        expires_at=max(session.starts_at, timezone.now()+timedelta(hours=1)))


def payment_event(alias, order):
    if order.environment != 'production' or order.status not in {'paid','failed'}:
        return
    if order.status == 'paid':
        queue_email(alias,key=f'payment:{order.pk}:paid', category='payment_confirmations', title='Payment received',
            message=f'We recorded your payment of {order.currency} {order.amount:.2f}.\nReference: {order.merchant_reference}\n{order.access_note}',
            member=order.subscription.member, path=f'/payments/{order.pk}/', module='payments',
            source_kind='payment', source_id=order.pk, expected_status='paid')
    if order.status == 'failed' or 'owner review' in order.access_note.lower():
        queue_email(alias,key=f'payment:{order.pk}:owner:{order.status}',category='owner_alerts',
            title='Payment needs attention', message=f'Payment reference: {order.merchant_reference}\nStatus: {order.status}\n{order.access_note}',
            path='/manage/payments/',module='payments',source_kind='payment',source_id=order.pk,expected_status=order.status)
