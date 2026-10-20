from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
import logging
import psycopg
from django.db import DatabaseError
from config.database_router import gym_database
from tenant_data.models import Location
from .databases import GymDatabaseUnavailable, provision_database, ready_database
from .forms import GymForm
from .models import Organization


def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok"})


@login_required
def organizations(request):
    providers = Organization.objects.filter(
        memberships__user=request.user, memberships__is_active=True
    ).values("id", "name", "slug")
    return JsonResponse({"organizations": list(providers)})


@login_required
def dashboard(request):
    gyms = Organization.objects.filter(
        memberships__user=request.user, memberships__is_active=True
    )
    if request.user.is_superuser:
        gyms = Organization.objects.all()
    return render(request, "gyms/dashboard.html", {"gyms": gyms})


@login_required
def add_gym(request):
    # Only the hosting platform can provision a new tenant.
    if not request.user.is_superuser:
        raise PermissionDenied
    form = GymForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        gym = form.save()
        try:
            provision_database(gym)
        except (GymDatabaseUnavailable, DatabaseError, psycopg.Error, OSError):
            logging.getLogger(__name__).exception("Gym provisioning failed for %s", gym.pk)
            return render(request, "gyms/unavailable.html", status=503)
        return redirect("gym-detail", slug=gym.slug)
    return render(request, "gyms/add.html", {"form": form})


@login_required
def gym_detail(request, slug):
    # Resolve authorization before loading any tenant-owned data.
    allowed = Organization.objects.all() if request.user.is_superuser else (
        Organization.objects.filter(
            memberships__user=request.user, memberships__is_active=True
        )
    )
    gym = get_object_or_404(allowed, slug=slug)
    try:
        alias = ready_database(gym)
        with gym_database(alias):
            branches = list(Location.objects.all())
    except (GymDatabaseUnavailable, DatabaseError, OSError):
        return render(request, "gyms/unavailable.html", status=503)
    return render(request, "gyms/detail.html", {"gym": gym, "branches": branches})
