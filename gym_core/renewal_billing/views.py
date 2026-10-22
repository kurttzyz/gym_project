from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from gym_core.classes.views import member_page
from gym_core.memberships.views import owner_page
from gym_core.members.services import EnrollmentError
from gyms.databases import ready_database
from .models import RenewalBillingSettings, RenewalPreference, RenewalInvoice
from .forms import SettingsForm, ConsentForm
from .services import opt_in, opt_out, cancel_invoice


@member_page
@never_cache
def preferences(request):
    config, _ = RenewalBillingSettings.objects.get_or_create(pk=1)
    form = ConsentForm(
        request.POST if request.method == "POST" else None, policy=config.late_policy
    )
    if request.method == "POST" and form.is_valid():
        try:
            opt_in(
                ready_database(request.tenant),
                request.tenant,
                request.class_member.pk,
                request.user.pk,
                form.cleaned_data["quoted_plan"],
            )
        except EnrollmentError as error:
            form.add_error(None, str(error))
        else:
            messages.success(
                request, "Automatic renewal invoicing enabled. No charge was authorized or made."
            )
            return redirect("member-auto-renewals")
    return render(
        request,
        "renewal_billing/member.html",
        {
            "config": config,
            "form": form,
            "preference": RenewalPreference.objects.filter(member=request.class_member).first(),
            "page": Paginator(
                request.class_member.renewal_invoices.select_related(
                    "subscription__payment", "previous_subscription"
                ),
                20,
            ).get_page(request.GET.get("page")),
        },
    )


@member_page
@require_POST
def stop(request):
    opt_out(ready_database(request.tenant), request.class_member.pk, request.user.pk)
    messages.success(
        request,
        "Future invoices stopped. Unsubmitted invoices were cancelled; already submitted payments still need reconciliation.",
    )
    return redirect("member-auto-renewals")


@owner_page
@never_cache
def settings_page(request):
    config, _ = RenewalBillingSettings.objects.get_or_create(pk=1)
    form = SettingsForm(request.POST if request.method == "POST" else None, instance=config)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(
            request,
            "Renewal invoice settings saved. Each member must opt in; no payment will be collected automatically.",
        )
        return redirect("owner-auto-renewals")
    return render(
        request,
        "renewal_billing/owner.html",
        {
            "config": config,
            "form": form,
            "page": Paginator(
                RenewalInvoice.objects.select_related("member", "subscription__payment"), 25
            ).get_page(request.GET.get("page")),
            "preferences": RenewalPreference.objects.select_related("member")
            .filter(enabled=True)
            .order_by("member__full_name")[:100],
        },
    )


@owner_page
@require_POST
def cancel(request, invoice_id):
    get_object_or_404(RenewalInvoice, pk=invoice_id)
    note = request.POST.get("note", "").strip()
    if not note or len(note) > 500:
        messages.error(request, "Enter a cancellation reason of up to 500 characters.")
    else:
        try:
            cancel_invoice(ready_database(request.tenant), invoice_id, request.user.pk, note)
        except EnrollmentError as error:
            messages.error(request, str(error))
        else:
            messages.success(
                request,
                "Unsubmitted invoice and pending term cancelled. No payment or refund was made.",
            )
    return redirect("owner-auto-renewals")
