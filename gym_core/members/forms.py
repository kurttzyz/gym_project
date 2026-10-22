import hashlib
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.utils import timezone
from .models import MemberProfile
from gym_core.memberships.models import MembershipPlan
from tenant_data.models import Location


def waiver_version(text):
    return hashlib.sha256(text.encode()).hexdigest() if text else ""


class MemberProfileForm(forms.ModelForm):
    class Meta:
        model = MemberProfile
        fields = ("full_name", "email", "phone", "date_of_birth", "emergency_contact_name", "emergency_contact_phone", "is_active")
        widgets = {"date_of_birth": forms.DateInput(attrs={"type": "date"})}
        labels = {"is_active": "Enable member access"}

    def clean_date_of_birth(self):
        value = self.cleaned_data["date_of_birth"]
        if value and value > timezone.localdate():
            raise forms.ValidationError("Birth date cannot be in the future.")
        return value

    def clean(self):
        data = super().clean()
        if bool(data.get("emergency_contact_name")) != bool(data.get("emergency_contact_phone")):
            raise forms.ValidationError("Provide both emergency contact name and phone, or leave both blank.")
        return data


class FrontDeskMemberForm(MemberProfileForm):
    walk_in = forms.BooleanField(required=False,label="Walk-in / day pass")
    plan = forms.ModelChoiceField(queryset=MembershipPlan.objects.none(),label="Membership plan")

    branch = forms.ModelChoiceField(queryset=Location.objects.none(),required=False,label="Check-in branch (walk-ins)")

    class Meta(MemberProfileForm.Meta):
        fields = tuple(field for field in MemberProfileForm.Meta.fields if field != "is_active")

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['branch'].queryset=Location.objects.filter(is_active=True)
        branches=list(self.fields['branch'].queryset[:2])
        if len(branches)==1:self.fields['branch'].initial=branches[0].pk
        self.fields['plan'].queryset=MembershipPlan.objects.filter(is_active=True)
        self.fields['plan'].label_from_instance=lambda plan: f'{plan.name} — PHP {plan.price:.2f} / {plan.get_period_display()}'

    def clean(self):
        data=super().clean()
        if data.get('walk_in') and data.get('plan') and data['plan'].period != 'day':
            self.add_error('plan','Choose a day-pass plan for a walk-in visitor.')
        if data.get('walk_in') and not data.get('branch') and 'branch' not in self.errors:
            branches=list(self.fields['branch'].queryset[:2])
            if len(branches)==1:data['branch']=branches[0]
            else:self.add_error('branch','Select an active branch to check in this walk-in.')
        return data


class AssignPlanForm(forms.Form):
    plan = forms.ModelChoiceField(queryset=MembershipPlan.objects.none())
    starts_on = forms.DateField(initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = MembershipPlan.objects.filter(is_active=True)


class SubscriptionActionForm(forms.Form):
    action = forms.ChoiceField(choices=[(value, value.title()) for value in ("activate", "freeze", "resume", "cancel")])
    note = forms.CharField(max_length=1000, label="Reason / offline reference", widget=forms.Textarea(attrs={"rows": 2}))


class WaiverMixin:
    def __init__(self, *args, waiver_text="", **kwargs):
        self.waiver_text = waiver_text
        super().__init__(*args, **kwargs)
        self.fields["waiver_version"].initial = waiver_version(waiver_text)
        if waiver_text:
            self.fields["accept_waiver"].required = True
        else:
            self.fields.pop("accept_waiver", None)

    def clean_waiver_version(self):
        value = self.cleaned_data.get("waiver_version", "")
        if value != waiver_version(self.waiver_text):
            raise forms.ValidationError("The waiver has changed. Review the current text and accept it again.")
        return value


class MemberAccountForm(WaiverMixin, UserCreationForm):
    accept_waiver = forms.BooleanField(required=False, label="I have read and accept the gym waiver shown above.")
    waiver_version = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username",)


class ExistingMemberAccountForm(WaiverMixin, AuthenticationForm):
    accept_waiver = forms.BooleanField(required=False, label="I have read and accept the gym waiver shown above.")
    waiver_version = forms.CharField(required=False, widget=forms.HiddenInput)
