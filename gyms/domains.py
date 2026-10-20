"""Domain activation shared by the dashboard and optional operator commands."""
import secrets

import dns.exception
import dns.resolver
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http.request import split_domain_port
from django.utils import timezone

from .models import GymDomain, normalize_hostname


class DomainVerificationError(Exception):
    pass


def is_local_hostname(hostname):
    return hostname.endswith((".localhost", ".test"))


def local_portals_enabled(request):
    host, _ = split_domain_port(request.get_host().lower())
    return settings.DEBUG and host in {"localhost", "127.0.0.1", "[::1]"}


def verify_domain(domain, *, local=False):
    if not domain.is_active:
        raise DomainVerificationError("Domain is disabled.")
    if local:
        if not settings.DEBUG or not is_local_hostname(domain.hostname):
            raise DomainVerificationError("Local activation requires DEBUG and a .test/.localhost hostname.")
    else:
        if is_local_hostname(domain.hostname):
            raise DomainVerificationError("Local domains can only be activated in development.")
        try:
            answers = dns.resolver.resolve(
                f"_powerhouse-verification.{domain.hostname}.", "TXT", lifetime=5
            )
            tokens = [b"".join(record.strings).decode("ascii") for record in answers]
        except (dns.exception.DNSException, UnicodeError) as error:
            raise DomainVerificationError("Could not read the DNS TXT record. Check DNS and try again.") from error
        if not any(secrets.compare_digest(token, domain.verification_token) for token in tokens):
            raise DomainVerificationError("The DNS TXT record does not match. Check the value and try again.")
    domain.verified_at = timezone.now()
    domain.save(update_fields=["verified_at"])


def ensure_local_portal(gym):
    if not settings.DEBUG:
        raise DomainVerificationError("Local portals are available only in development.")
    label = gym.slug.replace("_", "-")[:60].strip("-") or "gym"
    candidates = (f"{label}.localhost", f"gym-{gym.database_key.hex}.localhost")
    for hostname in candidates:
        try:
            hostname = normalize_hostname(hostname)
        except ValidationError:
            continue
        domain, _ = GymDomain.objects.get_or_create(
            hostname=hostname, defaults={"organization": gym}
        )
        if domain.organization_id != gym.pk:
            continue
        # Preserve an operator's explicit decision to disable a domain.
        if not domain.is_active:
            raise DomainVerificationError("The gym's local domain is disabled. Re-enable it in platform administration.")
        if domain.verified_at is None:
            verify_domain(domain, local=True)
        return domain
    raise DomainVerificationError("Could not allocate a local portal hostname.")


def portal_links(request, domain):
    if not domain.is_active or domain.verified_at is None:
        return None
    if is_local_hostname(domain.hostname):
        if not local_portals_enabled(request):
            return None
        _, port = split_domain_port(request.get_host().lower())
        origin = f"{request.scheme}://{domain.hostname}" + (f":{port}" if port else "")
    else:
        origin = f"https://{domain.hostname}"
    return {"website": origin + "/", "login": origin + "/accounts/login/",
            "owner": origin + "/manage/"}
