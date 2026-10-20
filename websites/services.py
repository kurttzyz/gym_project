from django.db import DatabaseError
from config.database_router import gym_database
from gyms.databases import GymDatabaseUnavailable, ready_database
from .models import WebsiteSettings


def website_for_request(request):
    """Load the host's site once; never accept a posted tenant/database selector."""
    if getattr(request, "website_unavailable", False):
        raise GymDatabaseUnavailable("Gym website is unavailable")
    if hasattr(request, "website"):
        return request.website
    try:
        with gym_database(ready_database(request.tenant)):
            request.website = WebsiteSettings.objects.get(pk=1)
    except (GymDatabaseUnavailable, DatabaseError, WebsiteSettings.DoesNotExist, OSError):
        request.website_unavailable = True
        raise GymDatabaseUnavailable("Migrate the gym database to initialize its website") from None
    return request.website
