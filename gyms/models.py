import uuid
from django.conf import settings
from django.db import models


class Organization(models.Model):
    """A gym provider (tenant), which can operate multiple locations."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
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
