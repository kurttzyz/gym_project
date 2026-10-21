"""Central-only hosting control plane. The web process never invokes sudo."""
from datetime import timedelta
import json
import socket

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .domains import is_local_hostname
from .models import GymDomain


def call_agent(domain):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(240)
        client.connect(settings.DOMAIN_HOSTING_SOCKET)
        client.sendall((json.dumps({"hostname": domain.hostname, "token": domain.verification_token}) + "\n").encode())
        with client.makefile("rb") as response:
            raw = response.readline(4097)
        if len(raw) > 4096:
            raise ValueError("Invalid agent response")
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("status") not in {"live", "waiting", "failed"}:
            raise ValueError("Invalid agent response")
        return result


def process_domain(domain_id):
    if not settings.DOMAIN_HOSTING_ENABLED:
        return
    now = timezone.now()
    with transaction.atomic(using="default"):
        domain = GymDomain.objects.select_for_update().get(pk=domain_id)
        if (not domain.is_active or is_local_hostname(domain.hostname)
                or domain.hosting_status in {"live", "failed"}
                or domain.hosting_next_attempt and domain.hosting_next_attempt > now):
            return
        # Durable lease survives worker failure; never hold a DB lock during ACME.
        domain.hosting_status = "provisioning"
        domain.hosting_next_attempt = now + timedelta(minutes=10)
        domain.hosting_message = "Checking DNS and preparing HTTPS."
        domain.save(update_fields=["hosting_status", "hosting_next_attempt", "hosting_message"])
    lease = domain.hosting_next_attempt
    try:
        result = call_agent(domain)
        status = result["status"]
        # Agent sends only fixed, public operational messages, never command output.
        message = str(result.get("message", ""))[:300]
    except (OSError, ValueError, TypeError):
        status, message = "failed", "Hosting agent unavailable or timed out. Ask the platform operator to check its service, then retry."
    changes = dict(hosting_status=status, hosting_message=message, hosting_checked_at=timezone.now(),
                   hosting_next_attempt=timezone.now() + timedelta(minutes=5) if status == "waiting" else None)
    if status == "live":
        changes["verified_at"] = timezone.now()
    GymDomain.objects.filter(pk=domain_id, hosting_next_attempt=lease, hosting_status="provisioning").update(**changes)


@shared_task(ignore_result=True)
def sweep_domains():
    if not settings.DOMAIN_HOSTING_ENABLED:
        return
    ids = list(GymDomain.objects.filter(is_active=True).exclude(
        hosting_status__in=["live", "failed"]).filter(
        Q(hosting_next_attempt__isnull=True) | Q(hosting_next_attempt__lte=timezone.now())
    ).exclude(hostname__endswith=".localhost").exclude(hostname__endswith=".test").order_by(
        "hosting_next_attempt", "pk").values_list("pk", flat=True)[:5])
    for domain_id in ids:
        process_domain(domain_id)
