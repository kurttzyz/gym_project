from functools import wraps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from config.database_router import gym_database
from gyms.databases import ready_database, GymDatabaseUnavailable
from gyms.models import Membership
from gym_core.members.models import MemberProfile
from gym_core.memberships.views import owner_page
from gym_core.attendance.views import attendance_page
from .models import Trainer, ClassSession, ClassBooking
from .forms import TrainerForm, SessionForm, CancelSessionForm
from .services import create_session, book, cancel_booking, cancel_session, BookingError


def member_page(view):
    @login_required
    @never_cache
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.tenant is None or not Membership.objects.filter(organization=request.tenant,
                user=request.user, role='member', is_active=True).exists():
            raise Http404
        try:
            with gym_database(ready_database(request.tenant)):
                request.class_member = get_object_or_404(MemberProfile, user_id=request.user.pk, is_active=True)
                return view(request, *args, **kwargs)
        except (GymDatabaseUnavailable, DatabaseError, OSError):
            return render(request, 'gyms/unavailable.html', status=503)
    return wrapped


@owner_page
@never_cache
def trainers(request, trainer_id=None):
    trainer = get_object_or_404(Trainer, pk=trainer_id) if trainer_id else None
    form = TrainerForm(request.POST if request.method == 'POST' else None, instance=trainer)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Trainer saved.')
        return redirect('class-trainers')
    return render(request, 'classes/trainers.html', {'form': form, 'trainers': Trainer.objects.all(), 'editing': trainer})


@owner_page
@never_cache
def new_session(request):
    form = SessionForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            session = create_session(ready_database(request.tenant), form.cleaned_data, request.user.pk)
        except BookingError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, 'Class published.')
            return redirect('class-roster', session_id=session.pk)
    return render(request, 'classes/session_form.html', {'form': form})


@attendance_page
def schedule(request):
    sessions = ClassSession.objects.select_related('trainer', 'location').annotate(
        confirmed_count=Count('bookings', filter=Q(bookings__status='confirmed'))).order_by('starts_at', 'id')
    if request.GET.get('history') != '1':
        sessions = sessions.filter(ends_at__gt=timezone.now())
    return render(request, 'classes/schedule.html', {'page': Paginator(sessions, 25).get_page(request.GET.get('page')),
        'history': request.GET.get('history') == '1', 'can_manage': Membership.objects.filter(
            organization=request.tenant, user=request.user, role='owner', is_active=True).exists()})


@attendance_page
def roster(request, session_id):
    session = get_object_or_404(ClassSession.objects.select_related('trainer', 'location'), pk=session_id)
    return render(request, 'classes/roster.html', {'session': session,
        'bookings': session.bookings.select_related('member'),
        'can_manage': Membership.objects.filter(organization=request.tenant, user=request.user, role='owner', is_active=True).exists(),
        'cancel_form': CancelSessionForm()})


@owner_page
@never_cache
@require_POST
def cancel_class(request, session_id):
    get_object_or_404(ClassSession, pk=session_id)
    form = CancelSessionForm(request.POST)
    if form.is_valid():
        try:
            cancel_session(ready_database(request.tenant), session_id, form.cleaned_data['reason'], request.user.pk)
        except BookingError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, 'Class and its bookings cancelled.')
    else:
        messages.error(request, 'A cancellation reason is required (up to 500 characters).')
    return redirect('class-roster', session_id=session_id)


@member_page
def member_classes(request):
    sessions = ClassSession.objects.filter(ends_at__gt=timezone.now()).select_related('trainer', 'location').annotate(
        confirmed_count=Count('bookings', filter=Q(bookings__status='confirmed'))).order_by('starts_at', 'id')
    page = Paginator(sessions, 25).get_page(request.GET.get('page'))
    current = {booking.session_id: booking for booking in ClassBooking.objects.filter(
        member=request.class_member, session_id__in=[session.pk for session in page]).exclude(status='cancelled')}
    rows = [{'session': session, 'booking': current.get(session.pk)} for session in page]
    bookings = ClassBooking.objects.filter(member=request.class_member).select_related(
        'session__trainer', 'session__location').order_by('-session__starts_at', 'id')[:100]
    return render(request, 'classes/member_classes.html', {'rows': rows, 'page': page, 'bookings': bookings})


@member_page
@require_POST
def reserve(request, session_id):
    get_object_or_404(ClassSession, pk=session_id)
    try:
        booking = book(ready_database(request.tenant), session_id, request.class_member.pk, request.user.pk)
    except BookingError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, f'Booking {booking.get_status_display().lower()}.')
    return redirect('member-classes')


@member_page
@require_POST
def cancel_reservation(request, booking_id):
    get_object_or_404(ClassBooking, pk=booking_id, member=request.class_member)
    try:
        cancel_booking(ready_database(request.tenant), booking_id, request.user.pk)
    except BookingError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, 'Booking cancelled.')
    return redirect('member-classes')
