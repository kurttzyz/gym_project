import uuid
from django.db import models


class PaymentOrder(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Awaiting checkout"
        SUBMITTING = "submitting", "Checking with COPAY"
        PENDING = "pending", "Awaiting payment"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.OneToOneField("gym_core.MemberSubscription", on_delete=models.PROTECT, related_name="payment")
    merchant_reference = models.CharField(max_length=40, unique=True)
    merchant_code = models.CharField(max_length=100)
    environment = models.CharField(max_length=10)
    payment_method = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    net_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="PHP")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.CREATED)
    provider_reference = models.CharField(max_length=100, null=True, blank=True, unique=True)
    checkout_url = models.URLField(max_length=2000, blank=True)
    provider_status = models.IntegerField(null=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    access_note = models.CharField(max_length=300, blank=True)
    central_collection = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_positive_amount")]

    @property
    def receipt_number(self):
        return "PAY-" + self.id.hex.upper()
