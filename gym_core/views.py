from django.contrib.auth.decorators import login_required
from django.db import DatabaseError
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.cache import never_cache

from config.database_router import gym_database
from gyms.databases import GymDatabaseUnavailable, ready_database
from gyms.models import Membership
from tenant_data.models import Location
from .permissions import owner_required
from websites.services import website_for_request
from .memberships.models import MembershipApplication, MembershipPlan
from websites.models import ContactMessage
from .members.models import MemberProfile
from .memberships.models import MemberSubscription
from platform_core.billing.services import access_for_gym
from .dashboard_data import owner_overview,member_overview


@login_required
@never_cache
def tenant_dashboard(request):
    gym = request.tenant
    if gym is None or not Membership.objects.filter(
        organization=gym, user=request.user, is_active=True
    ).exists():
        raise Http404
    try:
        with gym_database(ready_database(gym)):
            profile = MemberProfile.objects.filter(user_id=request.user.pk, is_active=True).first()
            access = Membership.objects.get(organization=gym, user=request.user)
            if access.role == "member" and profile is None:
                raise Http404
            subscriptions = list(profile.subscriptions.select_related("member", "payment")) if profile else []
            branches = list(Location.objects.all())
            overview=member_overview(profile,access_for_gym(gym).modules,subscriptions) if profile else {}
    except (GymDatabaseUnavailable, DatabaseError, OSError):
        return render(request, "gyms/unavailable.html", status=503)
    return render(request, "gyms/tenant_dashboard.html", {"branches": branches, "profile": profile, "subscriptions": subscriptions,**overview})


@owner_required
@never_cache
def owner_dashboard(request):
    try:
        site = website_for_request(request)
        with gym_database(ready_database(request.tenant)):
            branch_count = Location.objects.count()
            member_count = MemberProfile.objects.count()
            plan_count = MembershipPlan.objects.filter(is_active=True).count()
            application_count = MembershipApplication.objects.filter(status="new").count()
            message_count = ContactMessage.objects.filter(is_read=False).count()
            access=access_for_gym(request.tenant)
            period=30 if request.GET.get('period')=='30' else 7
            overview=owner_overview(access.modules if access.allowed else {},period)
    except (GymDatabaseUnavailable, DatabaseError, OSError):
        return render(request, "gyms/unavailable.html", status=503)
    return render(request, "gyms/owner_dashboard.html", {"site": site, "branch_count": branch_count,
        "member_count": member_count, "plan_count": plan_count, "application_count": application_count, "message_count": message_count,**overview})
