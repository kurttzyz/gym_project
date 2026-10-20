from copy import copy
from django.contrib import messages
from django.db import DatabaseError
from django.shortcuts import redirect, render
from config.database_router import gym_database
from gym_core.permissions import owner_required
from gyms.databases import GymDatabaseUnavailable, ready_database
from .forms import WebsiteSettingsForm
from .services import website_for_request


def homepage(request):
    try:
        site = website_for_request(request)
    except (GymDatabaseUnavailable, DatabaseError, OSError):
        return render(request, "gyms/unavailable.html", status=503)
    return render(request, "websites/home.html", {"site": site})


@owner_required
def edit_website(request):
    try:
        site = website_for_request(request)
        with gym_database(ready_database(request.tenant)):
            form = WebsiteSettingsForm(request.POST if request.method == "POST" else None, instance=copy(site))
            if request.method == "POST" and form.is_valid():
                form.save()
                messages.success(request, "Your website has been updated.")
                return redirect("website-edit")
    except (GymDatabaseUnavailable, DatabaseError, OSError):
        return render(request, "gyms/unavailable.html", status=503)
    return render(request, "websites/edit.html", {"form": form})
