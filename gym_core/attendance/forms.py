import uuid
from django import forms
from tenant_data.models import Location
from gyms.forms import OwnerAccountForm


class ScanForm(forms.Form):
    code = forms.CharField(max_length=800, label="Member QR code", widget=forms.Textarea(attrs={"rows": 3, "autocomplete": "off", "spellcheck": "false"}))
    location = forms.ModelChoiceField(queryset=Location.objects.none(), label="Branch")
    action = forms.ChoiceField(choices=[("checkin", "Check in"), ("checkout", "Check out")])
    request_id = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Include disabled branches so staff can still check out existing visits.
        self.fields["location"].queryset = Location.objects.all()


class VisitFilterForm(forms.Form):
    location = forms.ModelChoiceField(queryset=Location.objects.none(), required=False, label="Branch")
    date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    open_only = forms.BooleanField(required=False, label="Currently checked in only")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].queryset = Location.objects.all()


class LocationForm(forms.ModelForm):
    address = forms.CharField(max_length=2000, required=False, widget=forms.Textarea(attrs={"rows": 3}))

    class Meta:
        model = Location
        fields = ("name", "address", "is_active")


class CloseVisitForm(forms.Form):
    note = forms.CharField(max_length=300, widget=forms.Textarea(attrs={"rows": 2}))


class FrontDeskAccountForm(OwnerAccountForm):
    role = forms.ChoiceField(choices=[("staff", "Front desk staff"), ("manager", "Manager")])
