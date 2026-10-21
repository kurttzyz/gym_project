from gyms.databases import GymDatabaseUnavailable
from gyms.models import Membership
from .services import website_for_request


def branding(request):
    gym = getattr(request, "tenant", None)
    site = None
    if gym and not getattr(request, "platform_billing_only", False):
        try:
            site = website_for_request(request)
        except GymDatabaseUnavailable:
            pass
    owner = bool(gym and request.user.is_authenticated and Membership.objects.filter(
        organization=gym, user=request.user, role=Membership.Role.OWNER, is_active=True
    ).exists())
    front_desk = bool(gym and request.user.is_authenticated and Membership.objects.filter(
        organization=gym, user=request.user, role__in={"owner", "manager", "staff"}, is_active=True).exists())
    return {
        "tenant": gym,
        "branding_name": site.display_name if site else (gym.name if gym else "Powerhouse"),
        "branding_color": site.primary_color if site else (gym.primary_color if gym else "#245ac5"),
        "branding_logo": site.logo_url if site else (gym.logo_url if gym else ""),
        "is_gym_owner": owner,
        "is_front_desk": front_desk,
        "is_pos_staff": bool(front_desk and getattr(request, "saas_access", None)
                             and request.saas_access.allowed and request.saas_access.modules.get("pos", False)),
    }
