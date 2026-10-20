from django.contrib.auth.decorators import login_required
from django.db import DatabaseError
from django.http import Http404
from django.shortcuts import render

from config.database_router import gym_database
from gyms.databases import GymDatabaseUnavailable, ready_database
from gyms.models import Membership
from tenant_data.models import Location
from .permissions import owner_required
from websites.services import website_for_request
from .memberships.models import MembershipApplication, MembershipPlan
from websites.models import ContactMessage


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


@owner_required
def owner_dashboard(request):
    try:
        site = website_for_request(request)
        with gym_database(ready_database(request.tenant)):
            branch_count = Location.objects.count()
            plan_count = MembershipPlan.objects.filter(is_active=True).count()
            application_count = MembershipApplication.objects.filter(status="new").count()
            message_count = ContactMessage.objects.filter(is_read=False).count()
    except (GymDatabaseUnavailable, DatabaseError, OSError):
        return render(request, "gyms/unavailable.html", status=503)
    return render(request, "gyms/owner_dashboard.html", {"site": site, "branch_count": branch_count,
        "plan_count": plan_count, "application_count": application_count, "message_count": message_count})
