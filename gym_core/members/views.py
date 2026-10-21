from django.contrib import messages
from django.contrib.auth import login
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST
from gyms.databases import ready_database
from gym_core.memberships.views import owner_page
from gym_core.memberships.models import MemberSubscription, MembershipApplication
from websites.public_pages.views import public_page
from .forms import (AssignPlanForm, ExistingMemberAccountForm, MemberAccountForm,
                    MemberProfileForm, SubscriptionActionForm, waiver_version)
from .models import MemberInvitation, MemberProfile
from .services import (EnrollmentError, assign_plan, change_subscription, claim_invitation,
                       enroll_application, invitation_for_token, issue_invitation)


@owner_page
def members(request):
    query = request.GET.get("q", "").strip()[:160]
    profiles = MemberProfile.objects.all()
    if query:
        profiles = profiles.filter(Q(full_name__icontains=query) | Q(email__icontains=query) |
                                   Q(member_number__icontains=query) | Q(phone__icontains=query))
    page = Paginator(profiles, 25).get_page(request.GET.get("page"))
    return render(request, "members/list.html", {"page": page, "query": query})


@owner_page
def edit_member(request, member_id=None):
    member = get_object_or_404(MemberProfile, pk=member_id) if member_id else None
    form = MemberProfileForm(request.POST if request.method == "POST" else None, instance=member)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic(using=ready_database(request.tenant)):
            if member is None:
                member = form.save()
            else:
                current = MemberProfile.objects.select_for_update().get(pk=member.pk)
                updated = form.save(commit=False)
                # Preserve account links and waiver evidence if an invitation was
                # claimed after this edit form originally loaded its instance.
                updated.save(update_fields=list(MemberProfileForm.Meta.fields))
                if current.email.casefold() != updated.email.casefold() or not updated.is_active:
                    MemberInvitation.objects.filter(member=updated, used_at__isnull=True, revoked_at__isnull=True).update(revoked_at=timezone.now())
                member = updated
        return redirect("member-detail", member_id=member.pk)
    return render(request, "members/edit.html", {"form": form, "member": member})


def member_context(member, plan_form=None):
    return {"member": member,
            "subscriptions": member.subscriptions.select_related("member", "payment").prefetch_related("events"),
            "plan_form": plan_form if plan_form is not None else AssignPlanForm()}


@owner_page
def detail(request, member_id):
    member = get_object_or_404(MemberProfile, pk=member_id)
    return render(request, "members/detail.html", member_context(member))


@owner_page
@require_POST
def enroll(request, application_id):
    get_object_or_404(MembershipApplication, pk=application_id)
    try:
        member, _ = enroll_application(ready_database(request.tenant), application_id, request.user.pk)
    except EnrollmentError as error:
        messages.error(request, str(error))
        return redirect("owner-applications")
    messages.success(request, "Member enrolled. Their membership is pending activation; no payment was recorded.")
    return redirect("member-detail", member_id=member.pk)


@owner_page
@require_POST
def add_subscription(request, member_id):
    member = get_object_or_404(MemberProfile, pk=member_id)
    form = AssignPlanForm(request.POST)
    if form.is_valid():
        try:
            assign_plan(ready_database(request.tenant), member.pk, form.cleaned_data["plan"].pk,
                        form.cleaned_data["starts_on"], request.user.pk)
        except EnrollmentError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, "Membership term assigned, pending activation.")
            return redirect("member-detail", member_id=member.pk)
    return render(request, "members/detail.html", member_context(member, form), status=400)


@owner_page
@require_POST
def subscription_action(request, member_id, subscription_id):
    get_object_or_404(MemberProfile, pk=member_id)
    get_object_or_404(MemberSubscription, pk=subscription_id, member_id=member_id)
    form = SubscriptionActionForm(request.POST)
    if form.is_valid():
        try:
            change_subscription(ready_database(request.tenant), member_id, subscription_id,
                                form.cleaned_data["action"], form.cleaned_data["note"], request.user.pk)
        except EnrollmentError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, "Membership updated. This does not verify or collect payment.")
    else:
        messages.error(request, "Choose a valid action and provide a reason (maximum 1,000 characters).")
    return redirect("member-detail", member_id=member_id)


@owner_page
@require_POST
@never_cache
def invite(request, member_id):
    member = get_object_or_404(MemberProfile, pk=member_id)
    try:
        token = issue_invitation(ready_database(request.tenant), member.pk, request.user.pk)
    except EnrollmentError as error:
        messages.error(request, str(error))
        return redirect("member-detail", member_id=member.pk)
    response = render(request, "members/invitation.html", {
        "member": member, "activation_url": request.build_absolute_uri(reverse("member-activate", args=[token]))
    })
    response["Referrer-Policy"] = "no-referrer"
    return response


@never_cache
@sensitive_post_parameters("new-password1", "new-password2", "existing-password")
@public_page
def activate_account(request, token):
    try:
        invitation = invitation_for_token(token)
    except EnrollmentError:
        raise Http404 from None
    waiver = request.website.waiver_text
    mode = request.POST.get("mode", "new")
    new_form = MemberAccountForm(request.POST if request.method == "POST" and mode == "new" else None,
                                 prefix="new", waiver_text=waiver)
    new_form.instance.email = invitation.member.email
    existing_form = ExistingMemberAccountForm(request,
        data=request.POST if request.method == "POST" and mode == "existing" else None,
        prefix="existing", waiver_text=waiver)
    if request.method == "POST":
        if mode not in {"new", "existing"}:
            raise Http404
        form = existing_form if mode == "existing" else new_form
        if form.is_valid():
            try:
                user = claim_invitation(ready_database(request.tenant), request.tenant, token, form,
                                        existing=mode == "existing", waiver_text=waiver)
            except (EnrollmentError, IntegrityError) as error:
                form.add_error(None, str(error) if isinstance(error, EnrollmentError) else
                               "Could not create this account. Try signing in with an existing account instead.")
            else:
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                return redirect("dashboard")
        if "waiver_version" in form.errors:
            data = form.data.copy()
            data[f"{form.prefix}-waiver_version"] = waiver_version(waiver)
            data.pop(f"{form.prefix}-accept_waiver", None)
            form.data = data
    response = render(request, "members/activate.html", {
        "site": request.website, "member": invitation.member, "new_form": new_form,
        "existing_form": existing_form, "waiver": waiver,
    })
    response["Referrer-Policy"] = "no-referrer"
    return response
