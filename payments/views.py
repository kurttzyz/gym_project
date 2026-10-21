from functools import wraps
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.contrib.auth.decorators import login_required
from django.db import DatabaseError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from config.database_router import gym_database
from gym_core.memberships.views import owner_page
from gym_core.memberships.models import MemberSubscription
from gyms.databases import GymDatabaseUnavailable, ready_database
from gyms.models import Membership
from .client_ip import gateway_client_ip
from .models import PaymentOrder
from .services import create_order, start_checkout, reconcile, process_callback
from .gateway_adapters.copay import CopayError, CopayConfigurationError, config_for_gym


def payment_page(view):
    @login_required
    @never_cache
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        access = Membership.objects.filter(organization=request.tenant, user=request.user, is_active=True).first()
        if not request.tenant or not access or access.role not in {"owner", "member"}:
            raise Http404
        request.payment_owner = access.role == "owner"
        try:
            with gym_database(ready_database(request.tenant)):
                return view(request, *args, **kwargs)
        except (GymDatabaseUnavailable, DatabaseError, OSError):
            return render(request, "gyms/unavailable.html", status=503)
    return wrapped


def authorized_order(request, order_id):
    orders = PaymentOrder.objects.select_related("subscription__member")
    if not request.payment_owner:
        orders = orders.filter(subscription__member__user_id=request.user.pk, subscription__member__is_active=True)
    return get_object_or_404(orders, pk=order_id)


@owner_page
def owner_payments(request):
    try:
        config = config_for_gym(request.tenant)
        status = "COPAY " + config.environment + " configured"
    except CopayConfigurationError:
        status = "COPAY is not configured. Ask your platform administrator to complete the merchant settings."
    orders = PaymentOrder.objects.select_related("subscription__member")
    page = Paginator(orders, 25).get_page(request.GET.get("page"))
    return render(request, "payments/list.html", {"page": page, "gateway_status": status, "central_collections": settings.CENTRAL_COLLECTIONS_ENABLED})


@owner_page
@require_POST
def issue(request, subscription_id):
    subscription = get_object_or_404(MemberSubscription, pk=subscription_id)
    try:
        order = create_order(ready_database(request.tenant), request.tenant, subscription.pk)
    except CopayError as error:
        messages.error(request, str(error))
        return redirect("member-detail", member_id=subscription.member_id)
    return redirect("payment-detail", order_id=order.pk)


@payment_page
def detail(request, order_id):
    order = authorized_order(request, order_id)
    return render(request, "payments/detail.html", {"order": order, "owner": request.payment_owner})


@payment_page
@require_POST
def checkout(request, order_id):
    order = authorized_order(request, order_id)
    try:
        client_ip = gateway_client_ip(request)
        order = start_checkout(ready_database(request.tenant), request.tenant, order.pk, client_ip)
        if order.status == "pending" and order.checkout_url:
            from .services import gateway_for_order
            return redirect(gateway_for_order(request.tenant, order).config.validate_url(order.checkout_url))
        messages.info(request, "Checkout was already submitted. Recheck status; a second payment was not created.")
    except (CopayError, ValueError) as error:
        messages.error(request, str(error) if isinstance(error, CopayError) else "Could not determine the client IP address.")
    return redirect("payment-detail", order_id=order.pk)


@payment_page
@require_POST
def recheck(request, order_id):
    order = authorized_order(request, order_id)
    try:
        reconcile(ready_database(request.tenant), request.tenant, order.pk)
    except CopayError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Payment status checked with COPAY.")
    return redirect("payment-detail", order_id=order.pk)


@payment_page
def returned(request, order_id):
    # ReturnUrl query parameters are never evidence of payment.
    order = authorized_order(request, order_id)
    messages.info(request, "Welcome back. Your payment is confirmed by COPAY's server notification. You can also recheck its status below.")
    return redirect("payment-detail", order_id=order.pk)


@payment_page
def receipt(request, order_id):
    order = authorized_order(request, order_id)
    if order.status != "paid":
        raise Http404
    return render(request, "payments/receipt.html", {"order": order})


@csrf_exempt
@require_POST
@never_cache
def notify(request):
    if request.tenant is None:
        raise Http404
    if request.content_type != "application/json" or len(request.body) > 32768:
        return HttpResponse("invalid request", status=400, content_type="text/plain")
    try:
        with gym_database(ready_database(request.tenant)):
            process_callback(ready_database(request.tenant), request.tenant, request.body)
    except CopayConfigurationError:
        return HttpResponse("gateway unavailable", status=503, content_type="text/plain")
    except CopayError:
        # Acknowledge only verified, reconciled callbacks. No secrets or payloads in logs.
        return HttpResponse("not confirmed", status=400, content_type="text/plain")
    except (GymDatabaseUnavailable, DatabaseError, OSError):
        return HttpResponse("temporarily unavailable", status=503, content_type="text/plain")
    return HttpResponse("success", content_type="text/plain")
