import re
import hashlib
import hmac
import smtplib
import uuid
from datetime import timedelta
from email.utils import formataddr
from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from config.database_router import gym_database
from gyms.models import Membership
from gyms.databases import ready_database
from gym_core.members.models import MemberProfile
from gym_core.notifications.delivery import transport_ready
from gym_core.notifications.services import validated_origin
from websites.models import WebsiteSettings
from .models import PasswordRecovery,RecoveryThrottle


class RecoveryForm(forms.Form):
    username = forms.CharField(max_length=150)
    email = forms.EmailField(label='Login account email')


def fingerprint(user):
    return hashlib.sha256(user.password.encode()).hexdigest()


def token_generator(gym):
    generator=PasswordResetTokenGenerator()
    generator.key_salt='powerhouse.member-recovery.'+str(gym.database_key)
    return generator


def allowed_member(gym,user,alias):
    if not user.is_active or user.is_staff or user.is_superuser or not user.has_usable_password():
        return False
    if not Membership.objects.filter(organization=gym,user=user,role='member',is_active=True).exists():
        return False
    with gym_database(alias):
        return MemberProfile.objects.filter(user_id=user.pk,is_active=True).exists()


def queue_recovery(gym,alias,username,email,origin,remote_address):
    now=timezone.now()
    # Hash IP buckets with an application secret; don't retain raw client IPs.
    window=int(now.timestamp())//600
    bucket=hmac.new(settings.SECRET_KEY.encode(),f'{gym.database_key}:{remote_address}:{window}'.encode(),hashlib.sha256).hexdigest()
    with transaction.atomic(using='default'):
        throttle,_=RecoveryThrottle.objects.get_or_create(bucket=bucket,defaults={'expires_at':now+timedelta(minutes=20)})
        throttle=RecoveryThrottle.objects.select_for_update().get(pk=bucket)
        if throttle.count>=10:
            return
        throttle.count+=1
        throttle.save(update_fields=['count'])
        user=get_user_model().objects.select_for_update().filter(username=username,email__iexact=email,is_active=True).first()
        if not user or not allowed_member(gym,user,alias):
            return
        if PasswordRecovery.objects.filter(user=user,created_at__gt=now-timedelta(minutes=5)).exists():
            return
        # The destination is the central identity address, never a gym-editable
        # member profile address. Disable old requests only once new one exists.
        PasswordRecovery.objects.filter(user=user,used_at__isnull=True,status__in=['queued','sending','sent']).update(status='superseded')
        PasswordRecovery.objects.create(user=user,gym=gym,recipient=user.email,origin=validated_origin(gym,origin),
            password_fingerprint=fingerprint(user),expires_at=now+timedelta(minutes=30),available_at=now)


def valid_request(row,gym,user,alias,token=None):
    if (row.gym_id!=gym.pk or row.used_at or row.expires_at<=timezone.now() or row.status not in {'queued','sending','sent'}
            or row.recipient.casefold()!=user.email.casefold() or row.password_fingerprint!=fingerprint(user)
            or not allowed_member(gym,user,alias)):
        return False
    try:validated_origin(gym,row.origin)
    except forms.ValidationError:return False
    return token_generator(gym).check_token(user,token) if token is not None else True


def deliver_recovery(request_id):
    """Generate reset tokens only in worker memory; never store them in a gym DB."""
    if not transport_ready():
        return
    now=timezone.now()
    with transaction.atomic(using='default'):
        row=PasswordRecovery.objects.select_for_update(of=('self',)).select_related('gym','user').filter(pk=request_id).first()
        if not row or row.status not in {'queued','sending'} or row.available_at>now:
            return
        if row.status=='sending' and row.lease_until and row.lease_until>now:
            return
        alias=ready_database(row.gym)
        if not valid_request(row,row.gym,row.user,alias):
            row.status,row.outcome='skipped','account_or_request_unavailable'
            row.save(update_fields=['status','outcome'])
            return
        if row.attempts>=3:
            row.status,row.outcome='failed','attempt_limit_reached'
            row.save(update_fields=['status','outcome'])
            return
        token=uuid.uuid4()
        row.status,row.claim_token,row.lease_until='sending',token,now+timedelta(minutes=5)
        row.attempts+=1
        row.save(update_fields=['status','claim_token','lease_until','attempts'])
        with gym_database(alias):
            site=WebsiteSettings.objects.get(pk=1)
        reset_token=token_generator(row.gym).make_token(row.user)
        url=f'{row.origin}/accounts/reset/{row.pk}/{reset_token}/'
        name=site.display_name.replace('\r',' ').replace('\n',' ')[:200]
        context={'gym_name':name,'color':site.primary_color if re.fullmatch(r'#[0-9a-fA-F]{6}',site.primary_color) else '#245ac5','title':'Reset your login password',
            'message':'You requested a password reset. This link expires 30 minutes after your request. Changing this password affects every gym using this same login account. If you did not request this, ignore this email.',
            'url':url,'link_label':'Reset login password'}
        message=EmailMultiAlternatives(f'{name}: Reset your login password',render_to_string('notifications/email.txt',context),
            formataddr((name,settings.DEFAULT_FROM_EMAIL)),[row.recipient],headers={'Auto-Submitted':'auto-generated'})
        message.attach_alternative(render_to_string('notifications/email.html',context),'text/html')
    try:
        if message.send(fail_silently=False)!=1:raise RuntimeError('not_accepted')
        outcome='accepted_by_mail_server'
    except Exception as error:
        outcome='recipient_rejected' if isinstance(error,smtplib.SMTPRecipientsRefused) else 'transport_error'
    with transaction.atomic(using='default'):
        current=PasswordRecovery.objects.select_for_update().get(pk=row.pk)
        if current.claim_token!=token or current.status!='sending':return
        current.status='sent' if outcome=='accepted_by_mail_server' else 'failed' if current.attempts>=3 or outcome=='recipient_rejected' else 'queued'
        current.outcome,current.claim_token,current.lease_until=outcome,None,None
        current.available_at=timezone.now()+timedelta(minutes=2**current.attempts)
        current.save(update_fields=['status','outcome','claim_token','lease_until','available_at'])
