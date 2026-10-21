from datetime import timedelta
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from config.database_router import gym_database
from gym_core.members.models import MemberProfile
from gym_core.members.services import EnrollmentError, end_of_term, record_event
from gym_core.memberships.models import MembershipPlan, MemberSubscription
from gym_core.notifications.services import queue_email
from .models import RenewalRequest, MemberProfileChange
from .forms import SelfProfileForm


def locked_member(member_id,user_id):
    try:
        return MemberProfile.objects.select_for_update().get(pk=member_id,user_id=user_id,is_active=True)
    except MemberProfile.DoesNotExist:
        raise EnrollmentError('Your member access has changed. Please sign in again or contact your gym.') from None


def suggested_start(member):
    end = member.subscriptions.filter(status='active').aggregate(end=Max('expires_on'))['end']
    return max(timezone.localdate(),end) if end else timezone.localdate()


def update_profile(alias,member_id,user_id,data):
    with gym_database(alias),transaction.atomic(using=alias):
        member = locked_member(member_id,user_id)
        changes = {}
        for field in SelfProfileForm.Meta.fields:
            old,new = getattr(member,field),data[field]
            if old != new:
                changes[field] = {'before':str(old) if old is not None else '', 'after':str(new) if new is not None else ''}
                setattr(member,field,new)
        if changes:
            member.save(update_fields=list(changes))
            MemberProfileChange.objects.create(member=member,actor_id=user_id,changes=changes)
        return member


def check_term(member,start,end):
    today = timezone.localdate()
    if not member.is_active or not today <= start <= today+timedelta(days=365):
        raise EnrollmentError('Choose a start date from today through the next year for an enabled member.')
    if member.subscriptions.filter(status='frozen').exists():
        raise EnrollmentError('Contact your gym to review the frozen membership before renewing.')
    if member.subscriptions.filter(status='pending',expires_on__gt=today).exists():
        raise EnrollmentError('A membership is already pending. Contact your gym to complete or cancel it.')
    if member.subscriptions.filter(status='active',starts_on__lt=end,expires_on__gt=start).exists():
        raise EnrollmentError('This request overlaps an active or scheduled membership. Choose a later start date.')


def request_renewal(alias,member_id,user_id,plan_id,start,note):
    with gym_database(alias),transaction.atomic(using=alias):
        member = locked_member(member_id,user_id)
        existing = RenewalRequest.objects.filter(member=member,status='pending').first()
        if existing:
            if existing.plan_id == plan_id and existing.starts_on == start:
                return existing
            raise EnrollmentError('You already have a request awaiting review. Cancel it before requesting a different term.')
        plan = MembershipPlan.objects.get(pk=plan_id,is_active=True)
        end = end_of_term(start,plan.period)
        check_term(member,start,end)
        row = RenewalRequest.objects.create(member=member,plan=plan,plan_name=plan.name,price=plan.price,
            period=plan.period,starts_on=start,expires_on=end,note=note,requested_by_id=user_id)
        queue_email(alias,key=f'renewal:{row.pk}:new',category='owner_alerts',title='New membership renewal request',
            message=f'{member.full_name} requested {row.plan_name} from {start}. Review the quoted term in your dashboard.',path='/manage/renewals/')
        return row


def cancel_renewal(alias,request_id,member_id,user_id):
    with gym_database(alias),transaction.atomic(using=alias):
        member = locked_member(member_id,user_id)
        row = RenewalRequest.objects.select_for_update().get(pk=request_id,member=member)
        if row.status == 'cancelled':
            return row
        if row.status != 'pending':
            raise EnrollmentError('This request has already been reviewed. Contact your gym.')
        row.status, row.reviewed_at = 'cancelled',timezone.now()
        row.save(update_fields=['status','reviewed_at'])
        return row


def review_renewal(alias,request_id,action,note,actor_id):
    if action not in {'approve','decline'} or not note.strip() or len(note)>500:
        raise EnrollmentError('Choose an action and provide a review note of up to 500 characters.')
    with gym_database(alias),transaction.atomic(using=alias):
        preview = RenewalRequest.objects.get(pk=request_id)
        member = MemberProfile.objects.select_for_update().get(pk=preview.member_id)
        row = RenewalRequest.objects.select_for_update().get(pk=request_id)
        desired = 'approved' if action=='approve' else 'declined'
        if row.status == desired:
            return row
        if row.status != 'pending':
            raise EnrollmentError('This request is already closed.')
        if action == 'approve':
            if not MembershipPlan.objects.filter(pk=row.plan_id,is_active=True).exists():
                raise EnrollmentError('This plan is no longer available. Decline the request and discuss another plan.')
            check_term(member,row.starts_on,row.expires_on)
            row.subscription = MemberSubscription.objects.create(member=member,plan_id=row.plan_id,
                plan_name=row.plan_name,price=row.price,period=row.period,starts_on=row.starts_on,expires_on=row.expires_on)
            record_event(row.subscription,actor_id,'renewal',f'Approved renewal request {row.pk}; payment and activation pending. {note}')
        row.status,row.review_note,row.reviewed_by_id,row.reviewed_at = desired,note.strip(),actor_id,timezone.now()
        row.save(update_fields=['status','review_note','reviewed_by_id','reviewed_at','subscription'])
        queue_email(alias,key=f'renewal:{row.pk}:{desired}',category='membership_updates',title=f'Renewal request {desired}',
            message=f'{row.plan_name}\n{row.starts_on} to {row.expires_on}\nQuoted price: PHP {row.price:.2f}\n{row.review_note}\nApproval alone does not collect payment or activate membership.',
            member=member,path='/portal/renewals/')
        return row
