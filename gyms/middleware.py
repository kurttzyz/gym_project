from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.http import HttpResponseNotFound
from django.http.request import split_domain_port
from .models import GymDomain


class TenantHostMiddleware:
    """Validate every Host against the platform list or an active verified domain."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            host, _ = split_domain_port(request.get_host().lower())
        except DisallowedHost:
            return HttpResponseNotFound("Unknown host")
        request.tenant = None
        if host in settings.PLATFORM_HOSTS:
            request.urlconf = "config.urls"
        else:
            domain = GymDomain.objects.select_related("organization").filter(
                hostname=host, verified_at__isnull=False, is_active=True
            ).first()
            if domain is None:
                return HttpResponseNotFound("Unknown host")
            request.tenant = domain.organization
            request.urlconf = "config.tenant_urls"
        return self.get_response(request)
