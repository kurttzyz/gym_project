from django.db import connections, transaction
from django.utils import timezone
from config.database_router import gym_database
from gym_core.members.models import MemberProfile
from gym_core.memberships.models import MemberSubscription
from .models import Trainer, ClassSession, ClassBooking, BookingEvent


class BookingError(ValueError):
    pass


def lock(alias):
    # Tenant-local transaction lock serializes seat allocation, timetable changes,
    # and cross-branch member overlap checks without locking another gym.
    with connections[alias].cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', [72419302])


def eligible(member, session):
    first = timezone.localtime(session.starts_at).date()
    last = timezone.localtime(session.ends_at - timezone.timedelta(microseconds=1)).date()
    return member.is_active and member.subscriptions.filter(status=MemberSubscription.Status.ACTIVE,
        starts_on__lte=first, expires_on__gt=last).exists()


def overlap(member, session):
    return ClassBooking.objects.filter(member=member, status='confirmed', session__cancelled_at__isnull=True,
        session__starts_at__lt=session.ends_at, session__ends_at__gt=session.starts_at).exclude(session=session).exists()


def promote(session):
    if session.cancelled_at or session.starts_at <= timezone.now() or not session.location.is_active or not session.trainer.is_active:
        return
    seats = session.capacity - session.bookings.filter(status='confirmed').count()
    for booking in session.bookings.filter(status='waitlisted').select_related('member'):
        if seats <= 0:
            break
        if eligible(booking.member, session) and not overlap(booking.member, session):
            booking.status = 'confirmed'
            booking.save(update_fields=['status', 'updated_at'])
            BookingEvent.objects.create(booking=booking, action='promoted', actor_id=None)
            seats -= 1


def create_session(alias, data, actor_id):
    with gym_database(alias), transaction.atomic(using=alias):
        lock(alias)
        trainer = Trainer.objects.get(pk=data['trainer'].pk)
        location = type(data['location']).objects.get(pk=data['location'].pk)
        start, end = data['starts_at'], data['ends_at']
        if start <= timezone.now() or end <= start or not 1 <= data['capacity'] <= 500:
            raise BookingError('Choose a future start, a later end, and capacity between 1 and 500.')
        if not trainer.is_active or not location.is_active:
            raise BookingError('Choose an active trainer and branch.')
        if ClassSession.objects.filter(trainer=trainer, cancelled_at__isnull=True,
                starts_at__lt=end, ends_at__gt=start).exists():
            raise BookingError('This trainer already has a session at that time.')
        return ClassSession.objects.create(**data, created_by_id=actor_id)


def book(alias, session_id, member_id, actor_id):
    with gym_database(alias), transaction.atomic(using=alias):
        lock(alias)
        session = ClassSession.objects.select_related('location').get(pk=session_id)
        member = MemberProfile.objects.get(pk=member_id, user_id=actor_id, is_active=True)
        if session.cancelled_at or session.starts_at <= timezone.now() or not session.location.is_active or not session.trainer.is_active:
            raise BookingError('This session is no longer available for booking.')
        if not eligible(member, session):
            raise BookingError('An active membership covering the whole session is required.')
        existing = ClassBooking.objects.filter(session=session, member=member).first()
        if existing and existing.status != 'cancelled':
            promote(session)
            existing.refresh_from_db()
            return existing
        if overlap(member, session):
            raise BookingError('You already have a confirmed class at that time.')
        promote(session)
        status = 'confirmed' if session.bookings.filter(status='confirmed').count() < session.capacity else 'waitlisted'
        if existing:
            existing.status, existing.queued_at = status, timezone.now()
            existing.save()
            booking = existing
        else:
            booking = ClassBooking.objects.create(session=session, member=member, status=status, queued_at=timezone.now())
        BookingEvent.objects.create(booking=booking, action=status, actor_id=actor_id)
        return booking


def cancel_booking(alias, booking_id, actor_id):
    with gym_database(alias), transaction.atomic(using=alias):
        lock(alias)
        booking = ClassBooking.objects.select_related('session__location').get(pk=booking_id, member__user_id=actor_id)
        if booking.status == 'cancelled':
            return
        if booking.session.starts_at <= timezone.now():
            raise BookingError('This class has already started. Contact your gym.')
        booking.status = 'cancelled'
        booking.save(update_fields=['status', 'updated_at'])
        BookingEvent.objects.create(booking=booking, action='cancelled', actor_id=actor_id)
        promote(booking.session)


def cancel_session(alias, session_id, reason, actor_id):
    with gym_database(alias), transaction.atomic(using=alias):
        lock(alias)
        session = ClassSession.objects.get(pk=session_id)
        if session.cancelled_at:
            return
        if not reason.strip() or session.starts_at <= timezone.now():
            raise BookingError('Provide a reason; only future sessions can be cancelled.')
        session.cancelled_at, session.cancellation_reason = timezone.now(), reason.strip()[:500]
        session.save(update_fields=['cancelled_at', 'cancellation_reason'])
        for booking in session.bookings.exclude(status='cancelled'):
            booking.status = 'cancelled'
            booking.save(update_fields=['status', 'updated_at'])
            BookingEvent.objects.create(booking=booking, action='session_cancelled', actor_id=actor_id)
