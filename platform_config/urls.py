from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path
from platform_core import views
from platform_core.billing import views as billing_views
from gyms.forms import PortalAuthenticationForm

urlpatterns = [
    path("billing/", billing_views.dashboard, name="saas-dashboard"),
    path("billing/plans/", billing_views.plans, name="saas-plans"),
    path("billing/plans/new/", billing_views.edit_plan, name="saas-plan-new"),
    path("billing/plans/<uuid:plan_id>/", billing_views.edit_plan, name="saas-plan-edit"),
    path("billing/invoices/<uuid:invoice_id>/", billing_views.invoice, name="saas-invoice"),
    path("billing/invoices/<uuid:invoice_id>/payment/", billing_views.confirm_payment, name="saas-payment-record"),
    path("gyms/<slug:slug>/subscription/", billing_views.subscription, name="saas-subscription"),
    path("gyms/<slug:slug>/subscription/plan/", billing_views.update_plan, name="saas-subscription-plan"),
    path("gyms/<slug:slug>/subscription/state/", billing_views.update_state, name="saas-subscription-state"),
    path("", views.dashboard, name="dashboard"),
    path("accounts/login/", auth_views.LoginView.as_view(template_name="gyms/login.html", authentication_form=PortalAuthenticationForm), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("gyms/new/", views.add_gym, name="gym-add"),
    path("gyms/<slug:slug>/owners/new/", views.add_owner, name="owner-add"),
    path("gyms/<slug:slug>/local-portal/", views.enable_local_portal, name="local-portal"),
    path("gyms/<slug:slug>/retry-setup/", views.retry_gym_setup, name="gym-retry"),
    path("gyms/<slug:slug>/domains/<int:domain_id>/verify/", views.verify_gym_domain, name="domain-verify"),
    path("gyms/<slug:slug>/domains/", views.add_domain, name="domain-add"),
    path("gyms/<slug:slug>/", views.gym_detail, name="gym-detail"),
    path("admin/", admin.site.urls),
    path("health/", views.health, name="health"),
    path("api/organizations/", views.organizations, name="organizations"),
]
