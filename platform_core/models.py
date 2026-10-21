import uuid
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.conf import settings


class SaaSPlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    period = models.CharField(max_length=5, choices=[("month", "Monthly"), ("year", "Yearly")], default="month")
    websites = models.BooleanField(default=True)
    memberships = models.BooleanField(default=True)
    attendance = models.BooleanField(default=True)
    payments = models.BooleanField(default=True)
    pos = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["price", "name"]
        constraints = [models.CheckConstraint(condition=models.Q(price__gte=0), name="saas_plan_price_nonnegative")]

    @property
    def modules(self):
        return {key: getattr(self, key) for key in ("websites", "memberships", "attendance", "payments", "pos")}

    def __str__(self):
        return self.name


class GymSubscription(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField("gyms.Organization", on_delete=models.PROTECT, related_name="saas_subscription")
    plan = models.ForeignKey(SaaSPlan, on_delete=models.PROTECT)
    plan_name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    period = models.CharField(max_length=5)
    modules = models.JSONField()
    trial_modules = models.JSONField()
    started_on = models.DateField()
    trial_ends_on = models.DateField()
    billing_anchor_day = models.PositiveSmallIntegerField()
    grace_days = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(30)])
    suspended_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class PlatformInvoice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(GymSubscription, on_delete=models.PROTECT, related_name="invoices")
    customer_name = models.CharField(max_length=200)
    plan_name = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="PHP")
    period = models.CharField(max_length=5)
    modules = models.JSONField()
    period_start = models.DateField()
    period_end = models.DateField()
    due_on = models.DateField()
    status = models.CharField(max_length=6, choices=[("issued", "Issued"), ("paid", "Paid"), ("void", "Void")], default="issued")
    paid_at = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start", "-issued_at", "id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gte=0), name="platform_invoice_nonnegative"),
            models.CheckConstraint(condition=models.Q(period_end__gt=models.F("period_start")), name="platform_invoice_positive_term"),
            models.UniqueConstraint(fields=["subscription"], condition=models.Q(status="issued"), name="one_open_platform_invoice"),
            models.UniqueConstraint(fields=["subscription", "period_start"], condition=~models.Q(status="void"), name="unique_platform_invoice_period"),
        ]

    @property
    def number(self):
        return "PH-" + self.id.hex.upper()


class PlatformPayment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.OneToOneField(PlatformInvoice, on_delete=models.PROTECT, related_name="payment_record")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reference = models.CharField(max_length=120)
    method = models.CharField(max_length=16, choices=[("bank", "Bank transfer"), ("cash", "Cash"), ("other", "Other"), ("complimentary", "No charge")])
    note = models.CharField(max_length=500)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("reference"), name="unique_platform_payment_reference")]


class SubscriptionEvent(models.Model):
    subscription = models.ForeignKey(GymSubscription, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=20)
    note = models.CharField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class GymWallet(models.Model):
    organization = models.ForeignKey('gyms.Organization', on_delete=models.PROTECT)
    environment = models.CharField(max_length=10)
    pending = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    available = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    reserved = models.DecimalField(max_digits=16, decimal_places=2, default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'environment'], name='unique_gym_wallet'),
            models.CheckConstraint(condition=models.Q(pending__gte=0, available__gte=0, reserved__gte=0), name='wallet_balances_nonnegative')]


class CentralCollection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('gyms.Organization', on_delete=models.PROTECT)
    tenant_order_id = models.UUIDField()
    merchant_reference = models.CharField(max_length=40, unique=True)
    merchant_code = models.CharField(max_length=100)
    environment = models.CharField(max_length=10)
    payment_method = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='PHP')
    status = models.CharField(max_length=12, default='submitting')
    provider_reference = models.CharField(max_length=100, null=True, blank=True, unique=True)
    net_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    authenticated_result = models.JSONField(default=dict, editable=False)
    tenant_synced = models.BooleanField(default=False)
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'tenant_order_id'], name='unique_central_tenant_order')]


class PayoutDestination(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey('gyms.Organization', on_delete=models.PROTECT)
    bank = models.CharField(max_length=100)
    account_name = models.CharField(max_length=150)
    account_number = models.CharField(max_length=80)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='+')
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_note = models.CharField(max_length=500, blank=True)
    is_active = models.BooleanField(default=True)


class GymWithdrawal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(GymWallet, on_delete=models.PROTECT)
    destination = models.ForeignKey(PayoutDestination, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    request_key = models.UUIDField(unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    bank = models.CharField(max_length=100)
    account_name = models.CharField(max_length=150)
    account_number = models.CharField(max_length=80)
    status = models.CharField(max_length=12, default='requested', choices=[(key,key.title()) for key in ['requested','approved','processing','paid','rejected','cancelled']])
    payout_reference = models.CharField(max_length=120, null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='+')
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name='withdrawal_positive'),
            models.UniqueConstraint(Lower('payout_reference'), name='unique_gym_payout_reference')]
        ordering = ['-created_at']


class WalletEntry(models.Model):
    wallet = models.ForeignKey(GymWallet, on_delete=models.PROTECT, related_name='entries')
    key = models.CharField(max_length=150, unique=True)
    action = models.CharField(max_length=20)
    pending_delta = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    available_delta = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    reserved_delta = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    note = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
