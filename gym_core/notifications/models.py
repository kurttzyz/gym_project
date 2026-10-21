import uuid
from django.db import models


class NotificationSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    email_enabled = models.BooleanField(default=False)
    public_origin = models.URLField(blank=True, help_text='Your verified gym website origin, for example https://gym.example.com.')
    owner_email = models.EmailField(blank=True, help_text='Inbox for gym owner alerts. Leave empty to disable owner alerts.')
    bookings = models.BooleanField(default=True)
    membership_reminders = models.BooleanField(default=True)
    payment_confirmations = models.BooleanField(default=True)
    owner_alerts = models.BooleanField(default=True)
    class_reminders = models.BooleanField(default=True)
    last_worker_check = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='one_notification_settings_per_gym')]


class EmailNotification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    deduplication_key = models.CharField(max_length=220, unique=True, editable=False)
    category = models.CharField(max_length=32)
    module = models.CharField(max_length=20, default='memberships')
    member = models.ForeignKey('gym_core.MemberProfile', null=True, blank=True, on_delete=models.PROTECT)
    recipient = models.EmailField()
    sender_name = models.CharField(max_length=200)
    reply_to = models.EmailField(blank=True)
    subject = models.CharField(max_length=255)
    text_body = models.TextField()
    html_body = models.TextField()
    source_kind = models.CharField(max_length=20, blank=True)
    source_id = models.UUIDField(null=True, blank=True)
    source_version = models.CharField(max_length=64, blank=True)
    expected_status = models.CharField(max_length=12, blank=True)
    origin = models.URLField(blank=True)
    status = models.CharField(max_length=12, default='queued', choices=[('queued','Queued'),('sending','Sending'),
        ('sent','Sent to mail server'),('failed','Failed'),('skipped','Skipped')])
    attempts = models.PositiveSmallIntegerField(default=0)
    attempt_limit = models.PositiveSmallIntegerField(default=5)
    available_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    lease_until = models.DateTimeField(null=True, blank=True)
    claim_token = models.UUIDField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', 'id']
        indexes = [models.Index(fields=['status','available_at'], name='email_pending_idx')]


class EmailAttempt(models.Model):
    notification = models.ForeignKey(EmailNotification, on_delete=models.PROTECT, related_name='delivery_attempts')
    number = models.PositiveSmallIntegerField()
    outcome = models.CharField(max_length=80)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ['-started_at', '-id']
        constraints = [models.UniqueConstraint(fields=['notification','number'], name='unique_email_attempt')]
