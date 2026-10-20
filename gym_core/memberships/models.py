import uuid
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Lower


class MembershipPlan(models.Model):
    class Period(models.TextChoices):
        DAY = "day", "day pass"
        MONTH = "month", "month"
        QUARTER = "quarter", "3 months"
        YEAR = "year", "year"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    description = models.TextField(max_length=1200, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    period = models.CharField(max_length=10, choices=Period.choices, default=Period.MONTH)
    features = models.TextField(max_length=2000, blank=True, help_text="One benefit per line.")
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "price", "name", "id"]
        constraints = [models.CheckConstraint(condition=models.Q(price__gte=0), name="membership_plan_price_nonnegative")]

    @property
    def feature_list(self):
        return [line.strip() for line in self.features.splitlines() if line.strip()]

    def __str__(self):
        return self.name


class MembershipApplication(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        CONTACTED = "contacted", "Contacted"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(MembershipPlan, on_delete=models.PROTECT, related_name="applications")
    full_name = models.CharField(max_length=160)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    message = models.TextField(max_length=1200, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NEW)
    quoted_plan_name = models.CharField(max_length=100, editable=False)
    quoted_price = models.DecimalField(max_digits=10, decimal_places=2, editable=False)
    quoted_period = models.CharField(max_length=10, editable=False)
    owner_notes = models.TextField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [models.UniqueConstraint(
            Lower("email"), "plan", condition=models.Q(status__in=["new", "contacted"]),
            name="unique_pending_membership_application",
        )]
