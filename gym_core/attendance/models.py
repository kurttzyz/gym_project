import uuid
from django.db import models


class AttendanceVisit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    member = models.ForeignKey("gym_core.MemberProfile", on_delete=models.PROTECT, related_name="visits")
    subscription = models.ForeignKey("gym_core.MemberSubscription", on_delete=models.PROTECT)
    location = models.ForeignKey("tenant_data.Location", on_delete=models.PROTECT)
    checked_in_at = models.DateTimeField(auto_now_add=True)
    checked_in_by_id = models.PositiveBigIntegerField()
    checked_out_at = models.DateTimeField(null=True, blank=True)
    checked_out_by_id = models.PositiveBigIntegerField(null=True, blank=True)
    checkout_note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-checked_in_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["member"], condition=models.Q(checked_out_at__isnull=True), name="one_open_gym_visit_per_member"),
            models.CheckConstraint(condition=models.Q(checked_out_at__isnull=True) | models.Q(checked_out_at__gte=models.F("checked_in_at")), name="attendance_checkout_after_entry"),
        ]


class AttendanceScan(models.Model):
    """Immutable scan result and replay boundary. Never store raw QR credentials."""
    id = models.UUIDField(primary_key=True, editable=False)
    fingerprint = models.CharField(max_length=64)
    actor_id = models.PositiveBigIntegerField()
    location = models.ForeignKey("tenant_data.Location", on_delete=models.PROTECT)
    action = models.CharField(max_length=10)
    outcome = models.CharField(max_length=15)
    reason = models.CharField(max_length=200)
    member = models.ForeignKey("gym_core.MemberProfile", null=True, blank=True, on_delete=models.PROTECT)
    visit = models.ForeignKey(AttendanceVisit, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]
