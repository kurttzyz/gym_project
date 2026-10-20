from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path
from platform_core import views
from gyms.forms import PortalAuthenticationForm

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("accounts/login/", auth_views.LoginView.as_view(template_name="gyms/login.html", authentication_form=PortalAuthenticationForm), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("gyms/new/", views.add_gym, name="gym-add"),
    path("gyms/<slug:slug>/owners/new/", views.add_owner, name="owner-add"),
    path("gyms/<slug:slug>/domains/", views.add_domain, name="domain-add"),
    path("gyms/<slug:slug>/", views.gym_detail, name="gym-detail"),
    path("admin/", admin.site.urls),
    path("health/", views.health, name="health"),
    path("api/organizations/", views.organizations, name="organizations"),
]
