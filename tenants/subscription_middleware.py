from django.db import DatabaseError
from django.shortcuts import render
from django.urls import resolve, Resolver404, get_urlconf, set_urlconf
from django.utils.cache import patch_cache_control
from platform_core.billing.services import access_for_gym


RECOVERY_ROUTES = {"login", "logout", "tenant-saas-billing", "tenant-saas-invoice",
                   "copay-notify", "payment-return", "payment-detail", "payment-recheck", "payment-receipt", "pos-receipt"}
PUBLIC_READS = {"home", "about", "memberships", "contact", "contact-thanks", "join-thanks"}
WEBSITE_ROUTES = PUBLIC_READS | {"join", "website-edit"}
ATTENDANCE_ROUTES = {"attendance-scanner", "attendance-history", "attendance-checkout", "member-card",
                    "member-qr", "member-attendance", "owner-member-card", "owner-member-qr", "member-card-replace",
                    "attendance-locations", "attendance-location-edit", "attendance-staff", "attendance-staff-access"}
PAYMENT_ROUTES = {"owner-payments", "payment-issue", "payment-checkout"}

POS_ROUTES = {"pos-checkout", "pos-confirm", "pos-sales", "pos-products", "pos-product-new", "pos-product-edit",
              "pos-inventory", "pos-adjustment", "pos-refund", "pos-report"}
SHARED_OPERATIONS_ROUTES = {"attendance-locations", "attendance-location-edit", "attendance-staff", "attendance-staff-access"}


class TenantSubscriptionMiddleware:
    """Central control-plane policy, checked before any tenant view runs."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.tenant is None:
            return self.get_response(request)
        try:
            name = resolve(request.path_info, urlconf="gym_core.urls").url_name
        except Resolver404:
            return self.get_response(request)
        if name in RECOVERY_ROUTES:
            return self.get_response(request)
        try:
            access = access_for_gym(request.tenant)
        except DatabaseError:
            return self.block(request)
        request.saas_access = access
        if name == "owner-dashboard":
            return self.get_response(request) if access.allowed else self.block(request)
        if name in SHARED_OPERATIONS_ROUTES:
            if not access.allowed:
                return self.block(request)
            if not (access.modules.get("attendance", False) or access.modules.get("pos", False) or access.modules.get("memberships", False)):
                return self.block(request, module=True)
            return self.get_response(request)
        module = ("pos" if name in POS_ROUTES else "websites" if name in WEBSITE_ROUTES else "attendance" if name in ATTENDANCE_ROUTES
                  else "payments" if name in PAYMENT_ROUTES else "memberships")
        if name == "join" and not access.modules.get("memberships", False):
            return self.block(request, module=True)
        if not access.modules.get(module, False):
            return self.block(request, module=True)
        if not access.allowed and not (name in PUBLIC_READS and request.method in {"GET", "HEAD"}):
            return self.block(request)
        return self.get_response(request)

    def block(self, request, module=False):
        request.platform_billing_only = True
        # This response short-circuits the handler before it selects request.urlconf.
        # Scope template URL reversing explicitly, then restore the thread context.
        previous = get_urlconf()
        try:
            set_urlconf(request.urlconf)
            response = render(request, "platform_billing/unavailable.html", {"module_unavailable": module}, status=503)
        finally:
            set_urlconf(previous)
        patch_cache_control(response, no_store=True, private=True)
        response["Retry-After"] = "3600"
        return response
