from functools import wraps
from uuid import UUID
from django.db import DatabaseError, IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from config.database_router import gym_database
from gyms.databases import GymDatabaseUnavailable, ready_database
from gym_core.memberships.models import MembershipPlan
from gym_core.memberships.forms import MembershipApplicationForm
from websites.forms import ContactForm
from websites.services import website_for_request


def public_page(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            request.website = website_for_request(request)
            with gym_database(ready_database(request.tenant)):
                # Render inside the scope: forms evaluate their plan queryset lazily.
                return view(request, *args, **kwargs)
        except (GymDatabaseUnavailable, DatabaseError, OSError):
            return render(request, "gyms/unavailable.html", status=503)
    return wrapped


@public_page
def home(request):
    plans = MembershipPlan.objects.filter(is_active=True)[:3]
    return render(request, "websites/home.html", {"site": request.website, "plans": plans})


@public_page
def about(request):
    return render(request, "websites/about.html", {"site": request.website})


@public_page
def memberships(request):
    return render(request, "websites/memberships.html", {
        "site": request.website, "plans": MembershipPlan.objects.filter(is_active=True)
    })


@public_page
def join(request):
    initial = {}
    if request.method == "GET" and request.GET.get("plan"):
        try:
            plan_id = UUID(request.GET["plan"])
        except (ValueError, AttributeError):
            raise Http404 from None
        initial["plan"] = get_object_or_404(MembershipPlan, pk=plan_id, is_active=True)
    form = MembershipApplicationForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic(using=ready_database(request.tenant)):
                plan = MembershipPlan.objects.select_for_update().get(
                    pk=form.cleaned_data["plan"].pk, is_active=True
                )
                application = form.save(commit=False)
                application.quoted_plan_name = plan.name
                application.quoted_price = plan.price
                application.quoted_period = plan.period
                application.save()
        except MembershipPlan.DoesNotExist:
            form.add_error("plan", "This plan is no longer available.")
        except IntegrityError:
            form.add_error(None, "This request has already been received. Contact the gym for an update.")
        else:
            return redirect("join-thanks")
    return render(request, "websites/join.html", {
        "site": request.website, "form": form,
        "has_plans": MembershipPlan.objects.filter(is_active=True).exists(),
    })


@public_page
def join_thanks(request):
    return render(request, "websites/thanks.html", {"site": request.website, "membership_request": True})


@public_page
def contact(request):
    form = ContactForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("contact-thanks")
    return render(request, "websites/contact.html", {"site": request.website, "form": form})


@public_page
def contact_thanks(request):
    return render(request, "websites/thanks.html", {"site": request.website})
