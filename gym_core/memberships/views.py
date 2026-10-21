from functools import wraps
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from config.database_router import gym_database
from gyms.databases import GymDatabaseUnavailable, ready_database
from gym_core.permissions import owner_required
from websites.models import ContactMessage
from .forms import MembershipPlanForm
from .models import MembershipApplication, MembershipPlan


def owner_page(view):
    @owner_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            with gym_database(ready_database(request.tenant)):
                return view(request, *args, **kwargs)
        except (GymDatabaseUnavailable, DatabaseError, OSError):
            return render(request, "gyms/unavailable.html", status=503)
    return wrapped


@owner_page
def plans(request):
    return render(request, "memberships/owner_plans.html", {"plans": MembershipPlan.objects.all()})


@owner_page
def edit_plan(request, plan_id=None):
    plan = get_object_or_404(MembershipPlan, pk=plan_id) if plan_id else None
    form = MembershipPlanForm(request.POST if request.method == "POST" else None, instance=plan)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Membership plan saved.")
        return redirect("owner-plans")
    return render(request, "memberships/edit_plan.html", {"form": form, "plan": plan})


@owner_page
def applications(request):
    page = Paginator(MembershipApplication.objects.select_related("plan", "subscription__member"), 25).get_page(request.GET.get("page"))
    return render(request, "memberships/applications.html", {"page": page})


@owner_page
@require_POST
def review_application(request, application_id):
    status = request.POST.get("status")
    if status not in {"contacted", "approved", "declined"}:
        return HttpResponseBadRequest("Invalid application status")
    with transaction.atomic(using=ready_database(request.tenant)):
        application = get_object_or_404(MembershipApplication.objects.select_for_update(), pk=application_id)
        if application.status not in {"new", "contacted"}:
            messages.error(request, "This application has already been closed.")
        else:
            application.status = status
            application.save(update_fields=["status", "updated_at"])
            messages.success(request, "Application updated. No payment was collected or account activated.")
    return redirect("owner-applications")


@owner_page
def inbox(request):
    page = Paginator(ContactMessage.objects.all(), 25).get_page(request.GET.get("page"))
    return render(request, "memberships/inbox.html", {"page": page})


@owner_page
@require_POST
def mark_read(request, message_id):
    message = get_object_or_404(ContactMessage, pk=message_id)
    message.is_read = True
    message.save(update_fields=["is_read"])
    return redirect("owner-inbox")
