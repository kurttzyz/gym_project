import hashlib
import uuid
from django.core import signing
from django.db import transaction
from django.utils import timezone
from tenant_data.models import Location
from gym_core.members.models import MemberProfile
from gym_core.memberships.models import MemberSubscription
from .models import AttendanceScan, AttendanceVisit


class AttendanceError(Exception):
    pass


def card_code(gym, member):
    return "GYM1:" + signing.dumps({"g": gym.database_key.hex, "m": member.id.hex, "v": member.qr_version.hex},
                                   salt="gym.attendance.card.v1." + gym.database_key.hex)


def card_member(gym, code):
    if not isinstance(code, str) or len(code) > 800 or not code.startswith("GYM1:"):
        return None
    try:
        data = signing.loads(code[5:], salt="gym.attendance.card.v1." + gym.database_key.hex)
        if not isinstance(data, dict) or data.get("g") != gym.database_key.hex:
            return None
        member_id, version = uuid.UUID(data["m"]), uuid.UUID(data["v"])
    except (signing.BadSignature, ValueError, KeyError, TypeError, AttributeError):
        return None
    return MemberProfile.objects.filter(pk=member_id, qr_version=version).first()


def rotate_card(alias, member_id):
    with transaction.atomic(using=alias):
        member = MemberProfile.objects.select_for_update().get(pk=member_id)
        member.qr_version = uuid.uuid4()
        member.save(update_fields=["qr_version"])


def record_scan(alias, gym, code, location_id, action, request_id, actor_id):
    if action not in {"checkin", "checkout"}:
        raise AttendanceError("Choose check-in or check-out.")
    fingerprint = hashlib.sha256(f"{actor_id}|{location_id}|{action}|{code}".encode()).hexdigest()
    with transaction.atomic(using=alias):
        # Branch locking also serializes invalid-card retries and branch disabling.
        branch = Location.objects.select_for_update().get(pk=location_id)
        previous = AttendanceScan.objects.filter(pk=request_id).first()
        if previous:
            if previous.fingerprint != fingerprint:
                raise AttendanceError("This scan request has already been used. Start a new scan.")
            previous.replayed = True
            return previous
        member = card_member(gym, code)
        if member:
            version = member.qr_version
            member = MemberProfile.objects.select_for_update().get(pk=member.pk)
            # Card replacement may have completed after the first lookup.
            if member.qr_version != version:
                member = None
        outcome, reason, visit = "denied", "Invalid or replaced card. Ask the member to show their current gym card.", None
        if member:
            visit = AttendanceVisit.objects.select_for_update().filter(member=member, checked_out_at__isnull=True).first()
            if action == "checkout":
                if not visit:
                    outcome, reason = "not_in", "No open visit to check out."
                elif visit.location_id != branch.pk:
                    reason = "This member is checked in at another branch. Select that branch to check out."
                else:
                    visit.checked_out_at, visit.checked_out_by_id = timezone.now(), actor_id
                    visit.save(update_fields=["checked_out_at", "checked_out_by_id"])
                    outcome, reason = "checked_out", "Checked out successfully."
            elif not branch.is_active:
                reason = "This branch is disabled for entry."
            elif not member.is_active:
                reason = "Member access is disabled."
            else:
                today = timezone.localdate()
                term = MemberSubscription.objects.filter(member=member, status="active", starts_on__lte=today, expires_on__gt=today).first()
                if not term:
                    reason = "No active membership for today. Contact the gym owner."
                elif visit:
                    outcome = "already_in"
                    reason = "Already checked in." if visit.location_id == branch.pk else "Already checked in at another branch."
                else:
                    visit = AttendanceVisit.objects.create(member=member, subscription=term, location=branch, checked_in_by_id=actor_id)
                    outcome, reason = "checked_in", "Entry allowed. Check-in recorded."
        return AttendanceScan.objects.create(id=request_id, fingerprint=fingerprint, actor_id=actor_id,
            location=branch, action=action, member=member, visit=visit, outcome=outcome, reason=reason)


def close_visit(alias, visit_id, actor_id, note):
    if not note.strip():
        raise AttendanceError("A reason is required to close a visit without scanning a card.")
    preview = AttendanceVisit.objects.get(pk=visit_id)
    with transaction.atomic(using=alias):
        MemberProfile.objects.select_for_update().get(pk=preview.member_id)
        visit = AttendanceVisit.objects.select_for_update().get(pk=visit_id)
        if visit.checked_out_at:
            return visit
        visit.checked_out_at, visit.checked_out_by_id = timezone.now(), actor_id
        visit.checkout_note = note.strip()
        visit.save(update_fields=["checked_out_at", "checked_out_by_id", "checkout_note"])
        return visit
