import uuid
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models.functions import Lower


class Product(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sku = models.CharField(max_length=50)
    name = models.CharField(max_length=160)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    low_stock_threshold = models.PositiveIntegerField(default=5, validators=[MaxValueValidator(1000000)])
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sku", "id"]
        constraints = [models.UniqueConstraint(Lower("sku"), name="unique_gym_product_sku"),
                       models.CheckConstraint(condition=models.Q(price__gte=0), name="product_nonnegative_price")]

    def __str__(self):
        return f"{self.sku} — {self.name} · PHP {self.price:.2f}"


class StockBalance(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="balances")
    location = models.ForeignKey("tenant_data.Location", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["product", "location"], name="one_product_balance_per_branch")]


class Sale(models.Model):
    id = models.UUIDField(primary_key=True, editable=False)
    fingerprint = models.CharField(max_length=64)
    location = models.ForeignKey("tenant_data.Location", on_delete=models.PROTECT)
    branch_name = models.CharField(max_length=200)
    branch_address = models.TextField(blank=True)
    gym_name = models.CharField(max_length=200)
    actor_id = models.PositiveBigIntegerField()
    total = models.DecimalField(max_digits=16, decimal_places=2)
    method = models.CharField(max_length=4, choices=[("cash", "Cash"), ("bank", "Verified bank transfer")])
    tendered = models.DecimalField(max_digits=16, decimal_places=2)
    change = models.DecimalField(max_digits=16, decimal_places=2)
    external_reference = models.CharField(max_length=120, blank=True)
    verification_note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(total__gte=0), name="pos_sale_total_nonnegative"),
            models.CheckConstraint(condition=models.Q(tendered__gte=models.F("total")), name="pos_sale_sufficient_tender"),
            models.CheckConstraint(condition=models.Q(change__gte=0), name="pos_change_nonnegative"),
            models.UniqueConstraint(Lower("external_reference"), condition=~models.Q(external_reference=""), name="unique_pos_transfer_reference"),
        ]

    @property
    def number(self):
        return "POS-" + self.id.hex.upper()


class SaleLine(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    sku = models.CharField(max_length=50)
    name = models.CharField(max_length=160)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["sku", "id"]
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gt=0), name="sale_line_positive_quantity"),
                       models.UniqueConstraint(fields=["sale", "product"], name="unique_product_per_sale")]


class SaleRefund(models.Model):
    sale = models.OneToOneField(Sale, on_delete=models.PROTECT, related_name="refund")
    actor_id = models.PositiveBigIntegerField()
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    reference = models.CharField(max_length=120)
    note = models.CharField(max_length=300)
    restocked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("reference"), name="unique_pos_refund_reference")]


class StockMovement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    balance = models.ForeignKey(StockBalance, on_delete=models.PROTECT, related_name="movements")
    delta = models.IntegerField()
    balance_after = models.PositiveIntegerField()
    kind = models.CharField(max_length=10, choices=[("adjustment", "Adjustment"), ("sale", "Sale"), ("refund", "Refund")])
    actor_id = models.PositiveBigIntegerField()
    note = models.CharField(max_length=500)
    fingerprint = models.CharField(max_length=64, blank=True)
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [models.CheckConstraint(condition=~models.Q(delta=0), name="stock_movement_nonzero"),
                       models.UniqueConstraint(fields=["balance", "sale", "kind"], condition=models.Q(sale__isnull=False), name="unique_sale_stock_movement")]
