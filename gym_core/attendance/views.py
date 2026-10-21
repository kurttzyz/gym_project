from functools import wraps
from io import BytesIO
import qrcode
import qrcode.image.svg
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST
from config.database_router import gym_database
from gym_core.members.models import MemberProfile
from gym_core.memberships.views import owner_page
from gyms.databases import GymDatabaseUnavailable, ready_database
from gyms.models import Membership
from tenant_data.models import Location
from .forms import ScanForm, VisitFilterForm, LocationForm, CloseVisitForm, FrontDeskAccountForm
from .models import AttendanceScan, AttendanceVisit
from .services import AttendanceError, card_code, record_scan, rotate_card, close_visit


FRONT_DESK_ROLES = {"owner", "manager", "staff"}


def attendance_page(view):
    @login_required
    @never_cache
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.tenant is None or not Membership.objects.filter(
                organization=request.tenant, user=request.user, is_active=True, role__in=FRONT_DESK_ROLES).exists():
            raise Http404
        try:
            with gym_database(ready_database(request.tenant)):
                return view(request, *args, **kwargs)
        except (GymDatabaseUnavailable, DatabaseError, OSError):
            return render(request, "gyms/unavailable.html", status=503)
    return wrapped


@sensitive_post_parameters("code")
@attendance_page
def scanner(request):
    result = None
    form = ScanForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            result = record_scan(ready_database(request.tenant), request.tenant, form.cleaned_data["code"],
                                 form.cleaned_data["location"].pk, form.cleaned_data["action"],
                                 form.cleaned_data["request_id"], request.user.pk)
        except AttendanceError as error:
            form.add_error(None, str(error))
        else:
            # New scan starts with a new request ID. Do not echo a credential back.
            form = ScanForm(initial={"location": form.cleaned_data["location"], "action": form.cleaned_data["action"]})
    return render(request, "attendance/scanner.html", {"form": form, "result": result,
        "has_branches": Location.objects.exists(), "open_count": AttendanceVisit.objects.filter(checked_out_at__isnull=True).count(),
        "today_count": AttendanceVisit.objects.filter(checked_in_at__date=timezone.localdate()).count()})


@attendance_page
def history(request):
    form = VisitFilterForm(request.GET)
    visits = AttendanceVisit.objects.select_related("member", "subscription", "location")
    if form.is_valid():
        if form.cleaned_data["location"]:
            visits = visits.filter(location=form.cleaned_data["location"])
        if form.cleaned_data["date"]:
            visits = visits.filter(checked_in_at__date=form.cleaned_data["date"])
        if form.cleaned_data["open_only"]:
            visits = visits.filter(checked_out_at__isnull=True)
    else:
        visits = visits.none()
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "attendance/history.html", {"form": form,
        "page": Paginator(visits, 25).get_page(request.GET.get("page")), "filter_query": query.urlencode(),
        "recent_scans": AttendanceScan.objects.select_related("member", "location")[:15]})


@attendance_page
@require_POST
def checkout_visit(request, visit_id):
    get_object_or_404(AttendanceVisit, pk=visit_id)
    form = CloseVisitForm(request.POST)
    if form.is_valid():
        close_visit(ready_database(request.tenant), visit_id, request.user.pk, form.cleaned_data["note"])
        messages.success(request, "Visit closed. The checkout operator and reason were recorded.")
    else:
        messages.error(request, "Enter a checkout reason (maximum 300 characters).")
    return redirect("attendance-history")


def member_page(view):
    @login_required
    @never_cache
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.tenant or not Membership.objects.filter(organization=request.tenant, user=request.user, is_active=True).exists():
            raise Http404
        try:
            with gym_database(ready_database(request.tenant)):
                member = get_object_or_404(MemberProfile, user_id=request.user.pk, is_active=True)
                return view(request, member, *args, **kwargs)
        except (GymDatabaseUnavailable, DatabaseError, OSError):
            return render(request, "gyms/unavailable.html", status=503)
    return wrapped


def render_card(request, member, *, owner=False):
    response = render(request, "attendance/card.html", {"member": member, "code": card_code(request.tenant, member), "owner": owner})
    response["Referrer-Policy"] = "no-referrer"
    return response


def render_qr(request, member):
    image = qrcode.make(card_code(request.tenant, member), image_factory=qrcode.image.svg.SvgPathImage, box_size=6, border=4)
    output = BytesIO()
    image.save(output)
    response = HttpResponse(output.getvalue(), content_type="image/svg+xml")
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@member_page
def my_card(request, member):
    return render_card(request, member)


@member_page
def my_qr(request, member):
    return render_qr(request, member)


@member_page
def my_history(request, member):
    visits = AttendanceVisit.objects.filter(member=member).select_related("location", "subscription")
    return render(request, "attendance/member_history.html", {"page": Paginator(visits, 25).get_page(request.GET.get("page"))})


@owner_page
@never_cache
def owner_card(request, member_id):
    return render_card(request, get_object_or_404(MemberProfile, pk=member_id), owner=True)


@owner_page
@never_cache
def owner_qr(request, member_id):
    return render_qr(request, get_object_or_404(MemberProfile, pk=member_id))


@owner_page
@require_POST
def replace_card(request, member_id):
    get_object_or_404(MemberProfile, pk=member_id)
    rotate_card(ready_database(request.tenant), member_id)
    messages.success(request, "Card replaced. All previous copies of this member's QR card are now invalid.")
    return redirect("member-detail", member_id=member_id)


@owner_page
def locations(request, location_id=None):
    location = get_object_or_404(Location, pk=location_id) if location_id else None
    form = LocationForm(request.POST if request.method == "POST" else None, instance=location)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic(using=ready_database(request.tenant)):
            if location:
                Location.objects.select_for_update().get(pk=location.pk)
            form.save()
        messages.success(request, "Branch saved. Disabling entry does not prevent existing visits from checking out.")
        return redirect("attendance-locations")
    return render(request, "attendance/locations.html", {"locations": Location.objects.all(), "form": form, "location": location})


@owner_page
def staff(request):
    form = FrontDeskAccountForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic(using="default"):
            user = form.save()
            Membership.objects.create(organization=request.tenant, user=user, role=form.cleaned_data["role"])
        messages.success(request, "Front-desk account created. This grants access only to this gym.")
        return redirect("attendance-staff")
    return render(request, "attendance/staff.html", {"form": form,
        "accesses": Membership.objects.filter(organization=request.tenant, role__in={"staff", "manager"}).select_related("user")})


@owner_page
@require_POST
def staff_access(request, access_id):
    access = get_object_or_404(Membership, pk=access_id, organization=request.tenant, role__in={"staff", "manager"})
    action = request.POST.get("action")
    if action not in {"enable", "disable"}:
        raise Http404
    Membership.objects.filter(pk=access.pk).update(is_active=action == "enable")
    messages.success(request, "This account's gym access was updated.")
    return redirect("attendance-staff")
