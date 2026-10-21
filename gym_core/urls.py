from django.contrib.auth import views as auth_views
from django.urls import path
from websites import views as website_views
from websites.public_pages import views as public_views
from .memberships import views as membership_views
from . import views
from .members import views as member_views
from .auth_views import GymLoginView
from payments import views as payment_views
from .attendance import views as attendance_views
from platform_core.billing import views as billing_views

urlpatterns = [
    path("billing/", billing_views.tenant_billing, name="tenant-saas-billing"),
    path("billing/invoices/<uuid:invoice_id>/", billing_views.tenant_invoice, name="tenant-saas-invoice"),
    path("desk/", attendance_views.scanner, name="attendance-scanner"),
    path("desk/attendance/", attendance_views.history, name="attendance-history"),
    path("desk/attendance/<uuid:visit_id>/checkout/", attendance_views.checkout_visit, name="attendance-checkout"),
    path("portal/card/", attendance_views.my_card, name="member-card"),
    path("portal/card.svg", attendance_views.my_qr, name="member-qr"),
    path("portal/attendance/", attendance_views.my_history, name="member-attendance"),
    path("manage/members/<uuid:member_id>/card/", attendance_views.owner_card, name="owner-member-card"),
    path("manage/members/<uuid:member_id>/card.svg", attendance_views.owner_qr, name="owner-member-qr"),
    path("manage/members/<uuid:member_id>/card/replace/", attendance_views.replace_card, name="member-card-replace"),
    path("manage/branches/", attendance_views.locations, name="attendance-locations"),
    path("manage/branches/<int:location_id>/", attendance_views.locations, name="attendance-location-edit"),
    path("manage/staff/", attendance_views.staff, name="attendance-staff"),
    path("manage/staff/<int:access_id>/access/", attendance_views.staff_access, name="attendance-staff-access"),
    path("manage/payments/", payment_views.owner_payments, name="owner-payments"),
    path("payments/copay/notify/", payment_views.notify, name="copay-notify"),
    path("manage/payments/<uuid:subscription_id>/issue/", payment_views.issue, name="payment-issue"),
    path("payments/<uuid:order_id>/", payment_views.detail, name="payment-detail"),
    path("payments/<uuid:order_id>/checkout/", payment_views.checkout, name="payment-checkout"),
    path("payments/<uuid:order_id>/recheck/", payment_views.recheck, name="payment-recheck"),
    path("payments/<uuid:order_id>/return/", payment_views.returned, name="payment-return"),
    path("payments/<uuid:order_id>/receipt/", payment_views.receipt, name="payment-receipt"),
    path("", public_views.home, name="home"),
    path("about/", public_views.about, name="about"),
    path("memberships/", public_views.memberships, name="memberships"),
    path("join/", public_views.join, name="join"),
    path("join/thanks/", public_views.join_thanks, name="join-thanks"),
    path("contact/", public_views.contact, name="contact"),
    path("contact/thanks/", public_views.contact_thanks, name="contact-thanks"),
    path("activate/<str:token>/", member_views.activate_account, name="member-activate"),
    path("manage/members/", member_views.members, name="owner-members"),
    path("manage/members/new/", member_views.edit_member, name="member-new"),
    path("manage/members/<uuid:member_id>/", member_views.detail, name="member-detail"),
    path("manage/members/<uuid:member_id>/edit/", member_views.edit_member, name="member-edit"),
    path("manage/members/<uuid:member_id>/invite/", member_views.invite, name="member-invite"),
    path("manage/members/<uuid:member_id>/subscriptions/", member_views.add_subscription, name="member-subscription-new"),
    path("manage/members/<uuid:member_id>/subscriptions/<uuid:subscription_id>/action/", member_views.subscription_action, name="member-subscription-action"),
    path("manage/applications/<uuid:application_id>/enroll/", member_views.enroll, name="application-enroll"),
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
