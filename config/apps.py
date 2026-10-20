from django.contrib.admin.apps import AdminConfig


class PowerhouseAdminConfig(AdminConfig):
    default_site = "config.admin.PowerhouseAdminSite"
