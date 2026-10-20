from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth import get_user_model
from django.conf import settings
from .models import GymDomain, Membership, Organization, normalize_hostname


class GymForm(forms.ModelForm):
    domain_hostname = forms.CharField(
        required=False, max_length=253, label="Custom domain (optional)",
        help_text="For a real gym domain, enter only its hostname. During local development a local portal is created automatically.",
    )

    def clean_domain_hostname(self):
        value = self.cleaned_data["domain_hostname"]
        if not value:
            return ""
        value = normalize_hostname(value)
        if GymDomain.objects.filter(hostname=value).exists():
            raise forms.ValidationError("This domain is already registered.")
        if not settings.DEBUG and value.endswith((".localhost", ".test")):
            raise forms.ValidationError("Use a real domain outside local development.")
        return value

    class Meta:
        model = Organization
        fields = ("name", "slug", "logo_url", "primary_color")
        labels = {"name": "Gym name", "slug": "Internal gym identifier"}


class DomainForm(forms.ModelForm):
    class Meta:
        model = GymDomain
        fields = ("hostname",)

    def clean_hostname(self):
        hostname = normalize_hostname(self.cleaned_data["hostname"])
        if not settings.DEBUG and hostname.endswith((".localhost", ".test")):
            raise forms.ValidationError("Use a real domain outside local development.")
        return hostname


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


class OwnerAccountForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = False
        user.is_superuser = False
        if commit:
            user.save()
        return user
