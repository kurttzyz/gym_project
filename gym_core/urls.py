from django.contrib.auth import views as auth_views
from django.urls import path
from websites import views as website_views
from . import views
from .auth_views import GymLoginView

urlpatterns = [
    path("", website_views.homepage, name="home"),
    path("portal/", views.tenant_dashboard, name="dashboard"),
    path("manage/", views.owner_dashboard, name="owner-dashboard"),
    path("manage/website/", website_views.edit_website, name="website-edit"),
    path("accounts/login/", GymLoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
]
