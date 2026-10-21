import uuid
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


def member_number():
    return "M-" + uuid.uuid4().hex.upper()


class MemberProfile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    qr_version = models.UUIDField(default=uuid.uuid4, editable=False)
    member_number = models.CharField(max_length=34, unique=True, default=member_number, editable=False)
    user_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True, editable=False)
    full_name = models.CharField(max_length=160)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    date_of_birth = models.DateField(null=True, blank=True)
    emergency_contact_name = models.CharField(max_length=160, blank=True)
    emergency_contact_phone = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)
    email_notifications = models.BooleanField(default=True)
    waiver_text = models.TextField(blank=True, editable=False)
    waiver_accepted_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["full_name", "id"]
        constraints = [models.UniqueConstraint(Lower("email"), name="unique_gym_member_email")]

    def __str__(self):
        return self.full_name


class MemberInvitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    member = models.ForeignKey(MemberProfile, on_delete=models.CASCADE, related_name="invitations")
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by_id = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def available(self):
        return not self.used_at and not self.revoked_at and self.expires_at > timezone.now()
