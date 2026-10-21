import uuid
from django import forms
from django.forms import formset_factory
from tenant_data.models import Location
from .models import Product


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ("sku", "name", "price", "low_stock_threshold", "is_active")
        labels = {"price": "Selling price (PHP)", "is_active": "Available for sale"}

    def clean_sku(self):
        return self.cleaned_data["sku"].strip().upper()


class BranchForm(forms.Form):
    location = forms.ModelChoiceField(queryset=Location.objects.none(), label="Branch")

    def __init__(self, *args, **kwargs):
        active_only = kwargs.pop("active_only", False)
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = Location.objects.filter(is_active=True) if active_only else Location.objects.all()


class StockAdjustmentForm(BranchForm):
    product = forms.ModelChoiceField(queryset=Product.objects.none())
    delta = forms.IntegerField(min_value=-1000000, max_value=1000000, label="Quantity change (+ receive, − remove)")
    note = forms.CharField(max_length=300, label="Reason / receiving reference", widget=forms.Textarea(attrs={"rows": 2}))
    request_id = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.all()

    def clean_delta(self):
        value = self.cleaned_data["delta"]
        if value == 0:
            raise forms.ValidationError("Enter a nonzero quantity change.")
        return value


class CartLineForm(forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.none())
    quantity = forms.IntegerField(min_value=1, max_value=999)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.filter(is_active=True)


CartFormSet = formset_factory(CartLineForm, extra=5, max_num=20, validate_max=True, absolute_max=20)


class PaymentForm(forms.Form):
    quote = forms.CharField(max_length=65536, widget=forms.HiddenInput)
    method = forms.ChoiceField(choices=[("cash", "Cash"), ("bank", "Verified external bank transfer")])
    tendered = forms.DecimalField(max_digits=16, decimal_places=2, min_value=0, required=False, label="Cash received (PHP)")
    reference = forms.CharField(max_length=120, required=False, label="Bank transfer reference")
    note = forms.CharField(max_length=300, required=False, label="Bank verification note", widget=forms.Textarea(attrs={"rows": 2}))
    confirmed = forms.BooleanField(required=False, label="I verified that this bank transfer was received.")

    def clean(self):
        data = super().clean()
        if data.get("method") == "cash" and data.get("tendered") is None:
            self.add_error("tendered", "Enter the cash received, or zero for a zero-total sale.")
        if data.get("method") == "bank":
            for field in ("reference", "note", "confirmed"):
                if not data.get(field):
                    self.add_error(field, "Required for a verified bank transfer.")
        return data


class RefundForm(forms.Form):
    reference = forms.CharField(max_length=120, label="Unique refund / voucher reference")
    note = forms.CharField(max_length=300, label="Refund reason and payment verification", widget=forms.Textarea(attrs={"rows": 3}))
    restock = forms.BooleanField(required=False, initial=True, label="Return all items to stock (only if received in resellable condition)")
    confirmed = forms.BooleanField(label="I verified that the full refund was paid to the customer.")


class ReportFilterForm(forms.Form):
    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    location = forms.ModelChoiceField(queryset=Location.objects.none(), required=False, label="Branch (optional)")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = Location.objects.all()
