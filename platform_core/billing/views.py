from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_safe
from django.views.decorators.cache import never_cache
from platform_core.views import platform_required
from platform_core.models import SaaSPlan, GymSubscription, PlatformInvoice, PlatformPayment
from gyms.models import Organization
from gym_core.permissions import owner_required
from .forms import SaaSPlanForm, AssignmentForm, PlanChangeForm, StateForm, RecordPaymentForm
from .services import BillingError, assign_plan, change_plan, change_state, record_payment, access_for_gym


@platform_required
def dashboard(request):
    invoices = PlatformInvoice.objects.select_related("subscription__organization")
    state = request.GET.get("status", "")
    if state in {"issued", "paid", "void"}:
        invoices = invoices.filter(status=state)
    q = request.GET.get("q", "").strip()[:160]
    if q:
        invoices = invoices.filter(subscription__organization__name__icontains=q)
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "platform_billing/dashboard.html", {
        "page": Paginator(invoices, 25).get_page(request.GET.get("page")), "query": q,
        "filter_query": query.urlencode(), "status": state, "subscription_count": GymSubscription.objects.count(),
        "revenue": PlatformPayment.objects.aggregate(total=Sum("amount"))["total"] or 0,
        "due_total": PlatformInvoice.objects.filter(status="issued", due_on__lte=timezone.localdate()).aggregate(total=Sum("amount"))["total"] or 0,
    })


@platform_required
def plans(request):
    return render(request, "platform_billing/plans.html", {"plans": SaaSPlan.objects.all()})


@platform_required
def edit_plan(request, plan_id=None):
    plan = get_object_or_404(SaaSPlan, pk=plan_id) if plan_id else None
    form = SaaSPlanForm(request.POST if request.method == "POST" else None, instance=plan)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "SaaS plan saved. Existing subscription and invoice quotes were preserved.")
        return redirect("saas-plans")
    return render(request, "platform_billing/edit_plan.html", {"form": form, "plan": plan})


@platform_required
def subscription(request, slug):
    gym = get_object_or_404(Organization, slug=slug)
    current = GymSubscription.objects.filter(organization=gym).first()
    form = AssignmentForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            assign_plan(gym.pk, form.cleaned_data["plan"].pk, form.cleaned_data["trial_days"], form.cleaned_data["grace_days"], request.user.pk)
        except BillingError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, "Platform subscription assigned and the first invoice issued.")
            return redirect("saas-subscription", slug=gym.slug)
    return render(request, "platform_billing/subscription.html", {"gym": gym, "subscription": current,
        "access": access_for_gym(gym), "form": form, "plan_form": PlanChangeForm(), "state_form": StateForm(),
        "invoices": current.invoices.all()[:20] if current else [],
        "events": current.events.select_related("actor")[:20] if current else []})


@platform_required
@require_POST
def update_plan(request, slug):
    current = get_object_or_404(GymSubscription, organization__slug=slug)
    form = PlanChangeForm(request.POST)
    if form.is_valid():
        try:
            change_plan(current.pk, form.cleaned_data["plan"].pk, request.user.pk, form.cleaned_data["note"])
        except BillingError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "Next unpaid term updated. Paid terms and the original trial were preserved.")
    else:
        messages.error(request, "Select an available plan and enter a reason (maximum 500 characters).")
    return redirect("saas-subscription", slug=slug)


@platform_required
@require_POST
def update_state(request, slug):
    current = get_object_or_404(GymSubscription, organization__slug=slug)
    form = StateForm(request.POST)
    if form.is_valid():
        try:
            change_state(current.pk, form.cleaned_data["action"], request.user.pk, form.cleaned_data["note"])
        except BillingError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "Subscription state updated. Removing a suspension does not waive unpaid invoices.")
    else:
        messages.error(request, "Choose an action and enter a reason (maximum 500 characters).")
    return redirect("saas-subscription", slug=slug)


@platform_required
@never_cache
def invoice(request, invoice_id):
    current = get_object_or_404(PlatformInvoice.objects.select_related("subscription__organization", "payment_record__recorded_by"), pk=invoice_id)
    return render(request, "platform_billing/invoice.html", {"invoice": current, "platform": True, "form": RecordPaymentForm()})


@platform_required
@require_POST
def confirm_payment(request, invoice_id):
    get_object_or_404(PlatformInvoice, pk=invoice_id)
    form = RecordPaymentForm(request.POST)
    if form.is_valid():
        try:
            record_payment(invoice_id, request.user.pk, form.cleaned_data["reference"], form.cleaned_data["method"], form.cleaned_data["note"])
        except (BillingError, IntegrityError) as error:
            messages.error(request, str(error) if isinstance(error, BillingError) else "That payment reference was already recorded. No new payment was added.")
        else:
            messages.success(request, "Payment recorded. The next invoice was issued automatically.")
    else:
        messages.error(request, "Enter a valid payment method, unique reference, and verification note.")
    return redirect("saas-invoice", invoice_id=invoice_id)


@owner_required
@require_safe
@never_cache
def tenant_billing(request):
    request.platform_billing_only = True
    current = GymSubscription.objects.filter(organization=request.tenant).first()
    invoices = current.invoices.all() if current else PlatformInvoice.objects.none()
    return render(request, "platform_billing/tenant.html", {"subscription": current, "access": access_for_gym(request.tenant),
        "page": Paginator(invoices, 25).get_page(request.GET.get("page")), "instructions": settings.PLATFORM_BILLING_INSTRUCTIONS})


@owner_required
@require_safe
@never_cache
def tenant_invoice(request, invoice_id):
    request.platform_billing_only = True
    current = get_object_or_404(PlatformInvoice.objects.select_related("subscription__organization", "payment_record"),
                              pk=invoice_id, subscription__organization=request.tenant)
    return render(request, "platform_billing/invoice.html", {"invoice": current, "platform": False,
        "instructions": settings.PLATFORM_BILLING_INSTRUCTIONS})
