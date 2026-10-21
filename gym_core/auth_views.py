from django.contrib.auth.views import LoginView
from django.urls import reverse
from gyms.forms import PortalAuthenticationForm
from gyms.models import Membership


class GymLoginView(LoginView):
    template_name = "gyms/login.html"
    authentication_form = PortalAuthenticationForm

    def get_default_redirect_url(self):
        owner = Membership.objects.filter(
            organization=self.request.tenant, user=self.request.user,
            role=Membership.Role.OWNER, is_active=True,
        ).exists()
        if owner:
            return reverse("owner-dashboard")
        front_desk = Membership.objects.filter(organization=self.request.tenant, user=self.request.user,
            role__in={"manager", "staff"}, is_active=True).exists()
        if front_desk:
            from platform_core.billing.services import access_for_gym
            access = access_for_gym(self.request.tenant)
            if access.allowed and not access.modules.get("attendance", False) and access.modules.get("pos", False):
                return reverse("pos-checkout")
            return reverse("attendance-scanner")
        return reverse("dashboard")
