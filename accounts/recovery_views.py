from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import SetPasswordForm
from django.core.exceptions import ValidationError
from django.db import DatabaseError,transaction
from django.http import Http404
from django.shortcuts import redirect,render,get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from gyms.databases import ready_database,GymDatabaseUnavailable
from .models import PasswordRecovery
from .recovery import RecoveryForm,queue_recovery,valid_request


def private_response(response, *, form=False):
    # no-referrer makes Chromium submit forms with Origin: null.
    response['Referrer-Policy']='same-origin' if form else 'no-referrer'
    response['X-Robots-Tag']='noindex, nofollow'
    return response


@never_cache
@sensitive_post_parameters('email','username')
def request_reset(request):
    form=RecoveryForm(request.POST if request.method=='POST' else None)
    if request.method=='POST' and form.is_valid():
        try:
            origin=request.build_absolute_uri('/').rstrip('/') if settings.DEBUG else 'https://'+request.get_host()
            queue_recovery(request.tenant,ready_database(request.tenant),form.cleaned_data['username'],
                form.cleaned_data['email'],origin,request.META.get('REMOTE_ADDR',''))
        except (DatabaseError,GymDatabaseUnavailable,ValidationError,OSError):
            # Same response regardless of account existence, throttle or DB state.
            pass
        return redirect('member-reset-requested')
    return private_response(render(request,'self_service/reset_request.html',{'form':form}),form=True)


@never_cache
def requested(request):
    return private_response(render(request,'self_service/reset_requested.html'))


@never_cache
@sensitive_post_parameters('new_password1','new_password2')
def confirm_reset(request,recovery_id,token):
    # Replace the URL token with a session value before rendering any HTML.
    session_key=f'member-reset:{request.tenant.database_key}:{recovery_id}'
    try:
        alias=ready_database(request.tenant)
        row=get_object_or_404(PasswordRecovery.objects.select_related('user'),pk=recovery_id,gym=request.tenant)
        candidate=request.session.get(session_key) if token=='set-password' else token
        valid=bool(candidate and valid_request(row,request.tenant,row.user,alias,candidate))
        if valid and token!='set-password':
            request.session[session_key]=candidate
            return private_response(redirect('member-reset-confirm',recovery_id=recovery_id,token='set-password'))
        form=SetPasswordForm(row.user,request.POST if request.method=='POST' and valid else None) if valid else None
        if valid and request.method=='POST' and form.is_valid():
            with transaction.atomic(using='default'):
                # Match the request queue lock order (user then request) and
                # recheck under locks so a reset is single-use under concurrency.
                user=get_user_model().objects.select_for_update().get(pk=row.user_id)
                locked=PasswordRecovery.objects.select_for_update().get(pk=row.pk)
                if not valid_request(locked,request.tenant,user,alias,candidate):
                    valid,form=False,None
                else:
                    fresh=SetPasswordForm(user,request.POST)
                    if fresh.is_valid():
                        user.set_password(fresh.cleaned_data['new_password1'])
                        user.save(update_fields=['password'])
                        from django.utils import timezone
                        locked.used_at,locked.status=timezone.now(),'used'
                        locked.save(update_fields=['used_at','status'])
                        PasswordRecovery.objects.filter(user=user,used_at__isnull=True).exclude(pk=locked.pk).update(status='superseded')
                        request.session.pop(session_key,None)
                        return private_response(redirect('member-reset-complete'))
                    form=fresh
    except (DatabaseError,GymDatabaseUnavailable,OSError):
        valid,form=False,None
    return private_response(render(request,'self_service/reset_confirm.html',{'valid':valid,'form':form}),form=True)


@never_cache
def complete(request):
    return private_response(render(request,'self_service/reset_complete.html'))
