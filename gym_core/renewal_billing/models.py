import uuid
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator


class RenewalBillingSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    enabled = models.BooleanField(default=False)
    lead_days = models.PositiveSmallIntegerField(
        default=7, validators=[MinValueValidator(1), MaxValueValidator(30)]
    )
    late_policy = models.CharField(
        max_length=10,
        default="restart",
        choices=[
            ("restart", "Restart from confirmed payment date"),
            ("cycle", "Keep original billing cycle"),
        ],
    )
    last_worker_check = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="one_renewal_billing_settings"),
            models.CheckConstraint(
                condition=models.Q(lead_days__gte=1, lead_days__lte=30),
                name="renewal_lead_days_range",
            ),
        ]


class RenewalPreference(models.Model):
    member = models.OneToOneField(
        "gym_core.MemberProfile", on_delete=models.PROTECT, related_name="renewal_preference"
    )
    enabled = models.BooleanField(default=False)
    plan = models.ForeignKey("gym_core.MembershipPlan", on_delete=models.PROTECT)
    plan_name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    period = models.CharField(max_length=10)
    late_policy = models.CharField(max_length=10)
    consented_at = models.DateTimeField()
    revision = models.PositiveIntegerField(default=1)
    last_note = models.CharField(max_length=300, blank=True)


class RenewalConsentEvent(models.Model):
    member = models.ForeignKey("gym_core.MemberProfile", on_delete=models.PROTECT)
    actor_id = models.PositiveBigIntegerField()
    action = models.CharField(max_length=10)
    snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)


class RenewalInvoice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    member = models.ForeignKey(
        "gym_core.MemberProfile", on_delete=models.PROTECT, related_name="renewal_invoices"
    )
    previous_subscription = models.OneToOneField(
        "gym_core.MemberSubscription", on_delete=models.PROTECT, related_name="next_renewal_invoice"
    )
    subscription = models.OneToOneField(
        "gym_core.MemberSubscription", on_delete=models.PROTECT, related_name="renewal_invoice"
    )
    due_on = models.DateField()
    late_policy = models.CharField(max_length=10)
    consent_revision = models.PositiveIntegerField()
    status = models.CharField(
        max_length=12,
        default="issued",
        choices=[
            ("issued", "Awaiting payment"),
            ("paid", "Paid"),
            ("cancelled", "Cancelled"),
            ("review", "Owner review required"),
            ("sandbox", "Sandbox payment"),
            ("fulfilled", "Activated manually"),
        ],
    )
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]

    @property
    def number(self):
        return "REN-" + self.id.hex.upper()
