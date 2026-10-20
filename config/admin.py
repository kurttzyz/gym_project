from django.contrib.admin import AdminSite


class PowerhouseAdminSite(AdminSite):
    site_header = "Powerhouse administration"
    site_title = "Powerhouse"

    def has_permission(self, request):
        return super().has_permission(request) and request.user.is_superuser
