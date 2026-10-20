from django.contrib.auth import views as auth_views
from django.urls import path
from gyms import views
from gyms.forms import PortalAuthenticationForm

urlpatterns = [
    path("", views.tenant_dashboard, name="dashboard"),
    path("accounts/login/", auth_views.LoginView.as_view(
        template_name="gyms/login.html", authentication_form=PortalAuthenticationForm
    ), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
]
