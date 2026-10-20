from django.contrib.auth import views as auth_views
from django.urls import path
from websites import views as website_views
from websites.public_pages import views as public_views
from .memberships import views as membership_views
from . import views
from .auth_views import GymLoginView

urlpatterns = [
    path("", public_views.home, name="home"),
    path("about/", public_views.about, name="about"),
    path("memberships/", public_views.memberships, name="memberships"),
    path("join/", public_views.join, name="join"),
    path("join/thanks/", public_views.join_thanks, name="join-thanks"),
    path("contact/", public_views.contact, name="contact"),
    path("contact/thanks/", public_views.contact_thanks, name="contact-thanks"),
    path("portal/", views.tenant_dashboard, name="dashboard"),
    path("manage/", views.owner_dashboard, name="owner-dashboard"),
    path("manage/plans/", membership_views.plans, name="owner-plans"),
    path("manage/plans/new/", membership_views.edit_plan, name="owner-plan-new"),
    path("manage/plans/<uuid:plan_id>/", membership_views.edit_plan, name="owner-plan-edit"),
    path("manage/applications/", membership_views.applications, name="owner-applications"),
    path("manage/applications/<uuid:application_id>/review/", membership_views.review_application, name="owner-application-review"),
    path("manage/inbox/", membership_views.inbox, name="owner-inbox"),
    path("manage/inbox/<int:message_id>/read/", membership_views.mark_read, name="owner-message-read"),
    path("manage/website/", website_views.edit_website, name="website-edit"),
    path("accounts/login/", GymLoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
]
