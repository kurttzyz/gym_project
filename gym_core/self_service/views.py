from django.contrib import messages
from django.contrib.auth import update_session_auth_hash, get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404,redirect,render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST
from django.db import transaction
from gyms.databases import ready_database
from gym_core.classes.views import member_page
from gym_core.memberships.views import owner_page
from gym_core.members.services import EnrollmentError
from gym_core.memberships.models import MembershipPlan
from payments.models import PaymentOrder
from .forms import SelfProfileForm,RenewalForm,RenewalReviewForm
from .models import RenewalRequest
from .services import update_profile,request_renewal,cancel_renewal,review_renewal,suggested_start


@member_page
def profile(request):
    form = SelfProfileForm(request.POST if request.method=='POST' else None,instance=request.class_member)
    if request.method=='POST' and form.is_valid():
        try:update_profile(ready_database(request.tenant),request.class_member.pk,request.user.pk,form.cleaned_data)
        except EnrollmentError as error:form.add_error(None,str(error))
        else:
            messages.success(request,'Your profile was updated.')
            return redirect('member-profile')
    return render(request,'self_service/profile.html',{'form':form,'profile':request.class_member})


@member_page
def renewals(request):
    form = RenewalForm(request.POST if request.method=='POST' else None,initial={'starts_on':suggested_start(request.class_member)})
    if request.method=='POST' and form.is_valid():
        try:
            request_renewal(ready_database(request.tenant),request.class_member.pk,request.user.pk,
                form.cleaned_data['plan'].pk,form.cleaned_data['starts_on'],form.cleaned_data['note'])
        except (EnrollmentError,MembershipPlan.DoesNotExist) as error:
            form.add_error(None,str(error) if isinstance(error,EnrollmentError) else 'This plan is no longer available.')
        else:
            messages.success(request,'Renewal request sent to your gym for review. No payment was collected.')
            return redirect('member-renewals')
    rows = request.class_member.renewal_requests.select_related('subscription__payment')
    return render(request,'self_service/renewals.html',{'form':form,'plans':MembershipPlan.objects.filter(is_active=True),
        'page':Paginator(rows,25).get_page(request.GET.get('page'))})


@member_page
@require_POST
def withdraw_renewal(request,request_id):
    get_object_or_404(RenewalRequest,pk=request_id,member=request.class_member)
    try:cancel_renewal(ready_database(request.tenant),request_id,request.class_member.pk,request.user.pk)
    except EnrollmentError as error:messages.error(request,str(error))
    else:messages.success(request,'Renewal request cancelled.')
    return redirect('member-renewals')


@owner_page
@never_cache
def owner_renewals(request):
    page=Paginator(RenewalRequest.objects.select_related('member','subscription','plan'),25).get_page(request.GET.get('page'))
    for row in page:
        row.review_form=RenewalReviewForm(prefix=str(row.pk))
    return render(request,'self_service/owner_renewals.html',{'page':page})


@owner_page
@never_cache
@require_POST
def review(request,request_id):
    get_object_or_404(RenewalRequest,pk=request_id)
    form = RenewalReviewForm(request.POST,prefix=str(request_id))
    if form.is_valid():
        try:row=review_renewal(ready_database(request.tenant),request_id,form.cleaned_data['action'],form.cleaned_data['note'],request.user.pk)
        except EnrollmentError as error:messages.error(request,str(error))
        else:messages.success(request,'Request '+row.status+'. Approved terms still need payment verification and activation.')
    else:messages.error(request,'Choose an action and provide a note of up to 500 characters.')
    return redirect('owner-renewals')


@member_page
def payment_history(request):
    rows = PaymentOrder.objects.filter(subscription__member=request.class_member).select_related('subscription')
    return render(request,'self_service/payments.html',{'page':Paginator(rows.order_by('-created_at','id'),25).get_page(request.GET.get('page'))})


@member_page
@sensitive_post_parameters('old_password','new_password1','new_password2')
def password_change(request):
    form = PasswordChangeForm(request.user,request.POST if request.method=='POST' else None)
    if request.method=='POST' and form.is_valid():
        # The account is central: lock/reload to prevent concurrent resets from
        # replacing credentials after old-password validation.
        with transaction.atomic(using='default'):
            user=get_user_model().objects.select_for_update().get(pk=request.user.pk)
            fresh=PasswordChangeForm(user,request.POST)
            if fresh.is_valid():
                user=fresh.save()
                update_session_auth_hash(request,user)
                messages.success(request,'Password changed for your shared login account.')
                return redirect('member-password-change')
            form=fresh
    return render(request,'self_service/password_change.html',{'form':form})
