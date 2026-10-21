from django import forms
from platform_core.models import SaaSPlan


class SaaSPlanForm(forms.ModelForm):
    class Meta:
        model = SaaSPlan
        fields = ("name", "price", "period", "websites", "memberships", "attendance", "payments", "is_active")
        labels = {"price": "Platform subscription price (PHP)", "payments": "Gym payment module", "is_active": "Available for new assignments"}


class AssignmentForm(forms.Form):
    plan = forms.ModelChoiceField(queryset=SaaSPlan.objects.filter(is_active=True))
    trial_days = forms.IntegerField(min_value=0, max_value=90, initial=14)
    grace_days = forms.IntegerField(min_value=0, max_value=30, initial=0)


class PlanChangeForm(forms.Form):
    plan = forms.ModelChoiceField(queryset=SaaSPlan.objects.filter(is_active=True))
    note = forms.CharField(max_length=500, widget=forms.Textarea(attrs={"rows": 2}), label="Reason")


class StateForm(forms.Form):
    action = forms.ChoiceField(choices=[("suspend", "Suspend operations"), ("resume", "Remove suspension"), ("cancel", "Cancel subscription")])
    note = forms.CharField(max_length=500, widget=forms.Textarea(attrs={"rows": 2}), label="Reason")


class RecordPaymentForm(forms.Form):
    reference = forms.CharField(max_length=120, label="Unique payment / receipt reference")
    method = forms.ChoiceField(choices=[("bank", "Bank transfer"), ("cash", "Cash"), ("other", "Other"), ("complimentary", "No charge (zero-priced invoice)")])
    note = forms.CharField(max_length=500, widget=forms.Textarea(attrs={"rows": 3}), label="Verification note")
