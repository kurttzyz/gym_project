from uuid import uuid4
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from gyms.databases import ready_database
from gym_core.memberships.views import owner_page
from gym_core.classes.views import member_page
from .models import NotificationSettings, EmailNotification
from .forms import NotificationSettingsForm, MemberEmailForm
from .services import queue_email
from .delivery import transport_ready


@owner_page
@never_cache
def settings_page(request):
    config,_ = NotificationSettings.objects.get_or_create(pk=1)
    form = NotificationSettingsForm(request.POST if request.method == 'POST' else None,instance=config,gym=request.tenant)
    if request.method == 'POST' and form.is_valid():
        config = form.save(commit=False)
        config.save(update_fields=form._meta.fields)
        messages.success(request,'Notification settings saved.')
        return redirect('owner-notifications')
    history = EmailNotification.objects.all()
    status = request.GET.get('status','')
    if status in dict(EmailNotification._meta.get_field('status').choices):
        history = history.filter(status=status)
    return render(request,'notifications/settings.html',{'form':form,'config':config,'transport_ready':transport_ready(),
        'page':Paginator(history,25).get_page(request.GET.get('page')),'status':status,
        'statuses':EmailNotification._meta.get_field('status').choices})


@owner_page
@never_cache
def detail(request,notification_id):
    row = get_object_or_404(EmailNotification,pk=notification_id)
    return render(request,'notifications/detail.html',{'notification':row,'attempts':row.delivery_attempts.all(),
        'retry_allowed':row.status == 'failed' and row.expires_at > timezone.now() and row.attempts < 100})


@owner_page
@never_cache
@require_POST
def retry(request,notification_id):
    with transaction.atomic(using=ready_database(request.tenant)):
        row = get_object_or_404(EmailNotification.objects.select_for_update(),pk=notification_id)
        if row.status != 'failed' or row.expires_at <= timezone.now() or row.attempts >= 100:
            messages.error(request,'Only unexpired failed messages below the retry limit can be retried.')
        else:
            row.status, row.outcome, row.available_at = 'queued','owner_requested_retry',timezone.now()
            row.attempt_limit = min(100,row.attempts+5)
            row.save(update_fields=['status','outcome','available_at','attempt_limit'])
            messages.success(request,'Retry queued. Delivery will recheck current eligibility and preferences.')
    return redirect('notification-detail',notification_id=notification_id)


@owner_page
@never_cache
@require_POST
def test_email(request):
    row = queue_email(ready_database(request.tenant),key=f'owner-test:{uuid4()}',category='owner_alerts',
        title='Your gym email setup',message='This is a test of your gym’s branded email notifications.',path='/manage/notifications/')
    if row:
        messages.success(request,'Test queued for the configured owner inbox. Check delivery history after the next worker tick.')
    else:
        messages.error(request,'Enable gym email and owner alerts, and save an owner inbox before queuing a test.')
    return redirect('owner-notifications')


@member_page
def preferences(request):
    form = MemberEmailForm(request.POST if request.method == 'POST' else None,instance=request.class_member)
    if request.method == 'POST' and form.is_valid():
        profile = form.save(commit=False)
        profile.save(update_fields=['email_notifications'])
        messages.success(request,'Email preferences saved.')
        return redirect('member-email-preferences')
    return render(request,'notifications/preferences.html',{'form':form})
