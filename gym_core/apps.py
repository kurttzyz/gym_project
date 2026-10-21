from django.apps import AppConfig


class GymCoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gym_core"

    def ready(self):
        from .notifications import checks, signals  # noqa: F401
