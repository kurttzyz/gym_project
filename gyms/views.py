from functools import wraps
import logging

import psycopg
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from config.database_router import gym_database
from tenant_data.models import Location
from .databases import GymDatabaseUnavailable, provision_database, ready_database
from .forms import DomainForm, GymForm
from .models import Membership, Organization


def platform_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.tenant is not None or not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok"})


@platform_required
def organizations(request):
    providers = Organization.objects.values("id", "name", "slug")
    return JsonResponse({"organizations": list(providers)})


@platform_required
def dashboard(request):
    gyms = Organization.objects.all()
    return render(request, "gyms/dashboard.html", {"gyms": gyms})


@platform_required
def add_gym(request):
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


@platform_required
def gym_detail(request, slug):
    gym = get_object_or_404(Organization, slug=slug)
    try:
        alias = ready_database(gym)
        with gym_database(alias):
            branches = list(Location.objects.all())
    except (GymDatabaseUnavailable, DatabaseError, OSError):
        return render(request, "gyms/unavailable.html", status=503)
    return render(request, "gyms/detail.html", {
        "gym": gym, "branches": branches, "domain_form": DomainForm(),
        "domains": gym.domains.all(),
    })


@platform_required
def add_domain(request, slug):
    gym = get_object_or_404(Organization, slug=slug)
    if request.method != "POST":
        return redirect("gym-detail", slug=gym.slug)
    form = DomainForm(request.POST)
    if form.is_valid():
        domain = form.save(commit=False)
        domain.organization = gym
        domain.save()
        return redirect("gym-detail", slug=gym.slug)
    return render(request, "gyms/detail.html", {
        "gym": gym, "domains": gym.domains.all(), "domain_form": form,
        "branches": [],
    }, status=400)


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
