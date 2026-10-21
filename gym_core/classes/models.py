import uuid
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


class Trainer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160)
    specialty = models.CharField(max_length=200, blank=True)
    bio = models.TextField(max_length=2000, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name', 'id']

    def __str__(self):
        return self.name


class ClassSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=160)
    description = models.TextField(max_length=2000, blank=True)
    trainer = models.ForeignKey(Trainer, on_delete=models.PROTECT, related_name='sessions')
    location = models.ForeignKey('tenant_data.Location', on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    capacity = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(500)])
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=500, blank=True)
    created_by_id = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['starts_at', 'id']
        constraints = [models.CheckConstraint(condition=models.Q(ends_at__gt=models.F('starts_at')), name='class_positive_duration'),
                       models.CheckConstraint(condition=models.Q(capacity__gte=1, capacity__lte=500), name='class_valid_capacity')]


class ClassBooking(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(ClassSession, on_delete=models.PROTECT, related_name='bookings')
    member = models.ForeignKey('gym_core.MemberProfile', on_delete=models.PROTECT, related_name='class_bookings')
    status = models.CharField(max_length=12, choices=[('confirmed', 'Confirmed'), ('waitlisted', 'Waitlisted'), ('cancelled', 'Cancelled')])
    queued_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['queued_at', 'id']
        constraints = [models.UniqueConstraint(fields=['session', 'member'], name='unique_class_member')]


class BookingEvent(models.Model):
    booking = models.ForeignKey(ClassBooking, on_delete=models.PROTECT, related_name='events')
    action = models.CharField(max_length=30)
    actor_id = models.PositiveBigIntegerField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
