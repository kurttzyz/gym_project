"""Read-only tenant dashboard projections. Call only inside gym_database()."""
from datetime import timedelta
from django.db.models import Count,Sum,Prefetch,Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from .members.models import MemberProfile
from .memberships.models import MemberSubscription,MembershipApplication
from .attendance.models import AttendanceVisit
from .classes.models import ClassSession,ClassBooking
from .self_service.models import RenewalRequest
from payments.models import PaymentOrder
from websites.models import ContactMessage


def initials(name):
    return ''.join(word[0] for word in name.split()[:2]).upper() or 'M'


def owner_overview(modules,period=7):
    today=timezone.localdate()
    data={'dashboard_period':period,'dashboard_today':today,'recent_members':[], 'today_classes':[],
        'attendance_chart':[],'period_checkins':0,'today_checkins':0,'open_visits':0,
        'active_members':0,'application_count':0,'renewal_count':0,'message_count':0,'month_receipts':0}
    if modules.get('memberships'):
        active=MemberSubscription.objects.filter(status='active',starts_on__lte=today,expires_on__gt=today,member__is_active=True)
        data['active_members']=active.values('member_id').distinct().count()
        data['application_count']=MembershipApplication.objects.filter(status='new').count()
        data['renewal_count']=RenewalRequest.objects.filter(status='pending').count()
        profiles=MemberProfile.objects.order_by('-created_at','id').prefetch_related(Prefetch('subscriptions',queryset=active,to_attr='dashboard_terms'))[:5]
        data['recent_members']=[{'id':p.pk,'name':p.full_name,'initials':initials(p.full_name),'number':p.member_number,
            'status':'Active' if p.is_active and p.dashboard_terms else 'No active term' if p.is_active else 'Disabled',
            'tone':'' if p.is_active and p.dashboard_terms else 'neutral','plan':p.dashboard_terms[0].plan_name if p.dashboard_terms else 'No current membership'} for p in profiles]
        data['today_classes']=list(ClassSession.objects.filter(cancelled_at__isnull=True,starts_at__date=today,
            ends_at__gt=timezone.now()).select_related('trainer','location').annotate(
            confirmed_count=Count('bookings',filter=Q(bookings__status='confirmed'))).order_by('starts_at','id')[:4])
    if modules.get('attendance'):
        start=today-timedelta(days=period-1)
        counts=dict(AttendanceVisit.objects.filter(checked_in_at__date__gte=start,checked_in_at__date__lte=today).annotate(
            day=TruncDate('checked_in_at')).values('day').annotate(total=Count('pk')).values_list('day','total'))
        maximum=max(counts.values(),default=0) or 1
        data['attendance_chart']=[{'date':day,'label':day.strftime('%a') if period==7 else day.strftime('%d'),
            'value':counts.get(day,0),'height':round(counts.get(day,0)/maximum*100,2)} for day in (start+timedelta(days=i) for i in range(period))]
        data['period_checkins']=sum(counts.values())
        data['today_checkins']=counts.get(today,0)
        data['open_visits']=AttendanceVisit.objects.filter(checked_out_at__isnull=True).count()
    if modules.get('websites'):data['message_count']=ContactMessage.objects.filter(is_read=False).count()
    if modules.get('payments'):
        data['month_receipts']=PaymentOrder.objects.filter(status='paid',environment='production',currency='PHP',paid_at__date__gte=today.replace(day=1),
            paid_at__date__lte=today).aggregate(total=Sum('net_amount'))['total'] or 0
    return data


def member_overview(profile,modules,subscriptions):
    today=timezone.localdate()
    data={'current_membership':None,'next_membership':None,'upcoming_bookings':[], 'recent_visits':[],
        'member_initials':initials(profile.full_name),'month_visits':0,'upcoming_count':0}
    eligible=[term for term in subscriptions if term.status=='active' and term.starts_on<=today<term.expires_on]
    data['current_membership']=max(eligible,key=lambda term:term.expires_on) if eligible else None
    future=[term for term in subscriptions if term.status=='active' and term.starts_on>today]
    data['next_membership']=min(future,key=lambda term:term.starts_on) if future else None
    if data['current_membership']:data['days_remaining']=(data['current_membership'].expires_on-today).days
    if modules.get('memberships'):
        bookings=ClassBooking.objects.filter(member=profile,status__in=['confirmed','waitlisted'],session__cancelled_at__isnull=True,
            session__ends_at__gt=timezone.now()).select_related('session__trainer','session__location').order_by('session__starts_at','id')
        data['upcoming_count']=bookings.count()
        data['upcoming_bookings']=list(bookings[:3])
    if modules.get('attendance'):
        data['month_visits']=profile.visits.filter(checked_in_at__date__gte=today.replace(day=1),checked_in_at__date__lte=today).count()
        data['recent_visits']=list(profile.visits.select_related('location')[:4])
    return data
