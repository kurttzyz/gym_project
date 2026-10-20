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
        return reverse("owner-dashboard" if owner else "dashboard")
