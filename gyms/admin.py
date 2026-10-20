from django.contrib import admin
from .models import GymDomain, Membership, Organization


class PlatformAdmin(admin.ModelAdmin):
    """Reserve cross-tenant administration for platform superusers."""

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


@admin.register(Organization)
class OrganizationAdmin(PlatformAdmin):
    list_display = ("name", "slug", "created_at")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("database_key", "database_ready")

    def has_add_permission(self, request):
        # Tenant creation must run the provisioning workflow at /gyms/new/.
        return False

    def has_delete_permission(self, request, obj=None):
        # Database retirement requires a separate, explicit retention workflow.
        return False


@admin.register(Membership)
class MembershipAdmin(PlatformAdmin):
    list_display = ("user", "organization", "role", "is_active")
    list_filter = ("role", "is_active", "organization")
    autocomplete_fields = ("user", "organization")


@admin.register(GymDomain)
class GymDomainAdmin(PlatformAdmin):
    list_display = ("hostname", "organization", "verified_at", "is_active")
    readonly_fields = ("hostname", "organization", "verification_token", "verified_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
