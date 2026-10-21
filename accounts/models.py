import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Platform identity; organization access is granted through memberships."""


class PasswordRecovery(models.Model):
    """Security outbox kept central; tenant owners never receive reset tokens."""
    id = models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    user = models.ForeignKey('accounts.User',on_delete=models.CASCADE)
    gym = models.ForeignKey('gyms.Organization',on_delete=models.CASCADE)
    recipient = models.EmailField()
    origin = models.URLField()
    password_fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=12,default='queued')
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField()
    lease_until = models.DateTimeField(null=True)
    claim_token = models.UUIDField(null=True)
    outcome = models.CharField(max_length=80,blank=True)

    class Meta:
        indexes = [models.Index(fields=['status','available_at'],name='recovery_pending_idx')]


class RecoveryThrottle(models.Model):
    bucket = models.CharField(max_length=64,primary_key=True)
    count = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField()
