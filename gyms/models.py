import uuid
import secrets
import re
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator, URLValidator
from django.conf import settings
from django.db import models


class Organization(models.Model):
    """A gym provider (tenant), which can operate multiple locations."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    logo_url = models.URLField(blank=True, validators=[URLValidator(schemes=["https"])])
    primary_color = models.CharField(max_length=7, default="#245ac5", validators=[
        RegexValidator(r"^#[0-9a-fA-F]{6}$", "Use a six-digit hex color, e.g. #245ac5.")
    ])
    database_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    database_ready = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Membership(models.Model):
    """Organization access, separate from future paid gym subscriptions."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MANAGER = "manager", "Manager"
        STAFF = "staff", "Staff"
        MEMBER = "member", "Member"

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["organization", "user"], name="unique_user_per_organization"
        )]

    def __str__(self):
        return f"{self.user} at {self.organization} ({self.role})"


def domain_token():
    return secrets.token_urlsafe(32)


def normalize_hostname(value):
    value = value.strip().rstrip(".").lower()
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValidationError("Enter a valid hostname.") from error
    labels = value.split(".")
    if (len(value) > 253 or len(labels) < 2 or labels[-1].isdigit() or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    )):
        raise ValidationError("Enter only a hostname, without a scheme, port, or path.")
    if value in settings.PLATFORM_HOSTS:
        raise ValidationError("This hostname is reserved for the platform.")
    return value


class GymDomain(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="domains")
    hostname = models.CharField(max_length=253, unique=True)
    verification_token = models.CharField(max_length=64, default=domain_token, editable=False)
    verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    is_active = models.BooleanField(default=True)

    def clean(self):
        super().clean()
        self.hostname = normalize_hostname(self.hostname)

    def save(self, *args, **kwargs):
        self.hostname = normalize_hostname(self.hostname)
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            if previous.hostname != self.hostname or previous.organization_id != self.organization_id:
                raise ValidationError("Domain bindings are immutable; register a new domain instead.")
        super().save(*args, **kwargs)

    def __str__(self):
        return self.hostname
