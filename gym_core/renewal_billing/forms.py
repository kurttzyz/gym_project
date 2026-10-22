from django import forms
from gym_core.memberships.models import MembershipPlan
from .models import RenewalBillingSettings
from .services import plan_quote, read_quote
from gym_core.members.services import EnrollmentError


class SettingsForm(forms.ModelForm):
    class Meta:
        model = RenewalBillingSettings
        fields = ["enabled", "lead_days", "late_policy"]
        labels = {
            "enabled": "Enable automatic renewal invoices (no automatic debit)",
            "lead_days": "Generate invoice this many days before expiry",
        }


class ConsentForm(forms.Form):
    quoted_plan = forms.CharField(label="Plan and quoted price", widget=forms.Select)
    acknowledge = forms.BooleanField(
        label="I agree to automatic renewal invoices for this plan and price. I must authorize each one-time COPAY payment myself."
    )

    def __init__(self, *args, policy, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["quoted_plan"].widget.choices = [
            (
                plan_quote(plan, policy),
                f"{plan.name} — PHP {plan.price:.2f} / {plan.get_period_display()}",
            )
            for plan in MembershipPlan.objects.filter(
                is_active=True, price__gt=0, period__in=["month", "quarter", "year"]
            )
        ]

    def clean_quoted_plan(self):
        value = self.cleaned_data["quoted_plan"]
        try:
            read_quote(value)
        except EnrollmentError as error:
            raise forms.ValidationError(str(error))
        return value
