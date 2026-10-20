from django import forms
from .models import MembershipApplication, MembershipPlan


class MembershipPlanForm(forms.ModelForm):
    class Meta:
        model = MembershipPlan
        fields = ("name", "description", "price", "period", "features", "is_active", "is_featured", "sort_order")
        widgets = {"description": forms.Textarea(attrs={"rows": 3}),
                   "features": forms.Textarea(attrs={"rows": 5})}
        help_texts = {"price": "Price in Philippine pesos (PHP). No online charge is collected.",
                      "is_active": "Publish this plan on the gym website.",
                      "is_featured": "Highlight this plan in the membership page."}


class MembershipApplicationForm(forms.ModelForm):
    consent = forms.BooleanField(label="I agree that this gym may contact me about my membership request.")
    website_url = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = MembershipApplication
        fields = ("plan", "full_name", "email", "phone", "message")
        widgets = {"message": forms.Textarea(attrs={"rows": 3})}
        labels = {"plan": "Your membership plan", "message": "Anything you would like us to know?"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = MembershipPlan.objects.filter(is_active=True)

    def clean_website_url(self):
        if self.cleaned_data.get("website_url"):
            raise forms.ValidationError("Please leave this field blank.")
        return ""

    def clean(self):
        data = super().clean()
        plan, email = data.get("plan"), data.get("email")
        if plan and email and MembershipApplication.objects.filter(
            plan=plan, email__iexact=email, status__in=["new", "contacted"]
        ).exists():
            raise forms.ValidationError("A request for this plan has already been received. Please contact the gym for an update.")
        return data
