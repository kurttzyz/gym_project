from functools import wraps
import logging

import psycopg
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, connection, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from config.database_router import gym_database
from tenant_data.models import Location
from websites.models import ContactMessage, WebsiteSettings
from gym_core.memberships.models import MembershipPlan, MemberSubscription
from gym_core.members.models import MemberProfile
from payments.models import PaymentOrder
from gym_core.classes.models import Trainer, ClassSession, ClassBooking, BookingEvent
from pos.models import Product, StockBalance, StockMovement, Sale, SaleRefund
from gym_core.attendance.models import AttendanceScan, AttendanceVisit
from gyms.databases import GymDatabaseUnavailable, provision_database, ready_database
from gyms.forms import DomainForm, GymForm, OwnerAccountForm
from gyms.models import GymDomain, Membership, Organization
from gyms.domains import (DomainVerificationError, ensure_local_portal, is_local_hostname,
                          local_portals_enabled, portal_links, verify_domain)


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
    owner_form = OwnerAccountForm(request.POST if request.method == "POST" else None, prefix="owner")
    if request.method == "POST" and all([form.is_valid(), owner_form.is_valid()]):
        # Central records commit together; tenant provisioning is retryable and
        # cannot share an atomic transaction with a separate PostgreSQL database.
        with transaction.atomic():
            gym = form.save()
            owner = owner_form.save()
            Membership.objects.create(organization=gym, user=owner, role=Membership.Role.OWNER)
            if form.cleaned_data["saas_plan"]:
                from platform_core.billing.services import assign_plan
                trial = form.cleaned_data["trial_days"]
                grace = form.cleaned_data["grace_days"]
                assign_plan(gym.pk, form.cleaned_data["saas_plan"].pk,
                    14 if trial is None else trial, 0 if grace is None else grace, request.user.pk)
            if form.cleaned_data["domain_hostname"]:
                GymDomain.objects.create(organization=gym, hostname=form.cleaned_data["domain_hostname"])
        try:
            provision_database(gym)
        except (GymDatabaseUnavailable, DatabaseError, psycopg.Error, OSError):
            logging.getLogger(__name__).exception("Gym provisioning failed for %s", gym.pk)
            messages.error(request, "Gym and owner were saved, but database setup needs a retry. Use Retry setup below.")
            return redirect("gym-detail", slug=gym.slug)
        if local_portals_enabled(request):
            try:
                ensure_local_portal(gym)
            except DomainVerificationError as error:
                messages.error(request, str(error))
        return redirect("gym-detail", slug=gym.slug)
    return render(request, "gyms/add.html", {"form": form, "owner_form": owner_form})


def gym_detail_context(request, gym, domain_form=None):
    database_error = False
    branches = []
    try:
        with gym_database(ready_database(gym)):
            branches = list(Location.objects.all())
            WebsiteSettings.objects.get(pk=1)
            MembershipPlan.objects.exists()
            MemberProfile.objects.first()
            MemberSubscription.objects.exists()
            PaymentOrder.objects.first()
            AttendanceVisit.objects.first()
            AttendanceScan.objects.first()
            ContactMessage.objects.exists()
            Trainer.objects.first()
            ClassSession.objects.first()
            ClassBooking.objects.first()
            BookingEvent.objects.first()
            Product.objects.first()
            StockBalance.objects.first()
            StockMovement.objects.first()
            Sale.objects.first()
            SaleRefund.objects.first()
    except (GymDatabaseUnavailable, DatabaseError, WebsiteSettings.DoesNotExist, OSError):
        database_error = True
    domains = list(gym.domains.all())
    cards = [{"domain": domain, "links": portal_links(request, domain),
              "local": is_local_hostname(domain.hostname)} for domain in domains]
    return {
        "gym": gym, "branches": branches, "database_error": database_error,
        "domain_form": domain_form if domain_form is not None else DomainForm(),
        "domain_cards": cards, "local_portals": local_portals_enabled(request),
        "owners": gym.memberships.filter(role=Membership.Role.OWNER).select_related("user"),
    }


@platform_required
def gym_detail(request, slug):
    gym = get_object_or_404(Organization, slug=slug)
    return render(request, "gyms/detail.html", gym_detail_context(request, gym))


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
        if domain.hostname.endswith(".localhost") and local_portals_enabled(request):
            verify_domain(domain, local=True)
        return redirect("gym-detail", slug=gym.slug)
    return render(request, "gyms/detail.html", gym_detail_context(request, gym, form), status=400)


@platform_required
def add_owner(request, slug):
    gym = get_object_or_404(Organization, slug=slug)
    form = OwnerAccountForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            owner = form.save()
            Membership.objects.create(organization=gym, user=owner, role=Membership.Role.OWNER)
        return redirect("gym-detail", slug=gym.slug)
    return render(request, "gyms/add_owner.html", {"gym": gym, "form": form})


@platform_required
@require_POST
def enable_local_portal(request, slug):
    gym = get_object_or_404(Organization, slug=slug)
    if not local_portals_enabled(request):
        raise PermissionDenied
    try:
        ensure_local_portal(gym)
        messages.success(request, "Local portal is ready. Use the website and owner-login links below.")
    except DomainVerificationError as error:
        messages.error(request, str(error))
    return redirect("gym-detail", slug=gym.slug)


@platform_required
@require_POST
def verify_gym_domain(request, slug, domain_id):
    domain = get_object_or_404(GymDomain, pk=domain_id, organization__slug=slug)
    try:
        if is_local_hostname(domain.hostname) and not local_portals_enabled(request):
            raise DomainVerificationError("Activate local domains only from the local development platform.")
        verify_domain(domain, local=is_local_hostname(domain.hostname))
        messages.success(request, "Domain ownership verified. Website and owner-login links are available below.")
    except DomainVerificationError as error:
        messages.error(request, str(error))
    return redirect("gym-detail", slug=slug)


@platform_required
@require_POST
def retry_gym_setup(request, slug):
    gym = get_object_or_404(Organization, slug=slug)
    try:
        provision_database(gym)
        if local_portals_enabled(request):
            ensure_local_portal(gym)
        messages.success(request, "Gym setup is complete. Existing accounts and website edits were preserved.")
    except (DomainVerificationError, GymDatabaseUnavailable, DatabaseError, psycopg.Error, OSError):
        logging.getLogger(__name__).exception("Gym setup retry failed for %s", gym.pk)
        messages.error(request, "Setup could not finish. Check the PostgreSQL service or contact the platform operator.")
    return redirect("gym-detail", slug=slug)
