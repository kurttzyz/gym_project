from django.contrib.auth.decorators import login_required
from django.db import DatabaseError
from django.http import Http404
from django.shortcuts import render

from config.database_router import gym_database
from gyms.databases import GymDatabaseUnavailable, ready_database
from gyms.models import Membership
from tenant_data.models import Location


@login_required
def tenant_dashboard(request):
    gym = request.tenant
    if gym is None or not Membership.objects.filter(
        organization=gym, user=request.user, is_active=True
    ).exists():
        raise Http404
    try:
        with gym_database(ready_database(gym)):
            branches = list(Location.objects.all())
    except (GymDatabaseUnavailable, DatabaseError, OSError):
        return render(request, "gyms/unavailable.html", status=503)
    return render(request, "gyms/tenant_dashboard.html", {"branches": branches})
