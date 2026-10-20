from django import forms
from django.contrib.auth.forms import AuthenticationForm
from .models import GymDomain, Membership, Organization, normalize_hostname


class GymForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ("name", "slug", "logo_url", "primary_color")
        labels = {"name": "Gym name", "slug": "Internal gym identifier"}


class DomainForm(forms.ModelForm):
    class Meta:
        model = GymDomain
        fields = ("hostname",)

    def clean_hostname(self):
        return normalize_hostname(self.cleaned_data["hostname"])


class PortalAuthenticationForm(AuthenticationForm):
    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        gym = getattr(self.request, "tenant", None)
        allowed = (Membership.objects.filter(
            organization=gym, user=user, is_active=True
        ).exists() if gym else user.is_superuser)
        if not allowed:
            raise forms.ValidationError(self.error_messages["invalid_login"], code="invalid_login",
                                        params={"username": self.username_field.verbose_name})
