"""Real PostgreSQL integration suite, isolated from Django's test database aliases."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "platform_config.settings")
import django
django.setup()
import psycopg
from psycopg import sql
from django.conf import settings

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection, connections, DatabaseError, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, override_settings
from config.database_router import gym_database
from gyms.databases import create_database, provision_database, ready_database, register_database
from gyms.models import Organization, Membership, GymDomain
from django.utils import timezone
from django.core.management.base import CommandError
from django.core.exceptions import ValidationError
from types import SimpleNamespace
from tenant_data.models import Location
from websites.models import ContactMessage, WebsiteSettings
from gym_core.memberships.models import MembershipApplication, MembershipPlan
from decimal import Decimal
from datetime import date, timedelta
import re
from gym_core.members.models import MemberProfile, MemberInvitation
from gym_core.memberships.models import MemberSubscription, MembershipEvent
from gym_core.members.services import issue_invitation, end_of_term, assign_plan, EnrollmentError
from gym_core.members.forms import waiver_version
from payments.models import PaymentOrder
from payments.services import create_order, start_checkout, reconcile, process_callback
from payments.gateway_adapters.copay import CopayConfig, CopayError, CopayConfigurationError, CopayGateway, config_for_gym
from cryptography.hazmat.primitives.asymmetric import rsa, padding as rsa_padding
from cryptography.hazmat.primitives import hashes, serialization
import base64
import json
import tempfile
import hashlib
from gym_core.attendance.models import AttendanceVisit, AttendanceScan
from gym_core.attendance.services import card_code, record_scan, rotate_card
from concurrent.futures import ThreadPoolExecutor
from pos.models import Product, StockBalance, StockMovement, Sale, SaleLine, SaleRefund
from pos.services import quote_cart, commit_sale, adjust_stock, refund_sale, PosError
import time
from platform_core.models import SaaSPlan, GymSubscription, PlatformInvoice, PlatformPayment, SubscriptionEvent
from platform_core.billing.services import (assign_plan as assign_saas_plan, record_payment as record_saas_payment,
    change_plan as change_saas_plan, change_state as change_saas_state, access_for_gym, BillingError)
from websites.forms import WebsiteSettingsForm
from django.forms.models import model_to_dict


from gym_core.self_service.models import RenewalRequest, MemberProfileChange
from gym_core.self_service.services import request_renewal, review_renewal, cancel_renewal
from accounts.models import PasswordRecovery, RecoveryThrottle
from accounts.recovery import queue_recovery, deliver_recovery, token_generator
from gym_core.notifications.models import NotificationSettings, EmailNotification, EmailAttempt
from gym_core.notifications.services import queue_email as queue_notice
from gym_core.notifications.delivery import deliver_batch as deliver_email_batch
from gym_core.notifications.reminders import queue_reminders as queue_email_reminders
from gym_core.classes.models import Trainer, ClassSession, ClassBooking, BookingEvent
from gym_core.classes.services import (create_session as create_class, book as book_class,
    cancel_booking as cancel_class_booking, cancel_session as cancel_class_session, BookingError)

class GymDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        central = settings.DATABASES["default"]
        if not central["NAME"].startswith("test_gym_platform_"):
            raise RuntimeError("Integration suite requires an isolated test database")
        create_database(central["NAME"], central["USER"], central["PASSWORD"])
        # Exercise upgrade from the previous shared-table schema with real data.
        executor = MigrationExecutor(connection)
        executor.migrate([("gyms", "0001_initial")])
        old = executor.loader.project_state([("gyms", "0001_initial")]).apps
        Org = old.get_model("gyms", "Organization")
        Branch = old.get_model("gyms", "Location")
        for index in range(2):
            gym = Org.objects.create(name=f"Legacy {index}", slug=f"legacy-{index}")
            Branch.objects.create(organization=gym, name="Legacy branch", address=str(index))
        call_command("migrate", interactive=False, verbosity=0)

    def setUp(self):
        token = uuid4().hex
        self.user = get_user_model().objects.create_user(username=token, password="test-password")
        self.a = Organization.objects.create(name="Gym A", slug=f"a-{token}")
        self.b = Organization.objects.create(name="Gym B", slug=f"b-{token}")
        self.alias_a = provision_database(self.a)
        self.alias_b = provision_database(self.b)
        Membership.objects.create(user=self.user, organization=self.a, role="owner")
        with gym_database(self.alias_a):
            self.branch_a = Location.objects.create(name="Main", address="Address A")
        with gym_database(self.alias_b):
            self.branch_b = Location.objects.create(name="Main", address="Private address B")
        self.client = Client(HTTP_HOST="localhost")
        self.client.force_login(self.user)
        self.domain_a = GymDomain.objects.create(
            organization=self.a, hostname=f"a-{token}.test", verified_at=timezone.now()
        )
        self.domain_b = GymDomain.objects.create(
            organization=self.b, hostname=f"b-{token}.test", verified_at=timezone.now()
        )

    def tearDown(self):
        # Each scenario owns fresh gym databases; release sockets before the next
        # scenario rather than exhausting PostgreSQL's server-wide connection cap.
        connections.close_all()

    def tenant_client(self, domain=None, csrf=False):
        client = Client(HTTP_HOST=(domain or self.domain_a).hostname, enforce_csrf_checks=csrf)
        client.force_login(self.user)
        return client

    def owner_payload(self):
        token = uuid4().hex
        return {
            "owner-username": f"owner-{token}", "owner-email": f"owner-{token}@example.com",
            "owner-password1": "GymOwner-Strong-84!", "owner-password2": "GymOwner-Strong-84!",
        }

    def website_payload(self, **updates):
        site = WebsiteSettings.objects.using(self.alias_a).get(pk=1)
        data = model_to_dict(site, fields=WebsiteSettingsForm._meta.fields)
        data.update(updates)
        return data

    def plan(self, alias=None, **updates):
        data = {"name": "Standard membership", "price": Decimal("1250.50"),
                "period": "month", "features": "Gym access\nPersonal training enquiries"}
        data.update(updates)
        return MembershipPlan.objects.using(alias or self.alias_a).create(**data)

    def application_payload(self, plan, **updates):
        data = {"plan": str(plan.pk), "full_name": "Jamie Applicant", "email": "jamie@example.com",
                "phone": "09170000000", "message": "I would like to get started.", "consent": "on"}
        data.update(updates)
        return data

    def enrolled_member(self, **updates):
        plan = self.plan()
        client = Client(HTTP_HOST=self.domain_a.hostname)
        client.post("/join/", self.application_payload(plan, **updates))
        application = MembershipApplication.objects.using(self.alias_a).get()
        self.tenant_client().post(f"/manage/applications/{application.pk}/review/", {"status": "approved"})
        response = self.tenant_client().post(f"/manage/applications/{application.pk}/enroll/")
        self.assertEqual(response.status_code, 302)
        subscription = MemberSubscription.objects.using(self.alias_a).select_related("member", "application").get(application=application)
        return subscription.member, subscription

    def invitation_token(self, member):
        with gym_database(self.alias_a):
            return issue_invitation(self.alias_a, member.pk, self.user.pk)

    def claim_payload(self, username=None, **updates):
        data = {"mode": "new", "new-username": username or "member-" + uuid4().hex,
                "new-password1": "Member-Strong-84!", "new-password2": "Member-Strong-84!"}
        data.update(updates)
        return data

    def membership_action(self, member, subscription, action, note="Offline arrangement confirmed by owner"):
        return self.tenant_client().post(
            f"/manage/members/{member.pk}/subscriptions/{subscription.pk}/action/", {"action": action, "note": note}
        )

    def test_existing_data_migrated(self):
        for index in range(2):
            gym = Organization.objects.get(slug=f"legacy-{index}")
            with gym_database(ready_database(gym)):
                self.assertEqual(Location.objects.get().address, str(index))
                Location.objects.create(name="New branch after transfer")
        self.assertNotIn("gyms_location", connection.introspection.table_names())

    def test_physical_tables_are_separate(self):
        _, name_a = register_database(self.a)
        _, name_b = register_database(self.b)
        self.assertNotEqual(name_a, name_b)
        for alias in (self.alias_a, self.alias_b):
            with connections[alias].cursor() as cursor:
                cursor.execute("SELECT current_database(), current_user")
                self.assertEqual(cursor.fetchone(), (alias, alias))
        self.assertNotIn("tenant_data_location", connection.introspection.table_names())
        self.assertNotIn("websites_websitesettings", connection.introspection.table_names())
        self.assertNotIn("gym_core_membershipplan", connection.introspection.table_names())
        self.assertNotIn("gym_core_membershipapplication", connection.introspection.table_names())
        for alias in (self.alias_a, self.alias_b):
            tables = connections[alias].introspection.table_names()
            self.assertIn("tenant_data_location", tables)
            self.assertIn("websites_websitesettings", tables)
            self.assertIn("gym_core_membershipplan", tables)
            self.assertIn("gym_core_membershipapplication", tables)
            self.assertNotIn("accounts_user", tables)
            self.assertNotIn("gyms_membership", tables)

    def test_database_roles_cannot_connect_to_other_databases(self):
        config = connections.databases[self.alias_a]
        for target in (self.alias_b, settings.DATABASES["default"]["NAME"]):
            with self.assertRaises(psycopg.OperationalError):
                psycopg.connect(host=config["HOST"], port=config["PORT"], dbname=target,
                                 user=config["USER"], password=config["PASSWORD"])

    def test_overlapping_ids_and_writes_are_isolated(self):
        self.assertEqual(self.branch_a.pk, self.branch_b.pk)
        with gym_database(self.alias_a):
            Location.objects.filter(pk=self.branch_a.pk).update(address="Changed A")
        with gym_database(self.alias_b):
            self.assertEqual(Location.objects.get(pk=self.branch_b.pk).address, "Private address B")
            with self.assertRaises(IntegrityError):
                Location.objects.create(name="Main")

    def test_context_is_required_and_reset_after_exception(self):
        with self.assertRaises(ImproperlyConfigured):
            list(Location.objects.all())
        with self.assertRaises(ValueError):
            with gym_database(self.alias_a):
                self.assertEqual(Location.objects.count(), 1)
                raise ValueError
        with self.assertRaises(ImproperlyConfigured):
            Location.objects.count()

    def test_unauthorized_gym_is_denied_before_database_lookup(self):
        with patch("gym_core.views.ready_database") as lookup:
            self.assertEqual(self.tenant_client(self.domain_b).get("/portal/").status_code, 404)
            lookup.assert_not_called()
        response = self.tenant_client().get("/portal/")
        self.assertContainsText(response, "Address A")
        self.assertNotIn(b"Private address B", response.content)
        Membership.objects.filter(user=self.user).update(is_active=False)
        self.assertEqual(self.tenant_client().get("/portal/").status_code, 404)

    def assertContainsText(self, response, text):
        self.assertEqual(response.status_code, 200)
        self.assertIn(text.encode(), response.content)

    def test_platform_creation_and_retry_are_repeatable(self):
        self.user.is_superuser = True
        self.user.save()
        slug = uuid4().hex
        response = self.client.post("/gyms/new/", {"name": "New gym", "slug": slug, "primary_color": "#245ac5", **self.owner_payload()})
        self.assertEqual(response.status_code, 302)
        gym = Organization.objects.get(slug=slug)
        alias = ready_database(gym)
        with gym_database(alias):
            Location.objects.create(name="Retained")
        provision_database(gym)
        with gym_database(alias):
            self.assertEqual(Location.objects.get().name, "Retained")
        response = self.client.post("/gyms/new/", {"name": "Duplicate", "slug": slug, "primary_color": "#245ac5", **self.owner_payload()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Organization.objects.filter(slug=slug).count(), 1)

    def test_members_cannot_provision_or_view_other_gym_in_list(self):
        response = self.client.post("/gyms/new/", {"name": "Forbidden", "slug": uuid4().hex})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/organizations/").status_code, 403)
        self.assertEqual(self.client.get("/").status_code, 403)
        self.user.is_staff = True
        self.user.save()
        from django.contrib.auth.models import Permission
        self.user.user_permissions.add(Permission.objects.get(codename="view_organization"))
        self.assertEqual(self.client.get("/platform-admin/gyms/organization/").status_code, 302)

    def test_missing_database_fails_without_recreating(self):
        _, name = register_database(self.a)
        connections[self.alias_a].close()
        with psycopg.connect(**settings.GYM_PROVISIONER, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        self.assertEqual(self.tenant_client().get("/").status_code, 503)
        with psycopg.connect(**settings.GYM_PROVISIONER) as admin:
            self.assertIsNone(admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone())
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("migrate_gym_databases", slug=self.a.slug, verbosity=0)
        with psycopg.connect(**settings.GYM_PROVISIONER) as admin:
            self.assertIsNone(admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone())

    def test_provisioning_failure_can_be_retried(self):
        self.user.is_superuser = True
        self.user.save()
        slug = uuid4().hex
        with patch("platform_core.views.provision_database", side_effect=DatabaseError("unavailable")):
            self.assertEqual(self.client.post("/gyms/new/", {"name": "Retry", "slug": slug, "primary_color": "#245ac5", **self.owner_payload()}).status_code, 302)
        gym = Organization.objects.get(slug=slug)
        self.assertFalse(gym.database_ready)
        call_command("migrate_gym_databases", slug=slug, verbosity=0)
        gym.refresh_from_db()
        self.assertTrue(gym.database_ready)

    def test_login_and_health(self):
        self.client = self.tenant_client()
        self.client.logout()
        self.assertEqual(self.client.get("/portal/").status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(Client(HTTP_HOST="localhost").get("/health/").json(), {"status": "ok"})
        self.assertEqual(self.client.post("/accounts/login/", {
            "username": self.user.username, "password": "test-password"
        }).status_code, 302)
        self.assertContainsText(self.client.get("/"), self.a.name)


    def test_unknown_unverified_disabled_and_forwarded_hosts_are_rejected(self):
        for host in ("unknown.test", "bad/host"):
            self.assertEqual(Client(HTTP_HOST=host).get("/").status_code, 404)
        self.domain_a.verified_at = None
        self.domain_a.save()
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).get("/").status_code, 404)
        self.domain_a.verified_at = timezone.now()
        self.domain_a.is_active = False
        self.domain_a.save()
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).get("/").status_code, 404)
        self.assertEqual(Client(HTTP_HOST="unknown.test", HTTP_X_FORWARDED_HOST="localhost").get("/").status_code, 404)

    def test_tenant_has_no_platform_routes_even_for_superuser(self):
        self.user.is_superuser = True
        self.user.save()
        client = self.tenant_client()
        for path in ("/platform-admin/", "/gyms/new/", f"/gyms/{self.b.slug}/", "/api/organizations/"):
            self.assertEqual(client.get(path).status_code, 404)
        self.assertContainsText(client.get("/portal/"), "Address A")
        self.assertNotIn(b"Private address B", client.get("/portal/").content)

    def test_white_label_login_and_dashboard(self):
        self.a.name = "IronFit independent gym"
        self.a.logo_url = "https://images.example.com/ironfit.png"
        self.a.primary_color = "#aa2233"
        self.a.save()
        WebsiteSettings.objects.using(self.alias_a).filter(pk=1).update(
            display_name=self.a.name, logo_url=self.a.logo_url, primary_color=self.a.primary_color
        )
        client = self.tenant_client()
        client.logout()
        response = client.get("/accounts/login/")
        self.assertContainsText(response, self.a.name)
        self.assertIn(self.a.logo_url.encode(), response.content)
        self.assertIn(b"#aa2233", response.content)
        self.assertNotIn(b"Powerhouse", response.content)
        response = client.post("/accounts/login/", {
            "username": self.user.username, "password": "test-password"
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies[settings.SESSION_COOKIE_NAME]["domain"], "")
        self.assertContainsText(client.get("/"), self.a.name)
        other = Client(HTTP_HOST=self.domain_b.hostname)
        self.assertEqual(other.post("/accounts/login/", {
            "username": self.user.username, "password": "test-password"
        }).status_code, 200)
        self.assertEqual(other.get("/portal/").status_code, 302)

    def test_platform_login_rejects_tenant_account(self):
        self.client.logout()
        self.assertEqual(self.client.post("/accounts/login/", {
            "username": self.user.username, "password": "test-password"
        }).status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 302)
        self.user.is_superuser = True
        self.user.save()
        self.assertEqual(self.client.post("/accounts/login/", {
            "username": self.user.username, "password": "test-password"
        }).status_code, 302)
        self.assertContainsText(self.client.get("/"), "Powerhouse")
        organizations = self.client.get("/api/organizations/").json()["organizations"]
        self.assertIn(self.b.id, [gym["id"] for gym in organizations])

    def test_domain_registration_normalizes_and_rejects_duplicate_or_reserved(self):
        self.user.is_superuser = True
        self.user.save()
        response = self.client.post(f"/gyms/{self.a.slug}/domains/", {
            "hostname": f"NEW-{self.a.slug.upper()}.EXAMPLE.COM."
        })
        self.assertEqual(response.status_code, 302)
        domain = GymDomain.objects.get(hostname=f"new-{self.a.slug}.example.com")
        self.assertIsNone(domain.verified_at)
        for value in (domain.hostname.upper(), "localhost", "127.0.0.1", "https://example.com", "example.com/path", "example.com:8000", "*.example.com"):
            self.assertEqual(self.client.post(f"/gyms/{self.b.slug}/domains/", {
                "hostname": value
            }).status_code, 400)
        domain.organization = self.b
        with self.assertRaises(ValidationError):
            domain.save()

    def test_dns_verification_requires_exact_token(self):
        domain = GymDomain.objects.create(organization=self.a, hostname=f"proof-{uuid4().hex}.example.com")
        with patch("gyms.domains.dns.resolver.resolve", return_value=[SimpleNamespace(strings=[b"wrong"])]) as resolve:
            with self.assertRaises(CommandError):
                call_command("verify_gym_domain", domain.hostname)
            resolve.assert_called_once_with(f"_powerhouse-verification.{domain.hostname}.", "TXT", lifetime=5)
        domain.refresh_from_db()
        self.assertIsNone(domain.verified_at)
        token = domain.verification_token.encode()
        with patch("gyms.domains.dns.resolver.resolve", return_value=[SimpleNamespace(strings=[token[:10], token[10:]])]):
            call_command("verify_gym_domain", domain.hostname)
        domain.refresh_from_db()
        self.assertIsNotNone(domain.verified_at)

    def test_local_verification_is_debug_only_and_restricted(self):
        self.domain_a.verified_at = None
        self.domain_a.save()
        with override_settings(DEBUG=False):
            with self.assertRaises(CommandError):
                call_command("verify_gym_domain", self.domain_a.hostname, local=True)
        with override_settings(DEBUG=True):
            call_command("verify_gym_domain", self.domain_a.hostname, local=True)
            domain = GymDomain.objects.create(organization=self.a, hostname=f"{self.a.slug}.example.com")
            with self.assertRaises(CommandError):
                call_command("verify_gym_domain", domain.hostname, local=True)

    def test_cross_domain_post_and_cookie_settings(self):
        self.assertIsNone(settings.SESSION_COOKIE_DOMAIN)
        self.assertIsNone(settings.CSRF_COOKIE_DOMAIN)
        client = self.tenant_client(csrf=True)
        response = client.get("/accounts/login/")
        token = client.cookies["csrftoken"].value
        self.assertEqual(client.post("/accounts/logout/", {"csrfmiddlewaretoken": token},
            HTTP_ORIGIN=f"http://{self.domain_b.hostname}").status_code, 403)
        self.assertEqual(client.post("/accounts/logout/", {"csrfmiddlewaretoken": token},
            HTTP_ORIGIN=f"http://{self.domain_a.hostname}").status_code, 302)

    def test_branding_rejects_css_injection(self):
        self.user.is_superuser = True
        self.user.save()
        response = self.client.post("/gyms/new/", {
            "name": "Unsafe", "slug": uuid4().hex,
            "primary_color": "red;}body{display:none", "logo_url": "javascript:alert(1)", **self.owner_payload()
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Organization.objects.filter(name="Unsafe").exists())


    def test_onboarding_creates_owner_and_template_without_platform_privileges(self):
        self.user.is_superuser = True
        self.user.save()
        account = self.owner_payload()
        slug = uuid4().hex
        response = self.client.post("/gyms/new/", {
            "name": "New owner gym", "slug": slug, "primary_color": "#aabbcc", **account,
        })
        self.assertEqual(response.status_code, 302)
        gym = Organization.objects.get(slug=slug)
        owner = get_user_model().objects.get(username=account["owner-username"])
        self.assertTrue(owner.check_password(account["owner-password1"]))
        self.assertFalse(owner.is_staff or owner.is_superuser)
        self.assertTrue(Membership.objects.filter(user=owner, organization=gym, role="owner", is_active=True).exists())
        with gym_database(ready_database(gym)):
            site = WebsiteSettings.objects.get(pk=1)
            self.assertEqual(site.display_name, "New owner gym")
            self.assertEqual(site.primary_color, "#aabbcc")
        domain = GymDomain.objects.create(organization=gym, hostname=f"owner-{slug}.test", verified_at=timezone.now())
        client = Client(HTTP_HOST=domain.hostname)
        response = client.post("/accounts/login/", {
            "username": owner.username, "password": account["owner-password1"]
        })
        self.assertEqual(response["Location"], "/manage/")
        self.assertContainsText(client.get("/manage/"), "Customize website")
        self.assertEqual(client.get("/platform-admin/").status_code, 404)

    def test_platform_can_create_owner_for_existing_gym(self):
        self.assertEqual(self.client.get(f"/gyms/{self.a.slug}/owners/new/").status_code, 403)
        self.user.is_superuser = True
        self.user.save()
        data = {"username": f"extra-{uuid4().hex}", "email": "extra@example.com",
                "password1": "GymOwner-Strong-84!", "password2": "GymOwner-Strong-84!"}
        response = self.client.post(f"/gyms/{self.a.slug}/owners/new/", data)
        self.assertEqual(response.status_code, 302)
        owner = get_user_model().objects.get(username=data["username"])
        self.assertFalse(owner.is_staff or owner.is_superuser)
        self.assertTrue(owner.memberships.filter(organization=self.a, role="owner").exists())
        self.assertFalse(owner.memberships.filter(organization=self.b).exists())
        self.assertEqual(self.client.post(f"/gyms/{self.a.slug}/owners/new/", data).status_code, 200)
        self.assertEqual(get_user_model().objects.filter(username=data["username"]).count(), 1)

    def test_invalid_owner_credentials_do_not_create_partial_gym(self):
        self.user.is_superuser = True
        self.user.save()
        account = self.owner_payload()
        account["owner-password2"] = "mismatch"
        slug = uuid4().hex
        response = self.client.post("/gyms/new/", {
            "name": "Invalid owner", "slug": slug, "primary_color": "#123456", **account,
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Organization.objects.filter(slug=slug).exists())
        self.assertFalse(get_user_model().objects.filter(username=account["owner-username"]).exists())
        account["owner-password1"] = account["owner-password2"] = "12345678"
        self.assertEqual(self.client.post("/gyms/new/", {
            "name": "Weak password", "slug": slug, "primary_color": "#123456", **account,
        }).status_code, 200)
        self.assertFalse(Organization.objects.filter(slug=slug).exists())

    def test_anonymous_public_website_uses_shared_template_and_own_content(self):
        client = Client(HTTP_HOST=self.domain_a.hostname)
        response = client.get("/")
        self.assertContainsText(response, self.a.name)
        self.assertContainsText(response, "Train stronger. Live better.")
        self.assertNotIn(b"Powerhouse", response.content)
        self.assertNotIn(b"Private address B", response.content)
        self.assertEqual(client.get("/manage/").status_code, 302)
        self.assertEqual(client.get("/manage/website/").status_code, 302)

    def test_owner_edits_website_only_in_own_database(self):
        before_b = WebsiteSettings.objects.using(self.alias_b).get(pk=1).hero_title
        response = self.tenant_client().post("/manage/website/", self.website_payload(
            display_name="IronFit by owner", template="studio", primary_color="#567890",
            hero_title="Made for our community", contact_email="hello@ironfit.test",
            logo_url="https://images.example.com/logo.png", id=999, organization=self.b.pk,
            database_key=str(self.b.database_key),
        ))
        self.assertEqual(response.status_code, 302)
        site = WebsiteSettings.objects.using(self.alias_a).get(pk=1)
        self.assertEqual(site.display_name, "IronFit by owner")
        self.assertEqual(site.template, "studio")
        self.assertEqual(WebsiteSettings.objects.using(self.alias_b).get(pk=1).hero_title, before_b)
        public = Client(HTTP_HOST=self.domain_a.hostname).get("/")
        self.assertContainsText(public, "Made for our community")
        self.assertContainsText(public, 'class="studio"')
        self.assertContainsText(public, "hello@ironfit.test")
        self.assertNotIn(b"Made for our community", Client(HTTP_HOST=self.domain_b.hostname).get("/").content)
        login = Client(HTTP_HOST=self.domain_a.hostname).get("/accounts/login/")
        self.assertContainsText(login, "IronFit by owner")
        self.assertIn(b"https://images.example.com/logo.png", login.content)

    def test_nonowners_cannot_edit_or_access_owner_dashboard(self):
        client = self.tenant_client()
        for role in ("manager", "staff", "member"):
            Membership.objects.filter(user=self.user, organization=self.a).update(role=role)
            for path in ("/manage/", "/manage/website/"):
                self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(client.post("/manage/website/", self.website_payload(hero_title="Forbidden")).status_code, 404)
        self.assertNotEqual(WebsiteSettings.objects.using(self.alias_a).get(pk=1).hero_title, "Forbidden")

    def test_owner_cannot_edit_other_gym_even_with_superuser_flag(self):
        self.user.is_superuser = True
        self.user.save()
        other = self.tenant_client(self.domain_b)
        self.assertEqual(other.get("/manage/").status_code, 404)
        self.assertEqual(other.post("/manage/website/", self.website_payload(hero_title="Forbidden")).status_code, 404)
        Membership.objects.filter(user=self.user, organization=self.a).update(is_active=False)
        self.assertEqual(self.tenant_client().post("/manage/website/", self.website_payload()).status_code, 404)

    def test_invalid_customization_does_not_save_or_change_page_branding(self):
        original = WebsiteSettings.objects.using(self.alias_a).get(pk=1)
        for changes in ({"primary_color": "red;}body{display:none"},
                        {"logo_url": "javascript:alert(1)"},
                        {"hero_image_url": "http://insecure.example.com/image.png"},
                        {"template": "../../other_template"}):
            response = self.tenant_client().post("/manage/website/", self.website_payload(**changes))
            self.assertEqual(response.status_code, 200)
            site = WebsiteSettings.objects.using(self.alias_a).get(pk=1)
            self.assertEqual(site.primary_color, original.primary_color)
            self.assertEqual(site.template, original.template)
            self.assertIn(f"--brand:{original.primary_color}".encode(), response.content)

    def test_website_text_is_escaped_in_public_template(self):
        self.tenant_client().post("/manage/website/", self.website_payload(
            hero_title='<script>alert("x")</script>', about_text='<img src=x onerror=alert(1)>'
        ))
        response = Client(HTTP_HOST=self.domain_a.hostname).get("/")
        self.assertContainsText(response, "&lt;script&gt;")
        self.assertNotIn(b"<script>", response.content)
        self.assertNotIn(b"<img src=x", response.content)

    def test_reprovision_and_template_switch_preserve_customizations(self):
        self.tenant_client().post("/manage/website/", self.website_payload(hero_title="Keep my edits"))
        provision_database(self.a)
        self.assertEqual(WebsiteSettings.objects.using(self.alias_a).get(pk=1).hero_title, "Keep my edits")
        self.tenant_client().post("/manage/website/", self.website_payload(template="studio"))
        self.assertEqual(WebsiteSettings.objects.using(self.alias_a).get(pk=1).hero_title, "Keep my edits")
        with self.assertRaises(IntegrityError):
            WebsiteSettings.objects.using(self.alias_a).create(pk=2, display_name="Not allowed")

    def test_existing_gym_upgrade_initializes_website_and_preserves_branches(self):
        # Emulate a gym created before the websites app introduced its first schema.
        from django.db.migrations.recorder import MigrationRecorder
        with connections[self.alias_a].schema_editor() as editor:
            editor.delete_model(ContactMessage)
            editor.delete_model(WebsiteSettings)
        MigrationRecorder.Migration.objects.using(self.alias_a).filter(app="websites").delete()
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).get("/").status_code, 503)
        self.user.is_superuser = True
        self.user.save()
        self.assertContainsText(self.client.get(f"/gyms/{self.a.slug}/"), "Retry setup")
        self.assertEqual(self.client.post(f"/gyms/{self.a.slug}/retry-setup/").status_code, 302)
        site = WebsiteSettings.objects.using(self.alias_a).get(pk=1)
        self.assertEqual(site.display_name, self.a.name)
        self.assertEqual(Location.objects.using(self.alias_a).get(pk=self.branch_a.pk).address, "Address A")
        self.assertContainsText(Client(HTTP_HOST=self.domain_a.hostname).get("/"), self.a.name)

    def test_website_editor_requires_same_origin_csrf(self):
        client = self.tenant_client(csrf=True)
        client.get("/manage/website/")
        data = self.website_payload(hero_title="Same-origin edit")
        self.assertEqual(client.post("/manage/website/", data).status_code, 403)
        data["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        self.assertEqual(client.post("/manage/website/", data,
            HTTP_ORIGIN=f"http://{self.domain_b.hostname}").status_code, 403)
        self.assertEqual(client.post("/manage/website/", data,
            HTTP_ORIGIN=f"http://{self.domain_a.hostname}").status_code, 302)


    def test_demo_setup_creates_two_isolated_owner_portals_and_preserves_edits(self):
        with override_settings(DEBUG=True), patch(
            "gyms.management.commands.setup_demo_gyms.getpass", return_value="Local-Demo-Strong-94!"
        ):
            call_command("setup_demo_gyms", verbosity=0)
        first = Organization.objects.get(slug="demo-ironfit")
        second = Organization.objects.get(slug="demo-titans")
        self.assertNotEqual(first.database_key, second.database_key)
        for gym, username, hostname in (
            (first, "demo_ironfit_owner", "demo-ironfit.test"),
            (second, "demo_titans_owner", "demo-titans.test"),
        ):
            owner = get_user_model().objects.get(username=username)
            self.assertFalse(owner.is_staff or owner.is_superuser)
            client = Client(HTTP_HOST=hostname)
            self.assertEqual(client.get("/").status_code, 200)
            response = client.post("/accounts/login/", {
                "username": username, "password": "Local-Demo-Strong-94!"
            })
            self.assertEqual(response["Location"], "/manage/")
            self.assertContainsText(client.get("/manage/website/"), gym.name)
            other = Client(HTTP_HOST="demo-titans.test" if gym == first else "demo-ironfit.test")
            other.force_login(owner)
            self.assertEqual(other.get("/manage/").status_code, 404)
        alias = ready_database(first)
        WebsiteSettings.objects.using(alias).filter(pk=1).update(hero_title="My edited demo")
        counts = (Organization.objects.count(), get_user_model().objects.count())
        with override_settings(DEBUG=True), patch(
            "gyms.management.commands.setup_demo_gyms.getpass"
        ) as prompt:
            call_command("setup_demo_gyms", verbosity=0)
            prompt.assert_not_called()
        self.assertEqual(counts, (Organization.objects.count(), get_user_model().objects.count()))
        self.assertEqual(WebsiteSettings.objects.using(alias).get(pk=1).hero_title, "My edited demo")

    def test_demo_setup_is_disabled_in_production(self):
        with override_settings(DEBUG=False):
            with self.assertRaises(CommandError):
                call_command("setup_demo_gyms", verbosity=0)

    def test_demo_setup_rejects_domain_collision_without_creating_gyms(self):
        token = uuid4().hex
        spec = {"slug": f"collision-{token}", "name": "Collision demo", "color": "#123456",
                "username": f"collision-{token}", "hostname": f"collision-{token}.test"}
        domain = GymDomain.objects.create(organization=self.a, hostname=spec["hostname"])
        try:
            before = Organization.objects.count()
            with override_settings(DEBUG=True), patch(
                "gyms.management.commands.setup_demo_gyms.DEMOS", (spec,)
            ), patch("gyms.management.commands.setup_demo_gyms.getpass") as prompt:
                with self.assertRaises(CommandError):
                    call_command("setup_demo_gyms", verbosity=0)
                prompt.assert_not_called()
            self.assertEqual(Organization.objects.count(), before)
        finally:
            domain.delete()


    def test_onboarding_automatically_creates_local_portal_and_registers_custom_domain(self):
        self.user.is_superuser = True
        self.user.save()
        slug = "auto-" + uuid4().hex
        hostname = f"{slug}.example.com"
        response = self.client.post("/gyms/new/", {
            "name": "Automatic gym", "slug": slug, "primary_color": "#123456",
            "domain_hostname": hostname, **self.owner_payload(),
        })
        self.assertEqual(response.status_code, 302)
        gym = Organization.objects.get(slug=slug)
        local = GymDomain.objects.get(organization=gym, hostname=f"{slug}.localhost")
        self.assertIsNotNone(local.verified_at)
        self.assertIsNone(GymDomain.objects.get(hostname=hostname).verified_at)
        self.assertContainsText(Client(HTTP_HOST=local.hostname).get("/"), "Automatic gym")
        self.assertContainsText(self.client.get(response["Location"]), "Owner login")

    def test_enable_local_portal_for_existing_gym_preserves_owners_and_uses_visible_port(self):
        self.user.is_superuser = True
        self.user.save()
        client = Client(HTTP_HOST="localhost:8000")
        client.force_login(self.user)
        before = (get_user_model().objects.count(), Organization.objects.count())
        path = f"/gyms/{self.a.slug}/local-portal/"
        for _ in range(2):
            self.assertEqual(client.post(path).status_code, 302)
        domain = GymDomain.objects.get(organization=self.a, hostname=f"{self.a.slug}.localhost")
        response = client.get(f"/gyms/{self.a.slug}/")
        self.assertContainsText(response, f"http://{domain.hostname}:8000/accounts/login/")
        self.assertEqual(before, (get_user_model().objects.count(), Organization.objects.count()))
        self.assertTrue(Membership.objects.filter(organization=self.a, user=self.user, role="owner").exists())

    @override_settings(DOMAIN_HOSTING_ENABLED=True, DOMAIN_HOSTING_IPV4="203.0.113.1")
    def test_automatic_domain_hosting_real_state_and_other_gym_unchanged(self):
        from gyms.hosting import process_domain
        self.user.is_superuser = True
        self.user.save()
        domain = GymDomain.objects.create(organization=self.a, hostname=f"auto-{uuid4().hex}.example.com")
        before = (self.domain_b.verified_at, self.domain_b.hosting_status)
        with patch("gyms.hosting.call_agent", return_value={"status": "waiting", "message": "Waiting for TXT"}):
            process_domain(domain.pk)
        domain.refresh_from_db()
        self.assertEqual(domain.hosting_status, "waiting")
        self.assertIsNone(domain.verified_at)
        response = self.client.get(f"/gyms/{self.a.slug}/")
        self.assertContainsText(response, "Waiting for TXT")
        self.assertContainsText(response, "203.0.113.1")
        retry = f"/gyms/{self.a.slug}/domains/{domain.pk}/hosting/"
        self.assertEqual(self.client.post(retry).status_code, 302)
        with patch("gyms.hosting.call_agent", return_value={"status": "live", "message": "HTTPS ready"}):
            process_domain(domain.pk)
        domain.refresh_from_db()
        self.assertEqual(domain.hosting_status, "live")
        self.assertIsNotNone(domain.verified_at)
        self.assertContainsText(self.client.get(f"/gyms/{self.a.slug}/"), f"https://{domain.hostname}/accounts/login/")
        self.domain_b.refresh_from_db()
        self.assertEqual((self.domain_b.verified_at, self.domain_b.hosting_status), before)

    @override_settings(DOMAIN_HOSTING_ENABLED=True)
    def test_hosting_retry_is_scoped_and_preserves_live_lease(self):
        self.user.is_superuser = True
        self.user.save()
        domain = GymDomain.objects.create(organization=self.a, hostname=f"retry-{uuid4().hex}.example.com", hosting_status="failed")
        self.assertEqual(self.client.post(f"/gyms/{self.b.slug}/domains/{domain.pk}/hosting/").status_code, 404)
        path = f"/gyms/{self.a.slug}/domains/{domain.pk}/hosting/"
        self.assertEqual(self.client.post(path).status_code, 302)
        domain.refresh_from_db()
        self.assertEqual(domain.hosting_status, "pending")
        domain.hosting_status = "provisioning"
        domain.hosting_next_attempt = timezone.now() + timedelta(minutes=8)
        domain.save()
        lease = domain.hosting_next_attempt
        self.client.post(path)
        domain.refresh_from_db()
        self.assertEqual(domain.hosting_next_attempt, lease)
        self.assertEqual(domain.hosting_status, "provisioning")

    def test_dashboard_dns_verification_requires_matching_proof_and_correct_gym(self):
        self.user.is_superuser = True
        self.user.save()
        domain = GymDomain.objects.create(organization=self.a, hostname=f"dns-{uuid4().hex}.example.com")
        path = f"/gyms/{self.a.slug}/domains/{domain.pk}/verify/"
        with patch("gyms.domains.dns.resolver.resolve", return_value=[SimpleNamespace(strings=[b"wrong"])]) as resolve:
            response = self.client.post(path, {"local": "true"}, follow=True)
            self.assertContainsText(response, "does not match")
            resolve.assert_called_once()
        domain.refresh_from_db()
        self.assertIsNone(domain.verified_at)
        with patch("gyms.domains.dns.resolver.resolve", return_value=[SimpleNamespace(strings=[domain.verification_token.encode()])]):
            response = self.client.post(path, follow=True)
        self.assertContainsText(response, f"https://{domain.hostname}/accounts/login/")
        self.assertEqual(self.client.post(f"/gyms/{self.b.slug}/domains/{domain.pk}/verify/").status_code, 404)
        domain.is_active = False
        domain.verified_at = None
        domain.save()
        with patch("gyms.domains.dns.resolver.resolve") as resolve:
            self.client.post(path)
            resolve.assert_not_called()
        domain.refresh_from_db()
        self.assertIsNone(domain.verified_at)

    def test_onboarding_actions_require_platform_role_post_and_csrf(self):
        paths = (f"/gyms/{self.a.slug}/local-portal/", f"/gyms/{self.a.slug}/retry-setup/",
                 f"/gyms/{self.a.slug}/domains/{self.domain_a.pk}/verify/",
                 f"/gyms/{self.a.slug}/domains/{self.domain_a.pk}/hosting/")
        for path in paths:
            self.assertEqual(self.client.post(path).status_code, 403)
        self.user.is_superuser = True
        self.user.save()
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 405)
            self.assertEqual(self.tenant_client().post(path).status_code, 404)
        client = Client(HTTP_HOST="localhost", enforce_csrf_checks=True)
        client.force_login(self.user)
        client.get(f"/gyms/{self.a.slug}/")
        for path in paths:
            self.assertEqual(client.post(path).status_code, 403)
        token = client.cookies["csrftoken"].value
        self.assertEqual(client.post(paths[0], {"csrfmiddlewaretoken": token}).status_code, 302)

    def test_production_rejects_local_activation_and_does_not_create_local_portal(self):
        self.user.is_superuser = True
        self.user.save()
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.post(f"/gyms/{self.a.slug}/local-portal/").status_code, 403)
            self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).get("/").status_code, 404)
            self.assertEqual(self.client.post(f"/gyms/{self.a.slug}/domains/", {"hostname": "not-public.localhost"}).status_code, 400)
            slug = "prod-" + uuid4().hex
            self.assertEqual(self.client.post("/gyms/new/", {
                "name": "Production gym", "slug": slug, "primary_color": "#123456", **self.owner_payload()
            }).status_code, 302)
            self.assertFalse(Organization.objects.get(slug=slug).domains.exists())

    def test_local_hostname_collision_does_not_take_over_other_gym(self):
        self.user.is_superuser = True
        self.user.save()
        occupied = GymDomain.objects.create(organization=self.b, hostname=f"{self.a.slug}.localhost")
        self.assertEqual(self.client.post(f"/gyms/{self.a.slug}/local-portal/").status_code, 302)
        occupied.refresh_from_db()
        self.assertEqual(occupied.organization_id, self.b.pk)
        self.assertIsNone(occupied.verified_at)
        fallback = GymDomain.objects.get(organization=self.a, hostname=f"gym-{self.a.database_key.hex}.localhost")
        self.assertIsNotNone(fallback.verified_at)
        fallback.is_active = False
        fallback.save()
        self.client.post(f"/gyms/{self.a.slug}/local-portal/")
        fallback.refresh_from_db()
        self.assertFalse(fallback.is_active)

    def test_failed_provisioning_is_recoverable_entirely_through_dashboard(self):
        self.user.is_superuser = True
        self.user.save()
        slug = "retry-ui-" + uuid4().hex
        account = self.owner_payload()
        with patch("platform_core.views.provision_database", side_effect=DatabaseError("not ready")):
            response = self.client.post("/gyms/new/", {
                "name": "Retry through dashboard", "slug": slug, "primary_color": "#123456", **account
            }, follow=True)
        self.assertContainsText(response, "Retry setup")
        gym = Organization.objects.get(slug=slug)
        self.assertFalse(gym.database_ready)
        self.assertEqual(self.client.post(f"/gyms/{slug}/retry-setup/").status_code, 302)
        gym.refresh_from_db()
        self.assertTrue(gym.database_ready)
        self.assertEqual(gym.memberships.filter(role="owner").count(), 1)
        self.assertTrue(get_user_model().objects.get(username=account["owner-username"]).check_password(account["owner-password1"]))
        domain = GymDomain.objects.get(organization=gym, hostname=f"{slug}.localhost")
        self.assertContainsText(Client(HTTP_HOST=domain.hostname).get("/"), "Retry through dashboard")

    def test_remote_platform_does_not_offer_loopback_links(self):
        self.user.is_superuser = True
        self.user.save()
        self.client.post(f"/gyms/{self.a.slug}/local-portal/")
        with override_settings(PLATFORM_HOSTS=["platform.example.com"]):
            remote = Client(HTTP_HOST="platform.example.com")
            remote.force_login(self.user)
            response = remote.get(f"/gyms/{self.a.slug}/")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b"Enable local portal", response.content)
            self.assertNotIn(f'http://{self.a.slug}.localhost'.encode(), response.content)
            self.assertEqual(remote.post(f"/gyms/{self.a.slug}/local-portal/").status_code, 403)


    def test_full_public_membership_website_pages_are_branded_and_accessible(self):
        plan = self.plan()
        client = Client(HTTP_HOST=self.domain_a.hostname)
        for path in ("/", "/about/", "/memberships/", "/contact/", "/join/", f"/join/?plan={plan.pk}"):
            response = client.get(path)
            self.assertContainsText(response, self.a.name)
            self.assertNotIn(b"Powerhouse", response.content)
            self.assertIn(b"/memberships/", response.content)
            self.assertIn(b"/contact/", response.content)

    def test_only_published_gym_plans_and_precise_prices_are_public(self):
        self.plan(name="Public plan", is_featured=True)
        self.plan(name="Private draft", is_active=False)
        self.plan(self.alias_b, name="Other gym plan")
        response = Client(HTTP_HOST=self.domain_a.hostname).get("/memberships/")
        self.assertContainsText(response, "Public plan")
        self.assertIn("₱1250.50".encode(), response.content)
        self.assertNotIn(b"Private draft", response.content)
        self.assertNotIn(b"Other gym plan", response.content)

    def test_owner_creates_edits_and_hides_membership_plan(self):
        client = self.tenant_client()
        data = {"name": "Owner plan", "description": "Our plan", "price": "1500.25", "period": "month",
                "features": "Weights\nGym access", "is_active": "on", "is_featured": "on", "sort_order": "0"}
        self.assertEqual(client.post("/manage/plans/new/", data).status_code, 302)
        plan = MembershipPlan.objects.using(self.alias_a).get(name="Owner plan")
        self.assertEqual(plan.price, Decimal("1500.25"))
        self.assertEqual(MembershipPlan.objects.using(self.alias_b).count(), 0)
        self.assertContainsText(Client(HTTP_HOST=self.domain_a.hostname).get("/memberships/"), "Owner plan")
        data.pop("is_active")
        data["name"] = "Hidden owner plan"
        self.assertEqual(client.post(f"/manage/plans/{plan.pk}/", data).status_code, 302)
        self.assertNotIn(b"Hidden owner plan", Client(HTTP_HOST=self.domain_a.hostname).get("/memberships/").content)

    def test_membership_management_requires_active_owner(self):
        plan = self.plan()
        for role in ("member", "staff", "manager"):
            Membership.objects.filter(user=self.user, organization=self.a).update(role=role)
            for path in ("/manage/plans/", "/manage/plans/new/", f"/manage/plans/{plan.pk}/",
                         "/manage/applications/", "/manage/inbox/"):
                self.assertEqual(self.tenant_client().get(path).status_code, 404)
        self.assertEqual(self.tenant_client(self.domain_b).get("/manage/plans/").status_code, 404)

    def test_membership_request_is_tenant_local_with_server_quoted_price(self):
        plan = self.plan()
        users_before = get_user_model().objects.count()
        response = Client(HTTP_HOST=self.domain_a.hostname).post("/join/", self.application_payload(
            plan, quoted_price="1.00", quoted_plan_name="Tampered", status="approved", organization=self.b.pk
        ))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/join/thanks/")
        application = MembershipApplication.objects.using(self.alias_a).get()
        self.assertEqual(application.quoted_price, Decimal("1250.50"))
        self.assertEqual(application.quoted_plan_name, plan.name)
        self.assertEqual(application.status, "new")
        self.assertEqual(MembershipApplication.objects.using(self.alias_b).count(), 0)
        self.assertEqual(users_before, get_user_model().objects.count())
        receipt = Client(HTTP_HOST=self.domain_a.hostname).get(response["Location"])
        self.assertNotIn(b"jamie@example.com", receipt.content)

    def test_join_rejects_other_gym_inactive_and_malformed_plan_identifiers(self):
        other = self.plan(self.alias_b)
        hidden = self.plan(is_active=False)
        client = Client(HTTP_HOST=self.domain_a.hostname)
        for plan in (other, hidden):
            self.assertEqual(client.get(f"/join/?plan={plan.pk}").status_code, 404)
            self.assertEqual(client.post("/join/", self.application_payload(plan)).status_code, 200)
        self.assertEqual(client.get("/join/?plan=not-a-uuid").status_code, 404)
        self.assertEqual(MembershipApplication.objects.using(self.alias_a).count(), 0)

    def test_duplicate_pending_application_is_prevented_case_insensitively(self):
        plan = self.plan()
        client = Client(HTTP_HOST=self.domain_a.hostname)
        self.assertEqual(client.post("/join/", self.application_payload(plan)).status_code, 302)
        response = client.post("/join/", self.application_payload(plan, email="JAMIE@example.com"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MembershipApplication.objects.using(self.alias_a).count(), 1)
        application = MembershipApplication.objects.using(self.alias_a).get()
        application.status = "declined"
        application.save(using=self.alias_a)
        self.assertEqual(client.post("/join/", self.application_payload(plan)).status_code, 302)

    def test_plan_edits_preserve_historical_application_quote(self):
        plan = self.plan()
        Client(HTTP_HOST=self.domain_a.hostname).post("/join/", self.application_payload(plan))
        MembershipPlan.objects.using(self.alias_a).filter(pk=plan.pk).update(name="Changed", price=Decimal("9000"), is_active=False)
        application = MembershipApplication.objects.using(self.alias_a).get()
        self.assertEqual(application.quoted_plan_name, "Standard membership")
        self.assertEqual(application.quoted_price, Decimal("1250.50"))
        self.assertContainsText(self.tenant_client().get("/manage/applications/"), "Standard membership")

    def test_owner_reviews_application_without_creating_paid_membership(self):
        plan = self.plan()
        Client(HTTP_HOST=self.domain_a.hostname).post("/join/", self.application_payload(plan))
        application = MembershipApplication.objects.using(self.alias_a).get()
        users_before = get_user_model().objects.count()
        client = self.tenant_client()
        path = f"/manage/applications/{application.pk}/review/"
        self.assertEqual(client.post(path, {"status": "contacted"}).status_code, 302)
        self.assertEqual(client.post(path, {"status": "approved"}).status_code, 302)
        self.assertEqual(client.post(path, {"status": "declined"}).status_code, 302)
        application.refresh_from_db(using=self.alias_a)
        self.assertEqual(application.status, "approved")
        self.assertEqual(users_before, get_user_model().objects.count())

    def test_application_uuid_cannot_be_reviewed_from_other_gym(self):
        plan = self.plan(self.alias_b)
        Client(HTTP_HOST=self.domain_b.hostname).post("/join/", self.application_payload(plan))
        application = MembershipApplication.objects.using(self.alias_b).get()
        self.assertEqual(self.tenant_client().post(f"/manage/applications/{application.pk}/review/", {
            "status": "approved"
        }).status_code, 404)
        self.assertNotIn(b"jamie@example.com", self.tenant_client().get("/manage/applications/").content)
        application.refresh_from_db(using=self.alias_b)
        self.assertEqual(application.status, "new")

    def test_contact_messages_reach_only_own_gym_private_inbox(self):
        data = {"full_name": "Casey Visitor", "email": "casey@example.com", "subject": "Visiting",
                "message": "When can I visit?", "consent": "on"}
        client = Client(HTTP_HOST=self.domain_a.hostname)
        response = client.post("/contact/", data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContactMessage.objects.using(self.alias_a).count(), 1)
        self.assertEqual(ContactMessage.objects.using(self.alias_b).count(), 0)
        self.assertNotIn(b"casey@example.com", client.get(response["Location"]).content)
        self.assertEqual(client.get("/manage/inbox/").status_code, 302)
        self.assertContainsText(self.tenant_client().get("/manage/inbox/"), "casey@example.com")

    def test_contact_and_join_validate_consent_and_honeypot(self):
        plan = self.plan()
        client = Client(HTTP_HOST=self.domain_a.hostname)
        data = self.application_payload(plan)
        data.pop("consent")
        self.assertEqual(client.post("/join/", data).status_code, 200)
        self.assertEqual(client.post("/join/", self.application_payload(plan, website_url="https://spam.example.com")).status_code, 200)
        contact = {"full_name": "Bot", "email": "bot@example.com", "subject": "Hi", "message": "Hi"}
        self.assertEqual(client.post("/contact/", contact).status_code, 200)
        self.assertEqual(client.post("/contact/", {**contact, "consent": "on", "website_url": "spam"}).status_code, 200)
        self.assertEqual(MembershipApplication.objects.using(self.alias_a).count(), 0)
        self.assertEqual(ContactMessage.objects.using(self.alias_a).count(), 0)

    def test_public_forms_require_same_origin_csrf(self):
        plan = self.plan()
        client = Client(HTTP_HOST=self.domain_a.hostname, enforce_csrf_checks=True)
        client.get("/join/")
        data = self.application_payload(plan)
        self.assertEqual(client.post("/join/", data).status_code, 403)
        data["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
        self.assertEqual(client.post("/join/", data, HTTP_ORIGIN=f"http://{self.domain_b.hostname}").status_code, 403)
        self.assertEqual(client.post("/join/", data, HTTP_ORIGIN=f"http://{self.domain_a.hostname}").status_code, 302)
        self.assertEqual(client.post("/contact/", {}).status_code, 403)

    def test_owner_dashboard_counts_and_inbox_read_action(self):
        plan = self.plan()
        Client(HTTP_HOST=self.domain_a.hostname).post("/join/", self.application_payload(plan))
        message = ContactMessage.objects.using(self.alias_a).create(full_name="Hi", email="hi@example.com", subject="Question", message="Hello")
        response = self.tenant_client().get("/manage/")
        self.assertContainsText(response, "Manage memberships")
        self.assertContainsText(response, "Review applications")
        self.assertEqual(self.tenant_client().post(f"/manage/inbox/{message.pk}/read/").status_code, 302)
        message.refresh_from_db(using=self.alias_a)
        self.assertTrue(message.is_read)

    def test_owner_review_requires_post_and_valid_transition(self):
        plan = self.plan()
        Client(HTTP_HOST=self.domain_a.hostname).post("/join/", self.application_payload(plan))
        application = MembershipApplication.objects.using(self.alias_a).get()
        path = f"/manage/applications/{application.pk}/review/"
        client = self.tenant_client()
        self.assertEqual(client.get(path).status_code, 405)
        self.assertEqual(client.post(path, {"status": "paid"}).status_code, 400)
        application.refresh_from_db(using=self.alias_a)
        self.assertEqual(application.status, "new")

    def test_plan_price_validation_and_untrusted_text_are_safe(self):
        client = self.tenant_client()
        self.assertEqual(client.post("/manage/plans/new/", {
            "name": "Invalid", "price": "-1", "period": "month", "sort_order": "0", "is_active": "on"
        }).status_code, 200)
        self.assertFalse(MembershipPlan.objects.using(self.alias_a).filter(name="Invalid").exists())
        self.plan(name='<script>alert("plan")</script>')
        response = Client(HTTP_HOST=self.domain_a.hostname).get("/memberships/")
        self.assertIn(b"&lt;script&gt;", response.content)
        self.assertNotIn(b"<script>", response.content)
        ContactMessage.objects.using(self.alias_a).create(full_name="Hi", email="hi@example.com", subject="Question", message="<script>alert(1)</script>")
        self.assertNotIn(b"<script>", client.get("/manage/inbox/").content)


    def test_old_gym_installs_membership_module_from_setup_button_without_losing_branding(self):
        from django.db.migrations.recorder import MigrationRecorder
        WebsiteSettings.objects.using(self.alias_a).filter(pk=1).update(hero_title="Keep existing brand")
        with connections[self.alias_a].schema_editor() as editor:
            for model in (RenewalRequest, MemberProfileChange, EmailAttempt, EmailNotification, NotificationSettings, BookingEvent, ClassBooking, ClassSession, Trainer):
                # Empty notification tables belong to a later schema than this fixture.
                editor.delete_model(model)
            editor.delete_model(AttendanceScan)
            editor.delete_model(AttendanceVisit)
            editor.delete_model(PaymentOrder)
            editor.delete_model(MembershipEvent)
            editor.delete_model(MemberSubscription)
            editor.delete_model(MemberInvitation)
            editor.delete_model(MemberProfile)
            editor.delete_model(MembershipApplication)
            editor.delete_model(MembershipPlan)
            editor.delete_model(ContactMessage)
            editor.remove_field(WebsiteSettings, WebsiteSettings._meta.get_field("waiver_text"))
        MigrationRecorder.Migration.objects.using(self.alias_a).filter(app="payments").delete()
        MigrationRecorder.Migration.objects.using(self.alias_a).filter(app="gym_core").delete()
        MigrationRecorder.Migration.objects.using(self.alias_a).filter(app="websites", name__in=["0002_contactmessage", "0003_websitesettings_waiver_text"]).delete()
        self.user.is_superuser = True
        self.user.save()
        self.assertContainsText(self.client.get(f"/gyms/{self.a.slug}/"), "Retry setup")
        self.assertEqual(self.client.post(f"/gyms/{self.a.slug}/retry-setup/").status_code, 302)
        self.assertEqual(MembershipPlan.objects.using(self.alias_a).count(), 0)
        self.assertEqual(ContactMessage.objects.using(self.alias_a).count(), 0)
        self.assertEqual(WebsiteSettings.objects.using(self.alias_a).get(pk=1).hero_title, "Keep existing brand")
        self.assertContainsText(Client(HTTP_HOST=self.domain_a.hostname).get("/"), "Keep existing brand")


    def test_approved_application_enrolls_once_with_original_quote(self):
        member, subscription = self.enrolled_member()
        application = subscription.application
        self.assertEqual(subscription.status, "pending")
        self.assertEqual(subscription.price, application.quoted_price)
        self.assertEqual(member.full_name, application.full_name)
        self.assertIsNone(member.user_id)
        self.assertEqual(self.tenant_client().post(f"/manage/applications/{application.pk}/enroll/").status_code, 302)
        self.assertEqual(MemberProfile.objects.using(self.alias_a).count(), 1)
        self.assertEqual(MemberSubscription.objects.using(self.alias_a).count(), 1)
        self.assertEqual(MemberProfile.objects.using(self.alias_b).count(), 0)

    def test_unapproved_application_cannot_be_enrolled(self):
        plan = self.plan()
        Client(HTTP_HOST=self.domain_a.hostname).post("/join/", self.application_payload(plan))
        application = MembershipApplication.objects.using(self.alias_a).get()
        self.assertEqual(self.tenant_client().post(f"/manage/applications/{application.pk}/enroll/").status_code, 302)
        self.assertFalse(MemberProfile.objects.using(self.alias_a).exists())
        self.assertEqual(self.tenant_client().get(f"/manage/applications/{application.pk}/enroll/").status_code, 405)

    def test_owner_creates_searches_and_edits_member_profile(self):
        client = self.tenant_client()
        data = {"full_name": "Walk-in Member", "email": "walkin@example.com", "phone": "09171111111",
                "is_active": "on", "emergency_contact_name": "Family", "emergency_contact_phone": "09172222222"}
        self.assertEqual(client.post("/manage/members/new/", data).status_code, 302)
        member = MemberProfile.objects.using(self.alias_a).get(email=data["email"])
        self.assertContainsText(client.get("/manage/members/?q=Walk-in"), "Walk-in Member")
        data["full_name"] = "Updated Member"
        data["user_id"] = str(self.user.pk)
        self.assertEqual(client.post(f"/manage/members/{member.pk}/edit/", data).status_code, 302)
        member.refresh_from_db(using=self.alias_a)
        self.assertEqual(member.full_name, "Updated Member")
        self.assertIsNone(member.user_id)
        self.assertEqual(MemberProfile.objects.using(self.alias_b).count(), 0)

    def test_profile_changes_revoke_invites_without_overwriting_identity_or_waiver(self):
        member, _ = self.enrolled_member()
        token = self.invitation_token(member)
        data = {"full_name": member.full_name, "email": "changed@example.com", "phone": member.phone, "is_active": "on"}
        client = self.tenant_client()
        self.assertEqual(client.post(f"/manage/members/{member.pk}/edit/", data).status_code, 302)
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).get(f"/activate/{token}/").status_code, 404)
        token = self.invitation_token(member)
        data.pop("is_active")
        self.assertEqual(client.post(f"/manage/members/{member.pk}/edit/", data).status_code, 302)
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).get(f"/activate/{token}/").status_code, 404)
        MemberProfile.objects.using(self.alias_a).filter(pk=member.pk).update(
            user_id=self.user.pk, waiver_text="Recorded acceptance", waiver_accepted_at=timezone.now())
        data.update({"is_active": "on", "user_id": "999999", "waiver_text": "Tampered"})
        self.assertEqual(client.post(f"/manage/members/{member.pk}/edit/", data).status_code, 302)
        member.refresh_from_db(using=self.alias_a)
        self.assertEqual(member.user_id, self.user.pk)
        self.assertEqual(member.waiver_text, "Recorded acceptance")
        self.assertIsNotNone(member.waiver_accepted_at)

    def test_profile_validation_rejects_future_birth_date_and_incomplete_emergency_details(self):
        data = {"full_name": "Invalid profile", "email": "invalid@example.com", "phone": "123",
                "date_of_birth": (timezone.localdate() + timedelta(days=1)).isoformat(),
                "emergency_contact_name": "Someone", "is_active": "on"}
        self.assertEqual(self.tenant_client().post("/manage/members/new/", data).status_code, 200)
        self.assertFalse(MemberProfile.objects.using(self.alias_a).exists())

    def test_membership_calendar_terms_handle_month_end_and_leap_years(self):
        self.assertEqual(end_of_term(date(2026, 1, 31), "month"), date(2026, 2, 28))
        self.assertEqual(end_of_term(date(2024, 2, 29), "year"), date(2025, 2, 28))
        self.assertEqual(end_of_term(date(2026, 11, 30), "quarter"), date(2027, 2, 28))
        self.assertEqual(end_of_term(date(2026, 12, 31), "day"), date(2027, 1, 1))

    def test_manual_activation_requires_note_and_records_actor(self):
        member, subscription = self.enrolled_member()
        self.membership_action(member, subscription, "activate", note="")
        subscription.refresh_from_db(using=self.alias_a)
        self.assertEqual(subscription.status, "pending")
        self.membership_action(member, subscription, "activate")
        subscription.refresh_from_db(using=self.alias_a)
        self.assertEqual(subscription.status, "active")
        with gym_database(self.alias_a):
            self.assertTrue(subscription.allows_entry)
        event = MembershipEvent.objects.using(self.alias_a).get(subscription=subscription, action="activate")
        self.assertEqual(event.actor_id, self.user.pk)
        self.membership_action(member, subscription, "activate")
        self.assertEqual(MembershipEvent.objects.using(self.alias_a).filter(subscription=subscription, action="activate").count(), 1)

    def test_activation_rejects_overlapping_terms(self):
        member, subscription = self.enrolled_member()
        self.membership_action(member, subscription, "activate")
        response = self.tenant_client().post(f"/manage/members/{member.pk}/subscriptions/", {
            "plan": str(subscription.plan_id), "starts_on": (subscription.starts_on + timedelta(days=1)).isoformat()
        })
        self.assertEqual(response.status_code, 302)
        other = MemberSubscription.objects.using(self.alias_a).exclude(pk=subscription.pk).get()
        self.membership_action(member, other, "activate")
        other.refresh_from_db(using=self.alias_a)
        self.assertEqual(other.status, "pending")

    def test_freeze_resume_and_cancellation_are_audited(self):
        member, subscription = self.enrolled_member()
        self.membership_action(member, subscription, "activate")
        original_expiry = subscription.expires_on
        frozen_date = subscription.starts_on
        with patch("gym_core.members.services.timezone.localdate", return_value=frozen_date):
            self.membership_action(member, subscription, "freeze")
        subscription.refresh_from_db(using=self.alias_a)
        self.assertEqual(subscription.status, "frozen")
        with gym_database(self.alias_a):
            self.assertFalse(subscription.allows_entry)
        with patch("gym_core.members.services.timezone.localdate", return_value=frozen_date + timedelta(days=7)):
            self.membership_action(member, subscription, "resume")
        subscription.refresh_from_db(using=self.alias_a)
        self.assertEqual(subscription.expires_on, original_expiry + timedelta(days=7))
        self.assertEqual(subscription.status, "active")
        self.membership_action(member, subscription, "cancel")
        self.membership_action(member, subscription, "resume")
        subscription.refresh_from_db(using=self.alias_a)
        self.assertEqual(subscription.status, "cancelled")
        self.assertEqual(MembershipEvent.objects.using(self.alias_a).filter(subscription=subscription).count(), 5)

    def test_renewal_preserves_old_term_and_expiry_is_computed(self):
        member, subscription = self.enrolled_member()
        self.membership_action(member, subscription, "activate")
        path = f"/manage/members/{member.pk}/subscriptions/"
        data = {"plan": str(subscription.plan_id), "starts_on": subscription.expires_on.isoformat()}
        self.tenant_client().post(path, data)
        self.tenant_client().post(path, data)
        self.assertEqual(MemberSubscription.objects.using(self.alias_a).filter(member=member).count(), 2)
        renewal = MemberSubscription.objects.using(self.alias_a).exclude(pk=subscription.pk).get()
        self.membership_action(member, renewal, "activate")
        renewal.refresh_from_db(using=self.alias_a)
        with gym_database(self.alias_a):
            self.assertEqual(renewal.access_status, "Scheduled")
        subscription.refresh_from_db(using=self.alias_a)
        with gym_database(self.alias_a), patch("django.utils.timezone.localdate", return_value=subscription.expires_on):
            self.assertEqual(subscription.access_status, "Expired")
            self.assertEqual(renewal.access_status, "Active")

    def test_invitation_creates_member_chosen_account_and_single_use_link(self):
        member, _ = self.enrolled_member()
        response = self.tenant_client().post(f"/manage/members/{member.pk}/invite/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        token = re.search(r'/activate/([A-Za-z0-9_-]+)/', response.content.decode()).group(1)
        client = Client(HTTP_HOST=self.domain_a.hostname)
        username = "new-member-" + uuid4().hex
        response = client.post(f"/activate/{token}/", self.claim_payload(username))
        self.assertEqual(response.status_code, 302)
        member.refresh_from_db(using=self.alias_a)
        account = get_user_model().objects.get(pk=member.user_id)
        self.assertEqual(account.username, username)
        self.assertTrue(account.check_password("Member-Strong-84!"))
        self.assertFalse(account.is_staff or account.is_superuser)
        self.assertTrue(Membership.objects.filter(organization=self.a, user=account, role="member").exists())
        self.assertContainsText(client.get("/portal/"), member.member_number)
        self.assertEqual(client.post(f"/activate/{token}/", self.claim_payload()).status_code, 404)

    def test_expired_replaced_and_other_gym_invites_cannot_be_claimed(self):
        member, _ = self.enrolled_member()
        old = self.invitation_token(member)
        current = self.invitation_token(member)
        client = Client(HTTP_HOST=self.domain_a.hostname)
        self.assertEqual(client.get(f"/activate/{old}/").status_code, 404)
        self.assertEqual(Client(HTTP_HOST=self.domain_b.hostname).get(f"/activate/{current}/").status_code, 404)
        MemberInvitation.objects.using(self.alias_a).filter(member=member, revoked_at__isnull=True).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(client.get(f"/activate/{current}/").status_code, 404)
        member.refresh_from_db(using=self.alias_a)
        self.assertIsNone(member.user_id)

    def test_existing_account_claim_does_not_reset_its_password_or_other_access(self):
        member, _ = self.enrolled_member()
        existing = get_user_model().objects.create_user(username="existing-" + uuid4().hex, password="Existing-Strong-84!")
        Membership.objects.create(organization=self.b, user=existing, role="member")
        original_hash = existing.password
        token = self.invitation_token(member)
        client = Client(HTTP_HOST=self.domain_a.hostname)
        response = client.post(f"/activate/{token}/", {
            "mode": "existing", "existing-username": existing.username, "existing-password": "Existing-Strong-84!"
        })
        self.assertEqual(response.status_code, 302)
        existing.refresh_from_db()
        member.refresh_from_db(using=self.alias_a)
        self.assertEqual(existing.password, original_hash)
        self.assertEqual(member.user_id, existing.pk)
        self.assertTrue(existing.memberships.filter(organization=self.b).exists())

    def test_invitation_does_not_reenable_revoked_gym_access(self):
        member, _ = self.enrolled_member()
        existing = get_user_model().objects.create_user(username="revoked-" + uuid4().hex, password="Existing-Strong-84!")
        Membership.objects.create(organization=self.a, user=existing, role="member", is_active=False)
        token = self.invitation_token(member)
        response = Client(HTTP_HOST=self.domain_a.hostname).post(f"/activate/{token}/", {
            "mode": "existing", "existing-username": existing.username, "existing-password": "Existing-Strong-84!"
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(existing.memberships.get(organization=self.a).is_active)
        member.refresh_from_db(using=self.alias_a)
        self.assertIsNone(member.user_id)

    def test_configured_waiver_requires_acceptance_and_keeps_snapshot(self):
        member, _ = self.enrolled_member()
        text = "Test gym waiver version one."
        WebsiteSettings.objects.using(self.alias_a).filter(pk=1).update(waiver_text=text)
        token = self.invitation_token(member)
        data = self.claim_payload()
        client = Client(HTTP_HOST=self.domain_a.hostname)
        self.assertEqual(client.post(f"/activate/{token}/", data).status_code, 200)
        data.update({"new-accept_waiver": "on", "new-waiver_version": waiver_version(text)})
        self.assertEqual(client.post(f"/activate/{token}/", data).status_code, 302)
        WebsiteSettings.objects.using(self.alias_a).filter(pk=1).update(waiver_text="Changed after acceptance")
        member.refresh_from_db(using=self.alias_a)
        self.assertEqual(member.waiver_text, text)
        self.assertIsNotNone(member.waiver_accepted_at)

    def test_stale_waiver_submission_does_not_create_account(self):
        member, _ = self.enrolled_member()
        WebsiteSettings.objects.using(self.alias_a).filter(pk=1).update(waiver_text="New waiver")
        token = self.invitation_token(member)
        username = "stale-" + uuid4().hex
        data = self.claim_payload(username, **{"new-accept_waiver": "on", "new-waiver_version": waiver_version("Old waiver")})
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).post(f"/activate/{token}/", data).status_code, 200)
        self.assertFalse(get_user_model().objects.filter(username=username).exists())

    def test_member_login_and_portal_require_committed_enabled_profile(self):
        member, _ = self.enrolled_member()
        account = get_user_model().objects.create_user(username="profile-" + uuid4().hex, password="Member-Strong-84!")
        Membership.objects.create(organization=self.a, user=account, role="member")
        client = Client(HTTP_HOST=self.domain_a.hostname)
        self.assertEqual(client.post("/accounts/login/", {"username": account.username, "password": "Member-Strong-84!"}).status_code, 200)
        member.user_id = account.pk
        member.save(using=self.alias_a)
        self.assertEqual(client.post("/accounts/login/", {"username": account.username, "password": "Member-Strong-84!"}).status_code, 302)
        self.assertContainsText(client.get("/portal/"), member.member_number)
        MemberProfile.objects.using(self.alias_a).filter(pk=member.pk).update(is_active=False)
        self.assertEqual(client.get("/portal/").status_code, 404)

    def test_member_portal_never_shows_another_members_details(self):
        member, _ = self.enrolled_member()
        other = MemberProfile.objects.using(self.alias_a).create(full_name="Other private member", email="other-private@example.com", phone="private-phone")
        token = self.invitation_token(member)
        client = Client(HTTP_HOST=self.domain_a.hostname)
        client.post(f"/activate/{token}/", self.claim_payload())
        response = client.get("/portal/")
        self.assertNotIn(b"other-private@example.com", response.content)
        self.assertNotIn(b"private-phone", response.content)
        self.assertEqual(client.get(f"/manage/members/{other.pk}/").status_code, 404)

    def test_member_management_and_invites_require_owner_post_and_csrf(self):
        member, subscription = self.enrolled_member()
        invite = f"/manage/members/{member.pk}/invite/"
        self.assertEqual(self.tenant_client().get(invite).status_code, 405)
        client = self.tenant_client(csrf=True)
        self.assertEqual(client.post(invite).status_code, 403)
        self.assertEqual(self.tenant_client(self.domain_b).get(f"/manage/members/{member.pk}/").status_code, 404)
        Membership.objects.filter(user=self.user, organization=self.a).update(role="staff")
        self.assertEqual(self.tenant_client().get("/manage/members/").status_code, 404)
        self.assertEqual(self.tenant_client().post(invite).status_code, 404)

    def test_cross_gym_plan_cannot_be_assigned_to_member(self):
        member, _ = self.enrolled_member()
        other_plan = self.plan(self.alias_b)
        response = self.tenant_client().post(f"/manage/members/{member.pk}/subscriptions/", {
            "plan": str(other_plan.pk), "starts_on": timezone.localdate().isoformat()
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(MemberSubscription.objects.using(self.alias_a).filter(member=member).count(), 1)

    def test_member_and_subscription_tables_are_absent_from_platform_database(self):
        tables = connection.introspection.table_names()
        for name in ("gym_core_memberprofile", "gym_core_memberinvitation", "gym_core_membersubscription", "gym_core_membershipevent"):
            self.assertNotIn(name, tables)
            self.assertIn(name, connections[self.alias_a].introspection.table_names())

    def copay_config(self, environment="production"):
        # Test-only keys; generated in memory and never sent to a real provider.
        if not hasattr(self, "merchant_rsa"):
            self.merchant_rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            self.provider_rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return CopayConfig("GYM-TEST", "test-aes-secret", self.merchant_rsa,
            self.provider_rsa.public_key(), environment, "GCQR0", "https://" + self.domain_a.hostname,
            ("cashier.copay.plus",))

    def copay_order(self, environment="production"):
        member, subscription = self.enrolled_member()
        config = self.copay_config(environment)
        with gym_database(self.alias_a), patch("payments.services.config_for_gym", return_value=config):
            order = create_order(self.alias_a, self.a, subscription.pk)
        return member, subscription, order, config

    def copay_result(self, order, status=6, **overrides):
        return {"transactionId": "DTEST" + order.id.hex, "merchantTransactionId": order.merchant_reference,
                "statusId": status, "amount": str(order.amount), "netAmount": str(order.amount),
                "paymentMethodCode": order.payment_method, **overrides}

    def copay_callback(self, order, config, **overrides):
        gateway = CopayGateway(config)
        data = {"MerchantCode": order.merchant_code, "MerchantTransactionId": order.merchant_reference,
                "CurrencyCode": order.currency, "Amount": str(order.amount), "Status": 6,
                "PaymentMethodCode": order.payment_method, **overrides}
        raw = json.dumps(data).encode()
        return json.dumps({"TransactionId": "unsigned-outer-value", "MerchantTransactionId": order.merchant_reference,
                           "Data": gateway.encrypt(raw), "Signature": base64.b64encode(
                               self.provider_rsa.sign(raw, rsa_padding.PKCS1v15(), hashes.SHA1())).decode(),
                           "Version": 0}).encode()

    def test_copay_issue_is_idempotent_and_uses_server_quote(self):
        member, subscription, order, config = self.copay_order()
        with patch("payments.services.config_for_gym", return_value=config):
            response = self.tenant_client().post(f"/manage/payments/{subscription.pk}/issue/", {"amount": "0.01"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PaymentOrder.objects.using(self.alias_a).count(), 1)
        self.assertEqual(order.amount, subscription.price)
        self.assertEqual(PaymentOrder.objects.using(self.alias_b).count(), 0)
        self.assertNotIn("payments_paymentorder", connection.introspection.table_names())

    def test_copay_checkout_only_submits_once_even_after_timeout(self):
        member, subscription, order, config = self.copay_order()
        client = self.tenant_client()
        with patch("payments.services.config_for_gym", return_value=config), patch.object(
                CopayGateway, "submit", side_effect=CopayError("Timeout")) as submit:
            self.assertEqual(client.post(f"/payments/{order.pk}/checkout/").status_code, 302)
            self.assertEqual(client.post(f"/payments/{order.pk}/checkout/").status_code, 302)
        self.assertEqual(submit.call_count, 1)
        order.refresh_from_db(using=self.alias_a)
        self.assertEqual(order.status, "submitting")
        self.assertEqual(order.subscription_id, subscription.pk)

    def test_copay_checkout_redirects_only_to_configured_cashier(self):
        member, subscription, order, config = self.copay_order()
        with patch("payments.services.config_for_gym", return_value=config), patch.object(CopayGateway, "submit", return_value={
                "transactionId": "DTEST" + order.id.hex, "url": "https://cashier.copay.plus/checkout/123",
                "depositActualAmount": str(order.amount)}):
            response = self.tenant_client().post(f"/payments/{order.pk}/checkout/")
        self.assertEqual(response["Location"], "https://cashier.copay.plus/checkout/123")
        order.refresh_from_db(using=self.alias_a)
        self.assertEqual(order.status, "pending")

    def test_copay_verified_payment_activates_once_and_never_downgrades(self):
        member, subscription, order, config = self.copay_order()
        PaymentOrder.objects.using(self.alias_a).filter(pk=order.pk).update(status="submitting")
        with gym_database(self.alias_a), patch("payments.services.config_for_gym", return_value=config), patch.object(
                CopayGateway, "requery", return_value=self.copay_result(order)):
            reconcile(self.alias_a, self.a, order.pk)
            reconcile(self.alias_a, self.a, order.pk)
        order.refresh_from_db(using=self.alias_a)
        subscription.refresh_from_db(using=self.alias_a)
        self.assertEqual(order.status, "paid")
        self.assertEqual(subscription.status, "active")
        self.assertIsNotNone(order.paid_at)
        event = MembershipEvent.objects.using(self.alias_a).get(subscription=subscription, action="activate")
        self.assertIsNone(event.actor_id)
        with gym_database(self.alias_a), patch("payments.services.config_for_gym", return_value=config), patch.object(
                CopayGateway, "requery", return_value=self.copay_result(order, status=7)):
            reconcile(self.alias_a, self.a, order.pk)
        order.refresh_from_db(using=self.alias_a)
        self.assertEqual(order.status, "paid")

    def test_copay_short_payment_is_recorded_for_review_without_access(self):
        member, subscription, order, config = self.copay_order()
        PaymentOrder.objects.using(self.alias_a).filter(pk=order.pk).update(status="submitting")
        with gym_database(self.alias_a), patch("payments.services.config_for_gym", return_value=config), patch.object(
                CopayGateway, "requery", return_value=self.copay_result(order, netAmount="0.01")):
            reconcile(self.alias_a, self.a, order.pk)
        order.refresh_from_db(using=self.alias_a)
        subscription.refresh_from_db(using=self.alias_a)
        self.assertEqual(order.status, "paid")
        self.assertEqual(order.net_amount, Decimal("0.01"))
        self.assertEqual(subscription.status, "pending")
        self.assertIn("Owner review", order.access_note)
        self.assertContainsText(self.tenant_client().get(f"/payments/{order.pk}/receipt/"), "0.01")

    def test_copay_sandbox_does_not_activate_real_access(self):
        member, subscription, order, config = self.copay_order("sandbox")
        PaymentOrder.objects.using(self.alias_a).filter(pk=order.pk).update(status="submitting")
        with gym_database(self.alias_a), patch("payments.services.config_for_gym", return_value=config), patch.object(
                CopayGateway, "requery", return_value=self.copay_result(order)):
            reconcile(self.alias_a, self.a, order.pk)
        order.refresh_from_db(using=self.alias_a)
        subscription.refresh_from_db(using=self.alias_a)
        self.assertEqual(order.status, "paid")
        self.assertEqual(subscription.status, "pending")
        self.assertIn("Sandbox", order.access_note)

    def test_copay_paid_cancelled_term_requires_owner_review(self):
        member, subscription, order, config = self.copay_order()
        PaymentOrder.objects.using(self.alias_a).filter(pk=order.pk).update(status="submitting")
        self.membership_action(member, subscription, "cancel")
        with gym_database(self.alias_a), patch("payments.services.config_for_gym", return_value=config), patch.object(
                CopayGateway, "requery", return_value=self.copay_result(order)):
            reconcile(self.alias_a, self.a, order.pk)
        order.refresh_from_db(using=self.alias_a)
        subscription.refresh_from_db(using=self.alias_a)
        self.assertEqual(order.status, "paid")
        self.assertEqual(subscription.status, "cancelled")
        self.assertIn("Owner review", order.access_note)

    def test_copay_requery_rejects_amount_reference_and_method_mismatch(self):
        member, subscription, order, config = self.copay_order()
        PaymentOrder.objects.using(self.alias_a).filter(pk=order.pk).update(status="submitting")
        for overrides in ({"amount": "0.01"}, {"merchantTransactionId": "another-order"},
                          {"paymentMethodCode": "GCP"}, {"statusId": True}, {"currencyCode": "USD"}):
            with self.subTest(overrides=overrides), gym_database(self.alias_a), patch(
                    "payments.services.config_for_gym", return_value=config), patch.object(
                    CopayGateway, "requery", return_value=self.copay_result(order, **overrides)):
                with self.assertRaises(CopayError):
                    reconcile(self.alias_a, self.a, order.pk)
        order.refresh_from_db(using=self.alias_a)
        self.assertIsNone(order.paid_at)

    def test_copay_callback_requires_signature_and_requery_not_outer_transaction_id(self):
        member, subscription, order, config = self.copay_order()
        PaymentOrder.objects.using(self.alias_a).filter(pk=order.pk).update(status="submitting")
        body = self.copay_callback(order, config)
        client = Client(HTTP_HOST=self.domain_a.hostname, enforce_csrf_checks=True)
        with patch("payments.services.config_for_gym", return_value=config), patch.object(
                CopayGateway, "requery", return_value=self.copay_result(order)) as requery:
            for _ in range(2):
                response = client.post("/payments/copay/notify/", body, content_type="application/json")
                self.assertEqual(response.content, b"success")
            self.assertEqual(requery.call_count, 2)
        order.refresh_from_db(using=self.alias_a)
        self.assertNotEqual(order.provider_reference, "unsigned-outer-value")
        tampered = json.loads(body)
        tampered["Signature"] = base64.b64encode(b"invalid").decode()
        with patch("payments.services.config_for_gym", return_value=config), patch.object(CopayGateway, "requery") as requery:
            self.assertEqual(client.post("/payments/copay/notify/", json.dumps(tampered), content_type="application/json").status_code, 400)
            requery.assert_not_called()

    def test_copay_success_callback_is_retried_until_requery_confirms(self):
        member, subscription, order, config = self.copay_order()
        PaymentOrder.objects.using(self.alias_a).filter(pk=order.pk).update(status="submitting")
        body = self.copay_callback(order, config)
        client = Client(HTTP_HOST=self.domain_a.hostname)
        with patch("payments.services.config_for_gym", return_value=config), patch.object(
                CopayGateway, "requery", side_effect=[self.copay_result(order, status=1), self.copay_result(order)]):
            first = client.post("/payments/copay/notify/", body, content_type="application/json")
            self.assertEqual(first.status_code, 400)
            self.assertEqual(client.post("/payments/copay/notify/", body, content_type="application/json").content, b"success")
        order.refresh_from_db(using=self.alias_a)
        self.assertEqual(order.status, "paid")

    def test_copay_cross_gym_callback_cannot_select_another_database(self):
        member, subscription, order, config = self.copay_order()
        with patch("payments.services.config_for_gym", return_value=config), patch.object(CopayGateway, "requery") as requery:
            response = Client(HTTP_HOST=self.domain_b.hostname).post("/payments/copay/notify/", self.copay_callback(order, config), content_type="application/json")
        self.assertEqual(response.status_code, 400)
        requery.assert_not_called()

    def test_copay_return_url_never_marks_paid_and_receipts_require_settlement(self):
        member, subscription, order, config = self.copay_order()
        client = self.tenant_client()
        self.assertEqual(client.get(f"/payments/{order.pk}/return/?status=6&paid=true").status_code, 302)
        self.assertEqual(client.get(f"/payments/{order.pk}/receipt/").status_code, 404)
        order.refresh_from_db(using=self.alias_a)
        self.assertEqual(order.status, "created")
        PaymentOrder.objects.using(self.alias_a).filter(pk=order.pk).update(status="paid", paid_at=timezone.now())
        self.assertContainsText(client.get(f"/payments/{order.pk}/receipt/"), "Payment acknowledgment")

    def test_copay_member_can_only_view_and_pay_their_own_order(self):
        member, subscription, order, config = self.copay_order()
        token = self.invitation_token(member)
        client = Client(HTTP_HOST=self.domain_a.hostname)
        client.post(f"/activate/{token}/", self.claim_payload())
        self.assertEqual(client.get(f"/payments/{order.pk}/").status_code, 200)
        other = MemberProfile.objects.using(self.alias_a).create(full_name="Private",email="payment-private@example.com",phone="123")
        MemberSubscription.objects.using(self.alias_a).filter(pk=subscription.pk).update(member=other)
        self.assertEqual(client.get(f"/payments/{order.pk}/").status_code, 404)
        self.assertEqual(client.post(f"/payments/{order.pk}/checkout/").status_code, 404)
        self.assertEqual(client.post(f"/payments/{order.pk}/recheck/").status_code, 404)

    def test_copay_writes_require_csrf_owner_and_post(self):
        member, subscription, order, config = self.copay_order()
        client = self.tenant_client(csrf=True)
        self.assertEqual(client.post(f"/payments/{order.pk}/checkout/").status_code, 403)
        self.assertEqual(client.post(f"/payments/{order.pk}/recheck/").status_code, 403)
        self.assertEqual(self.tenant_client().get(f"/payments/{order.pk}/checkout/").status_code, 405)
        Membership.objects.filter(user=self.user,organization=self.a).update(role="staff")
        self.assertEqual(self.tenant_client().get(f"/payments/{order.pk}/").status_code, 404)
        self.assertEqual(self.tenant_client().post(f"/manage/payments/{subscription.pk}/issue/").status_code, 404)

    def test_copay_config_requires_separate_merchant_and_verified_domain(self):
        config = self.copay_config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "merchant.pem").write_bytes(self.merchant_rsa.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            (root / "copay.pem").write_bytes(self.provider_rsa.public_key().public_bytes(serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo))
            data = {"merchant_code":config.merchant_code, "aes_key":config.aes_key,
                    "merchant_private_key_file":"merchant.pem", "copay_public_key_file":"copay.pem",
                    "environment":"production", "public_origin":config.public_origin, "payment_method":"GCQR0"}
            path = root / "copay.secrets.json"
            with override_settings(COPAY_CONFIG_FILE=str(path)):
                path.write_text(json.dumps({str(self.a.database_key): data}))
                self.assertEqual(config_for_gym(self.a).merchant_code, config.merchant_code)
                with self.assertRaises(CopayConfigurationError):
                    config_for_gym(self.b)
                path.write_text(json.dumps({str(self.a.database_key):data, str(self.b.database_key):data}))
                with self.assertRaises(CopayConfigurationError):
                    config_for_gym(self.a)
                data["public_origin"] = "https://unverified.example.com"
                path.write_text(json.dumps({str(self.a.database_key):data}))
                with self.assertRaises(CopayConfigurationError):
                    config_for_gym(self.a)


    def active_card(self):
        member, subscription = self.enrolled_member()
        self.membership_action(member, subscription, "activate")
        return member, subscription, card_code(self.a, member)

    def scan_data(self, code, action="checkin", branch=None, request_id=None):
        return {"code":code, "action":action, "location":str((branch or self.branch_a).pk),
                "request_id":str(request_id or uuid4())}

    def scan_card(self, code, action="checkin", branch=None, client=None, request_id=None):
        return (client or self.tenant_client()).post("/desk/", self.scan_data(code, action, branch, request_id))

    def desk_account(self, role="staff", gym=None):
        user = get_user_model().objects.create_user(username="desk-"+uuid4().hex,password="Desk-Strong-84!")
        Membership.objects.create(organization=gym or self.a,user=user,role=role)
        return user

    def test_attendance_valid_card_checkin_and_checkout(self):
        member, subscription, code = self.active_card()
        response = self.scan_card(code)
        self.assertContainsText(response, "Entry allowed. Check-in recorded.")
        visit = AttendanceVisit.objects.using(self.alias_a).get()
        self.assertEqual(visit.member_id, member.pk)
        self.assertEqual(visit.subscription_id, subscription.pk)
        self.assertEqual(visit.checked_in_by_id, self.user.pk)
        self.assertContainsText(self.scan_card(code,"checkout"), "Checked out successfully.")
        visit.refresh_from_db(using=self.alias_a)
        self.assertIsNotNone(visit.checked_out_at)
        self.assertEqual(visit.checked_out_by_id,self.user.pk)
        self.assertEqual(AttendanceScan.objects.using(self.alias_a).count(),2)

    def test_attendance_repeated_scans_do_not_duplicate_open_visits(self):
        member, subscription, code = self.active_card()
        request_id=uuid4()
        self.scan_card(code,request_id=request_id)
        self.assertContainsText(self.scan_card(code,request_id=request_id),"Previously processed scan")
        self.assertContainsText(self.scan_card(code),"Already checked in.")
        self.assertEqual(AttendanceVisit.objects.using(self.alias_a).count(),1)
        self.assertEqual(AttendanceScan.objects.using(self.alias_a).count(),2)

    def test_attendance_replay_after_checkout_does_not_open_another_visit(self):
        member, subscription, code = self.active_card()
        request_id=uuid4()
        self.scan_card(code,request_id=request_id)
        self.scan_card(code,"checkout")
        self.assertContainsText(self.scan_card(code,request_id=request_id),"No new entry or checkout was recorded.")
        self.assertFalse(AttendanceVisit.objects.using(self.alias_a).filter(checked_out_at__isnull=True).exists())
        self.assertContainsText(self.scan_card(code,"checkout",request_id=request_id),"already been used")
        self.assertEqual(AttendanceScan.objects.using(self.alias_a).count(),2)

    def test_attendance_concurrent_scans_are_serialized(self):
        member, subscription, code = self.active_card()
        def scan(_):
            try:
                with gym_database(self.alias_a):
                    return record_scan(self.alias_a,self.a,code,self.branch_a.pk,"checkin",uuid4(),self.user.pk).outcome
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes=list(executor.map(scan,range(2)))
        self.assertCountEqual(outcomes,["checked_in","already_in"])
        self.assertEqual(AttendanceVisit.objects.using(self.alias_a).count(),1)

    def test_attendance_denies_nonactive_future_and_expired_terms(self):
        member, subscription, code = self.active_card()
        for status in ("pending","frozen","cancelled"):
            with self.subTest(status=status):
                MemberSubscription.objects.using(self.alias_a).filter(pk=subscription.pk).update(status=status)
                self.assertContainsText(self.scan_card(code),"No active membership for today")
        today=timezone.localdate()
        for start,end in ((today+timedelta(days=1),today+timedelta(days=5)),(today-timedelta(days=3),today)):
            MemberSubscription.objects.using(self.alias_a).filter(pk=subscription.pk).update(status="active",starts_on=start,expires_on=end)
            self.assertContainsText(self.scan_card(code),"No active membership for today")
        self.assertFalse(AttendanceVisit.objects.using(self.alias_a).exists())

    def test_attendance_disabled_member_cannot_enter_but_can_exit(self):
        member, subscription, code = self.active_card()
        self.scan_card(code)
        MemberProfile.objects.using(self.alias_a).filter(pk=member.pk).update(is_active=False)
        self.assertContainsText(self.scan_card(code),"Member access is disabled.")
        self.assertContainsText(self.scan_card(code,"checkout"),"Checked out successfully.")
        MemberSubscription.objects.using(self.alias_a).filter(pk=subscription.pk).update(status="cancelled")
        self.assertContainsText(self.scan_card(code,"checkout"),"No open visit")

    def test_attendance_different_branch_cannot_duplicate_or_checkout_visit(self):
        member, subscription, code = self.active_card()
        other=Location.objects.using(self.alias_a).create(name="Second branch")
        self.scan_card(code)
        self.assertContainsText(self.scan_card(code,branch=other),"Already checked in at another branch")
        self.assertContainsText(self.scan_card(code,"checkout",branch=other),"Select that branch to check out")
        self.assertTrue(AttendanceVisit.objects.using(self.alias_a).filter(checked_out_at__isnull=True).exists())
        self.assertContainsText(self.scan_card(code,"checkout"),"Checked out successfully.")

    def test_attendance_disabled_branch_blocks_entry_but_allows_exit(self):
        member, subscription, code = self.active_card()
        self.scan_card(code)
        Location.objects.using(self.alias_a).filter(pk=self.branch_a.pk).update(is_active=False)
        self.assertContainsText(self.scan_card(code),"branch is disabled")
        self.assertContainsText(self.scan_card(code,"checkout"),"Checked out successfully.")
        self.assertContainsText(self.scan_card(code),"branch is disabled")
        self.assertEqual(AttendanceVisit.objects.using(self.alias_a).count(),1)

    def test_attendance_foreign_tampered_and_plain_member_codes_are_denied(self):
        member, subscription, code = self.active_card()
        with gym_database(self.alias_b):
            foreign=MemberProfile.objects.create(full_name="Foreign private name",email="foreign@example.com",phone="123")
        foreign_code=card_code(self.b,foreign)
        for value in (foreign_code,code[:-6]+"broken",member.member_number,"https://another.example.com/"):
            response=self.scan_card(value)
            self.assertContainsText(response,"Invalid or replaced card")
            self.assertNotIn(b"Foreign private name",response.content)
        self.assertFalse(AttendanceVisit.objects.using(self.alias_a).exists())
        self.assertFalse(AttendanceScan.objects.using(self.alias_b).exists())

    def test_attendance_card_replacement_invalidates_all_previous_copies(self):
        member, subscription, old_code=self.active_card()
        self.assertEqual(self.tenant_client().post(f"/manage/members/{member.pk}/card/replace/").status_code,302)
        member.refresh_from_db(using=self.alias_a)
        new_code=card_code(self.a,member)
        self.assertNotEqual(old_code,new_code)
        self.assertContainsText(self.scan_card(old_code),"Invalid or replaced card")
        self.assertContainsText(self.scan_card(new_code),"Entry allowed")

    def test_attendance_only_desk_roles_can_scan_and_read_attendance(self):
        member, subscription, code=self.active_card()
        for role in ("staff","manager"):
            user=self.desk_account(role)
            client=Client(HTTP_HOST=self.domain_a.hostname)
            client.force_login(user)
            self.assertEqual(client.get("/desk/").status_code,200)
            self.assertEqual(client.get("/desk/attendance/").status_code,200)
            for path in ("/manage/members/","/manage/payments/","/manage/staff/","/manage/branches/","/manage/website/"):
                self.assertEqual(client.get(path).status_code,404)
        account=self.desk_account("member")
        client=Client(HTTP_HOST=self.domain_a.hostname);client.force_login(account)
        self.assertEqual(client.get("/desk/").status_code,404)
        self.assertEqual(self.scan_card(code,client=client).status_code,404)
        self.assertEqual(client.get("/desk/attendance/").status_code,404)
        self.assertEqual(AttendanceScan.objects.using(self.alias_a).count(),0)

    def test_attendance_revoked_staff_sessions_lose_access_immediately(self):
        member, subscription, code=self.active_card()
        account=self.desk_account()
        client=Client(HTTP_HOST=self.domain_a.hostname);client.force_login(account)
        self.assertEqual(client.get("/desk/").status_code,200)
        access=Membership.objects.get(organization=self.a,user=account)
        self.assertEqual(self.tenant_client().post(f"/manage/staff/{access.pk}/access/",{"action":"disable"}).status_code,302)
        self.assertEqual(self.scan_card(code,client=client).status_code,404)
        self.assertEqual(client.get("/desk/attendance/").status_code,404)
        account.refresh_from_db();self.assertTrue(account.is_active)

    def test_attendance_owner_creates_unprivileged_gym_staff_without_scripts(self):
        data={"username":"newdesk-"+uuid4().hex,"email":"desk@example.com","password1":"Desk-Strong-84!","password2":"Desk-Strong-84!","role":"staff"}
        self.assertEqual(self.tenant_client().post("/manage/staff/",data).status_code,302)
        account=get_user_model().objects.get(username=data["username"])
        self.assertFalse(account.is_staff or account.is_superuser)
        self.assertTrue(Membership.objects.filter(organization=self.a,user=account,role="staff").exists())
        self.assertFalse(Membership.objects.filter(organization=self.b,user=account).exists())
        client=Client(HTTP_HOST=self.domain_a.hostname)
        self.assertEqual(client.post("/accounts/login/",{"username":account.username,"password":"Desk-Strong-84!"})["Location"],"/desk/")
        self.assertEqual(Client(HTTP_HOST=self.domain_b.hostname).post("/accounts/login/",{"username":account.username,"password":"Desk-Strong-84!"}).status_code,200)
        data.update({"username":"badrole-"+uuid4().hex,"role":"owner"})
        self.assertEqual(self.tenant_client().post("/manage/staff/",data).status_code,200)
        self.assertFalse(get_user_model().objects.filter(username=data["username"]).exists())

    def test_attendance_staff_access_cannot_modify_owners_or_another_gym(self):
        other=self.desk_account(gym=self.b)
        foreign=Membership.objects.get(organization=self.b,user=other)
        owner=Membership.objects.get(organization=self.a,user=self.user)
        for access in (owner,foreign):
            self.assertEqual(self.tenant_client().post(f"/manage/staff/{access.pk}/access/",{"action":"disable"}).status_code,404)
            access.refresh_from_db();self.assertTrue(access.is_active)

    def test_attendance_owner_can_create_edit_and_disable_branches(self):
        client=self.tenant_client()
        self.assertEqual(client.post("/manage/branches/",{"name":"New branch","address":"Owned address","is_active":"on"}).status_code,302)
        branch=Location.objects.using(self.alias_a).get(name="New branch")
        self.assertEqual(client.post(f"/manage/branches/{branch.pk}/",{"name":"Renamed branch","address":"Updated"}).status_code,302)
        branch.refresh_from_db(using=self.alias_a);self.assertFalse(branch.is_active)
        self.assertEqual(branch.name,"Renamed branch")
        self.assertFalse(Location.objects.using(self.alias_b).filter(name="Renamed branch").exists())

    def test_attendance_all_writes_require_csrf_and_get_never_records(self):
        member,subscription,code=self.active_card()
        client=self.tenant_client(csrf=True)
        self.assertEqual(client.post("/desk/",self.scan_data(code)).status_code,403)
        self.assertEqual(client.post(f"/manage/members/{member.pk}/card/replace/").status_code,403)
        self.assertEqual(client.post("/manage/branches/",{"name":"Invalid"}).status_code,403)
        self.assertEqual(client.post("/manage/staff/",{}).status_code,403)
        self.assertEqual(self.tenant_client().get("/desk/").status_code,200)
        self.assertEqual(self.tenant_client().get(f"/manage/members/{member.pk}/card/replace/").status_code,405)
        self.assertFalse(AttendanceVisit.objects.using(self.alias_a).exists())

    def test_attendance_manual_checkout_needs_reason_and_records_operator(self):
        member,subscription,code=self.active_card();self.scan_card(code)
        visit=AttendanceVisit.objects.using(self.alias_a).get()
        path=f"/desk/attendance/{visit.pk}/checkout/"
        self.assertEqual(self.tenant_client().get(path).status_code,405)
        self.tenant_client().post(path,{})
        visit.refresh_from_db(using=self.alias_a);self.assertIsNone(visit.checked_out_at)
        self.tenant_client().post(path,{"note":"Member forgot to scan when leaving"})
        visit.refresh_from_db(using=self.alias_a)
        self.assertEqual(visit.checkout_note,"Member forgot to scan when leaving")
        self.assertEqual(visit.checked_out_by_id,self.user.pk)
        recorded=visit.checked_out_at
        self.tenant_client().post(path,{"note":"Must not rewrite history"})
        visit.refresh_from_db(using=self.alias_a);self.assertEqual(visit.checked_out_at,recorded)

    def test_attendance_member_card_and_history_only_show_their_own_data(self):
        member,subscription,code=self.active_card();self.scan_card(code)
        token=self.invitation_token(member)
        client=Client(HTTP_HOST=self.domain_a.hostname);client.post(f"/activate/{token}/",self.claim_payload())
        card=client.get("/portal/card/")
        self.assertContainsText(card,member.member_number)
        self.assertIn("no-store",card["Cache-Control"])
        self.assertEqual(card["Referrer-Policy"],"no-referrer")
        image=client.get("/portal/card.svg")
        self.assertEqual(image["Content-Type"],"image/svg+xml")
        self.assertIn(b"<svg",image.content)
        self.assertContainsText(client.get("/portal/attendance/"),self.branch_a.name)
        self.assertEqual(client.get(f"/manage/members/{member.pk}/card/").status_code,404)
        MemberProfile.objects.using(self.alias_a).filter(pk=member.pk).update(is_active=False)
        self.assertEqual(client.get("/portal/card/").status_code,404)
        self.assertEqual(client.get("/portal/card.svg").status_code,404)
        self.assertEqual(client.get("/portal/attendance/").status_code,404)

    def test_attendance_filters_reject_invalid_input_and_respect_gym(self):
        member,subscription,code=self.active_card();self.scan_card(code)
        client=self.tenant_client()
        self.assertContainsText(client.get("/desk/attendance/?open_only=on"),member.full_name)
        response=client.get("/desk/attendance/?date=not-a-date")
        self.assertEqual(response.status_code,200)
        self.assertContainsText(response,"No visits match")
        response=client.get("/desk/attendance/?date="+(timezone.localdate()-timedelta(days=1)).isoformat())
        self.assertContainsText(response,"No visits match")
        self.assertEqual(self.tenant_client(self.domain_b).get("/desk/attendance/").status_code,404)

    def test_attendance_scan_audit_never_stores_raw_credentials(self):
        member,subscription,code=self.active_card();self.scan_card(code)
        scan=AttendanceScan.objects.using(self.alias_a).get()
        self.assertNotIn(code,str(scan.__dict__))
        self.assertEqual(len(scan.fingerprint),64)
        self.assertEqual(scan.actor_id,self.user.pk)
        self.assertEqual(scan.outcome,"checked_in")
        for table in ("gym_core_attendancevisit","gym_core_attendancescan"):
            self.assertNotIn(table,connection.introspection.table_names())
            self.assertIn(table,connections[self.alias_a].introspection.table_names())
            self.assertIn(table,connections[self.alias_b].introspection.table_names())

    def test_attendance_upgrade_button_adds_module_without_losing_members(self):
        from django.db.migrations.recorder import MigrationRecorder
        member,subscription,code=self.active_card()
        with connections[self.alias_a].schema_editor() as editor:
            for model in (RenewalRequest, MemberProfileChange, EmailAttempt, EmailNotification, NotificationSettings, BookingEvent, ClassBooking, ClassSession, Trainer):
                # Empty notification tables belong to a later schema than this fixture.
                editor.delete_model(model)
            editor.delete_model(AttendanceScan);editor.delete_model(AttendanceVisit)
            editor.remove_field(MemberProfile,MemberProfile._meta.get_field("qr_version"))
            editor.remove_field(MemberProfile,MemberProfile._meta.get_field("email_notifications"))
        MigrationRecorder.Migration.objects.using(self.alias_a).filter(app="gym_core",name__in=["0004_memberprofile_qr_version_attendancevisit_and_more",
            "0005_trainer_classbooking_bookingevent_classsession_and_more",
            "0006_memberprofile_email_notifications_emailnotification_and_more",
            "0007_notificationsettings_membership_updates_and_more"]).delete()
        self.user.is_superuser=True;self.user.save()
        self.assertContainsText(self.client.get(f"/gyms/{self.a.slug}/"),"Retry setup")
        self.assertEqual(self.client.post(f"/gyms/{self.a.slug}/retry-setup/").status_code,302)
        member.refresh_from_db(using=self.alias_a)
        self.assertEqual(member.full_name,"Jamie Applicant")
        self.assertIsNotNone(member.qr_version)
        self.assertEqual(AttendanceVisit.objects.using(self.alias_a).count(),0)
        self.assertEqual(MemberSubscription.objects.using(self.alias_a).count(),1)


    def saas_plan(self, **updates):
        data={"name":"Platform plan "+uuid4().hex,"price":Decimal("2999.00"),"period":"month"}
        data.update(updates)
        return SaaSPlan.objects.create(**data)

    def saas_assignment(self, gym=None, plan=None, trial=0, grace=0):
        subscription=assign_saas_plan((gym or self.a).pk,(plan or self.saas_plan()).pk,trial,grace,self.user.pk)
        return subscription,subscription.invoices.get(status="issued")

    def make_platform_admin(self):
        self.user.is_superuser=True;self.user.save()
        return self.client

    def settle_saas(self, invoice, reference=None):
        return record_saas_payment(invoice.pk,self.user.pk,reference or "BANK-"+uuid4().hex,
            "complimentary" if invoice.amount==0 else "bank","Receipt verified by platform operator")

    def central_fixture(self, environment="sandbox"):
        from platform_core.collections import gateway as central
        member, term, order, config = self.copay_order(environment)
        order.status = "submitting"
        order.central_collection = True
        order.save(using=self.alias_a)
        row = central.register_collection(self.a, order)
        return member, term, order, config, row

    def payout_destination(self, gym=None, verified=True):
        from platform_core.models import PayoutDestination
        return PayoutDestination.objects.create(organization=gym or self.a, bank="Test bank", account_name="Gym Owner",
            account_number="TEST-"+uuid4().hex, created_by=self.user, verified_by=self.user if verified else None,
            verified_at=timezone.now() if verified else None)

    def test_central_collection_credits_only_correct_gym_once_and_not_platform_revenue(self):
        from platform_core.collections import gateway as central
        from platform_core.models import GymWallet, WalletEntry
        member, term, order, config, row = self.central_fixture()
        result = self.copay_result(order)
        central.apply_result(row.pk,result)
        central.apply_result(row.pk,result)
        wallet = GymWallet.objects.get(organization=self.a,environment="sandbox")
        self.assertEqual(wallet.pending,order.amount)
        self.assertEqual(wallet.available,0)
        self.assertEqual(WalletEntry.objects.filter(wallet=wallet,action="collection").count(),1)
        self.assertFalse(GymWallet.objects.filter(organization=self.b).exists())
        self.assertEqual(PlatformPayment.objects.count(),0)
        central.sync_tenant(row.pk)
        with gym_database(self.alias_a):
            term.refresh_from_db()
            self.assertEqual(term.status,"pending")
            order.refresh_from_db()
            self.assertEqual(order.status,"paid")
        row.refresh_from_db()
        self.assertTrue(row.tenant_synced)

    def test_central_verified_production_sync_activates_member_without_paying_saas(self):
        from platform_core.collections import gateway as central
        member,term,order,config,row=self.central_fixture("production")
        subscription,invoice=self.saas_assignment()
        central.apply_result(row.pk,self.copay_result(order))
        central.sync_tenant(row.pk)
        with gym_database(self.alias_a):
            term.refresh_from_db()
            self.assertEqual(term.status,"active")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status,"issued")
        self.assertFalse(access_for_gym(self.a).allowed)

    def test_central_credit_survives_tenant_outage_and_recovers_without_duplicate(self):
        from platform_core.collections import gateway as central
        from platform_core.models import GymWallet,WalletEntry
        member,term,order,config,row=self.central_fixture()
        result=self.copay_result(order)
        central.apply_result(row.pk,result)
        with patch.object(central,"ready_database",side_effect=OSError("unavailable")), self.assertRaises(OSError):
            central.sync_tenant(row.pk)
        row.refresh_from_db();self.assertFalse(row.tenant_synced)
        central.apply_result(row.pk,result)
        central.sync_tenant(row.pk)
        self.assertEqual(WalletEntry.objects.filter(wallet__organization=self.a,action="collection").count(),1)
        self.assertEqual(GymWallet.objects.get(organization=self.a,environment="sandbox").pending,order.amount)

    def test_central_callback_signature_and_immutable_registry_select_correct_tenant(self):
        from platform_core.collections import gateway as central
        from platform_core.models import GymWallet
        member,term,order,config,row=self.central_fixture()
        body=self.copay_callback(order,config)
        with patch.object(central,"central_config",return_value=config),patch.object(CopayGateway,"requery",return_value=self.copay_result(order)):
            path="/collections/copay/notify/"
            self.assertEqual(self.client.post(path,body,content_type="application/json").content,b"success")
            self.assertEqual(self.client.post(path,body,content_type="application/json").content,b"success")
            bad=json.loads(body);bad["Signature"]=base64.b64encode(b"bad").decode()
            self.assertEqual(self.client.post(path,json.dumps(bad),content_type="application/json").status_code,400)
            self.assertEqual(self.tenant_client().post(path,body,content_type="application/json").status_code,404)
        self.assertFalse(GymWallet.objects.filter(organization=self.b).exists())

    def test_central_release_and_withdrawal_reserve_and_manual_payout_audit(self):
        from platform_core.collections import gateway as central,ledger
        from platform_core.models import GymWallet,WalletEntry
        member,term,order,config,row=self.central_fixture()
        central.apply_result(row.pk,self.copay_result(order))
        destination=self.payout_destination()
        with self.assertRaises(ledger.LedgerError):
            ledger.request_withdrawal(self.a,"sandbox",destination.pk,"1",self.user.pk,uuid4())
        ledger.release(row.pk,self.user.pk,"TEST settlement statement")
        ledger.release(row.pk,self.user.pk,"TEST settlement statement replay")
        key=uuid4()
        withdrawal=ledger.request_withdrawal(self.a,"sandbox",destination.pk,"10",self.user.pk,key)
        duplicate=ledger.request_withdrawal(self.a,"sandbox",destination.pk,"10",self.user.pk,key)
        self.assertEqual(withdrawal.pk,duplicate.pk)
        wallet=GymWallet.objects.get(organization=self.a,environment="sandbox")
        self.assertEqual(wallet.pending,0);self.assertEqual(wallet.reserved,10)
        self.assertEqual(wallet.available,order.amount-10)
        ledger.transition(withdrawal.pk,"approve",self.user.pk,"Reviewed")
        ledger.transition(withdrawal.pk,"process",self.user.pk,"Manual transfer in progress")
        with self.assertRaises(ledger.LedgerError):
            ledger.transition(withdrawal.pk,"cancel",self.user.pk,"Uncertain transfer",gym=self.a)
        ledger.transition(withdrawal.pk,"pay",self.user.pk,"Confirmed manual payout","TESTPAYOUT-"+key.hex)
        ledger.transition(withdrawal.pk,"pay",self.user.pk,"Replay","TESTPAYOUT-"+key.hex)
        wallet.refresh_from_db();self.assertEqual(wallet.reserved,0)
        self.assertEqual(WalletEntry.objects.filter(wallet=wallet,action="payout").count(),1)

    def test_central_destination_verification_and_cross_gym_withdrawal_blocked(self):
        from platform_core.collections import ledger
        from platform_core.models import GymWallet
        destination=self.payout_destination(verified=False)
        wallet=ledger.wallet_for(self.a,"sandbox")
        ledger.adjust(wallet.pk,"available","100",self.user.pk,uuid4().hex,"Test correction")
        for target in [destination,self.payout_destination(gym=self.b)]:
            with self.assertRaises(ledger.LedgerError):
                ledger.request_withdrawal(self.a,"sandbox",target.pk,"50",self.user.pk,uuid4())
        wallet.refresh_from_db();self.assertEqual(wallet.available,100);self.assertEqual(wallet.reserved,0)

    def test_central_concurrent_withdrawals_cannot_overdraw(self):
        from platform_core.collections import ledger
        destination=self.payout_destination()
        wallet=ledger.wallet_for(self.a,"sandbox")
        ledger.adjust(wallet.pk,"available","100",self.user.pk,uuid4().hex,"Test opening funds")
        def withdraw(_):
            try:
                ledger.request_withdrawal(self.a,"sandbox",destination.pk,"80",self.user.pk,uuid4())
                return True
            except ledger.LedgerError:
                return False
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(withdraw,range(2))),[False,True])
        wallet.refresh_from_db();self.assertEqual(wallet.available,20);self.assertEqual(wallet.reserved,80)

    def test_central_rejection_refunds_reservation_once_and_adjustment_cannot_overdraw(self):
        from platform_core.collections import ledger
        wallet=ledger.wallet_for(self.a,"sandbox")
        ledger.adjust(wallet.pk,"available","100",self.user.pk,uuid4().hex,"Test funds")
        row=ledger.request_withdrawal(self.a,"sandbox",self.payout_destination().pk,"80",self.user.pk,uuid4())
        ledger.transition(row.pk,"reject",self.user.pk,"Destination not accepted")
        ledger.transition(row.pk,"reject",self.user.pk,"Replay")
        wallet.refresh_from_db();self.assertEqual(wallet.available,100);self.assertEqual(wallet.reserved,0)
        with self.assertRaises(ledger.LedgerError):
            ledger.adjust(wallet.pk,"available","-101",self.user.pk,uuid4().hex,"Invalid refund")
        wallet.refresh_from_db();self.assertEqual(wallet.available,100)

    def test_central_owner_wallet_isolated_and_role_post_csrf_enforced(self):
        from platform_core.collections import ledger
        wallet=ledger.wallet_for(self.a,"sandbox")
        ledger.adjust(wallet.pk,"available","100",self.user.pk,uuid4().hex,"Visible only to gym A")
        other=ledger.wallet_for(self.b,"sandbox")
        ledger.adjust(other.pk,"available","9876",self.user.pk,uuid4().hex,"PRIVATE GYM B LEDGER")
        response=self.tenant_client().get("/manage/wallet/")
        self.assertContainsText(response,"100.00")
        self.assertNotIn(b"PRIVATE GYM B LEDGER",response.content)
        desk=Client(HTTP_HOST=self.domain_a.hostname);desk.force_login(self.desk_account())
        self.assertEqual(desk.get("/manage/wallet/").status_code,404)
        self.assertEqual(self.client.get("/collections/").status_code,403)
        self.make_platform_admin()
        self.assertEqual(self.client.get("/collections/").status_code,200)
        path=f"/collections/wallets/{wallet.pk}/adjust/"
        self.assertEqual(self.client.get(path).status_code,405)
        csrf=Client(HTTP_HOST="localhost",enforce_csrf_checks=True);csrf.force_login(self.user)
        self.assertEqual(csrf.post(path).status_code,403)
        owner=self.tenant_client()
        self.assertEqual(owner.post("/manage/wallet/destinations/",{"bank":"UI-BANK","account_name":"Gym A","account_number":"TEST-1234"}).status_code,302)
        from platform_core.models import PayoutDestination,GymWithdrawal
        destination=PayoutDestination.objects.get(organization=self.a,bank="UI-BANK")
        verify=f"/collections/destinations/{destination.pk}/verify/"
        self.assertEqual(self.client.post(verify,{"note":"Account ownership independently checked"}).status_code,302)
        key=uuid4()
        payload={"environment":"sandbox","amount":"20.00","destination":str(destination.pk),"request_key":str(key)}
        with patch.object(CopayGateway,"request") as provider:
            self.assertEqual(owner.post("/manage/wallet/",payload).status_code,302)
            self.assertEqual(owner.post("/manage/wallet/",payload).status_code,302)
            withdrawal=GymWithdrawal.objects.get(request_key=key)
            for action in ["approve","process","pay"]:
                self.assertEqual(self.client.post(f"/collections/withdrawals/{withdrawal.pk}/",{
                    "action":action,"note":"Verified manual workflow","reference":"UI-"+key.hex}).status_code,302)
            provider.assert_not_called()
        withdrawal.refresh_from_db();self.assertEqual(withdrawal.status,"paid")
        wallet.refresh_from_db();self.assertEqual(wallet.available,80);self.assertEqual(wallet.reserved,0)

    @override_settings(CENTRAL_COLLECTIONS_ENABLED=True,CENTRAL_COLLECTIONS_PRODUCTION_APPROVED=False)
    def test_central_production_disabled_without_approval_and_sandbox_config_shared(self):
        from platform_core.collections import gateway as central
        config=self.copay_config("production")
        with patch.object(central,"central_config",return_value=config), self.assertRaises(CopayConfigurationError):
            config_for_gym(self.a)
        config=self.copay_config("sandbox")
        with patch.object(central,"central_config",return_value=config):
            a=config_for_gym(self.a);b=config_for_gym(self.b)
            self.assertEqual(a.merchant_code,b.merchant_code)
            self.assertNotEqual(a.public_origin,b.public_origin)
            member,term=self.enrolled_member()
            with gym_database(self.alias_a):
                order=create_order(self.alias_a,self.a,term.pk)
                self.assertTrue(order.central_collection)
                with patch.object(CopayGateway,"request",return_value={"transactionId":"CENTRAL-"+order.pk.hex,"url":"https://cashier.marvelpay.net/pay"}) as provider:
                    for _ in range(2):
                        started=start_checkout(self.alias_a,self.a,order.pk,"203.0.113.1")
                self.assertEqual(provider.call_count,1)
                self.assertEqual(started.status,"pending")
            from platform_core.models import CentralCollection
            self.assertEqual(CentralCollection.objects.filter(organization=self.a,tenant_order_id=order.pk).count(),1)

    def test_central_received_net_credited_and_short_payment_does_not_activate(self):
        from platform_core.collections import gateway as central
        from platform_core.models import GymWallet
        member,term,order,config,row=self.central_fixture("production")
        with self.assertRaises(CopayError):
            central.apply_result(row.pk,self.copay_result(order,netAmount=str(order.amount+1)))
        result=self.copay_result(order,netAmount="1.00")
        central.apply_result(row.pk,result);central.sync_tenant(row.pk)
        wallet=GymWallet.objects.get(organization=self.a,environment="production")
        self.assertEqual(wallet.pending,1)
        with gym_database(self.alias_a):
            term.refresh_from_db();self.assertEqual(term.status,"pending")

    def test_central_zero_invalid_and_tampered_collection_results_rejected(self):
        from platform_core.collections import gateway as central,ledger
        member,term,order,config,row=self.central_fixture()
        result=self.copay_result(order)
        for changes in [{"amount":"1.00"},{"currencyCode":"USD"},{"merchantTransactionId":"wrong"},{"paymentMethodCode":"MYP"},{"statusId":"6"}]:
            with self.assertRaises(CopayError):
                central.apply_result(row.pk,{**result,**changes})
        for value in ["0","-1","NaN","1.001"]:
            with self.assertRaises(ledger.LedgerError):ledger.amount(value)

    def test_saas_unmanaged_gyms_keep_existing_operations(self):
        self.assertEqual(access_for_gym(self.a).state,"Unmanaged")
        self.assertTrue(access_for_gym(self.a).allowed)
        self.assertEqual(self.tenant_client().get("/manage/").status_code,200)
        self.assertEqual(self.tenant_client().get("/desk/").status_code,200)
        self.assertFalse(GymSubscription.objects.filter(organization=self.a).exists())

    def test_saas_platform_plan_crud_is_superuser_only_and_rejects_negative_prices(self):
        self.assertEqual(self.client.get("/billing/").status_code,403)
        self.assertEqual(self.client.post("/billing/plans/new/",{"name":"Unauthorized"}).status_code,403)
        client=self.make_platform_admin()
        name="Published SaaS "+uuid4().hex
        data={"name":name,"price":"2999.00","period":"month","websites":"on","memberships":"on","attendance":"on","payments":"on","is_active":"on"}
        self.assertEqual(client.post("/billing/plans/new/",data).status_code,302)
        plan=SaaSPlan.objects.get(name=name)
        data["price"]="-1"
        self.assertEqual(client.post(f"/billing/plans/{plan.pk}/",data).status_code,200)
        plan.refresh_from_db();self.assertEqual(plan.price,Decimal("2999.00"))
        self.assertEqual(self.tenant_client().get("/billing/plans/").status_code,404)

    def test_saas_assignment_snapshots_quote_and_issues_first_invoice_once(self):
        plan=self.saas_plan()
        subscription,invoice=self.saas_assignment(plan=plan,trial=14,grace=3)
        self.assertEqual(invoice.period_start,timezone.localdate()+timedelta(days=14))
        self.assertEqual(invoice.amount,plan.price)
        self.assertEqual(access_for_gym(self.a).state,"Trial")
        SaaSPlan.objects.filter(pk=plan.pk).update(price="4999.00",attendance=False)
        invoice.refresh_from_db();subscription.refresh_from_db()
        self.assertEqual(invoice.amount,Decimal("2999.00"));self.assertTrue(invoice.modules["attendance"])
        self.assertEqual(subscription.price,Decimal("2999.00"))
        with self.assertRaises(BillingError):
            assign_saas_plan(self.a.pk,plan.pk,14,3,self.user.pk)
        self.assertEqual(subscription.invoices.count(),1)

    def test_saas_gym_onboarding_can_assign_plan_without_per_gym_scripts(self):
        client=self.make_platform_admin();plan=self.saas_plan()
        data={"name":"Onboard SaaS","slug":"saas-"+uuid4().hex,"primary_color":"#123456",
              "saas_plan":str(plan.pk),"trial_days":"7","grace_days":"2",**self.owner_payload()}
        self.assertEqual(client.post("/gyms/new/",data).status_code,302)
        gym=Organization.objects.get(slug=data["slug"])
        self.assertTrue(gym.database_ready)
        self.assertEqual(GymSubscription.objects.get(organization=gym).trial_ends_on,timezone.localdate()+timedelta(days=7))
        self.assertEqual(PlatformInvoice.objects.filter(subscription__organization=gym).count(),1)

    def test_saas_verified_recording_uses_invoice_amount_and_creates_next_invoice(self):
        subscription,invoice=self.saas_assignment()
        self.assertFalse(access_for_gym(self.a).allowed)
        client=self.make_platform_admin()
        path=f"/billing/invoices/{invoice.pk}/payment/"
        data={"reference":"TRANSFER-"+uuid4().hex,"method":"bank","note":"Matched bank receipt","amount":"0.01"}
        self.assertEqual(client.post(path,data).status_code,302)
        self.assertEqual(client.post(path,data).status_code,302)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status,"paid")
        payment=PlatformPayment.objects.get(invoice=invoice)
        self.assertEqual(payment.amount,Decimal("2999.00"))
        self.assertEqual(subscription.invoices.filter(status="issued").count(),1)
        next_invoice=subscription.invoices.get(status="issued")
        self.assertEqual(next_invoice.period_start,invoice.period_end)
        self.assertTrue(access_for_gym(self.a).allowed)
        self.assertEqual(SubscriptionEvent.objects.filter(subscription=subscription,action="payment_recorded").count(),1)

    def test_saas_reference_cannot_settle_two_invoices_even_with_changed_case(self):
        subscription,first=self.saas_assignment()
        reference="Transfer-"+uuid4().hex
        self.settle_saas(first,reference)
        second=subscription.invoices.get(status="issued")
        with self.assertRaises(BillingError):
            self.settle_saas(second,reference.upper())
        second.refresh_from_db();self.assertEqual(second.status,"issued")
        self.assertFalse(PlatformPayment.objects.filter(invoice=second).exists())
        with self.assertRaises(BillingError):
            self.settle_saas(first,"Different-"+uuid4().hex)

    def test_saas_expiry_and_grace_are_enforced_by_exclusive_dates(self):
        subscription,invoice=self.saas_assignment(grace=3)
        self.settle_saas(invoice)
        self.assertEqual(access_for_gym(self.a,today=invoice.period_end-timedelta(days=1)).state,"Active")
        self.assertEqual(access_for_gym(self.a,today=invoice.period_end).state,"Grace period")
        self.assertTrue(access_for_gym(self.a,today=invoice.period_end+timedelta(days=2)).allowed)
        self.assertFalse(access_for_gym(self.a,today=invoice.period_end+timedelta(days=3)).allowed)
        self.assertFalse(access_for_gym(self.a,today=invoice.period_end+timedelta(days=4)).allowed)

    def test_saas_plan_change_preserves_paid_and_trial_entitlements_until_next_term(self):
        original=self.saas_plan();replacement=self.saas_plan(price=Decimal("3999.00"),attendance=False)
        subscription,invoice=self.saas_assignment(plan=original,trial=7)
        self.settle_saas(invoice)
        old_pending=subscription.invoices.get(status="issued")
        next_invoice=change_saas_plan(subscription.pk,replacement.pk,self.user.pk,"Changed package")
        self.assertEqual(change_saas_plan(subscription.pk,replacement.pk,self.user.pk,"Retried plan change").pk,next_invoice.pk)
        old_pending.refresh_from_db();self.assertEqual(old_pending.status,"void")
        self.assertTrue(access_for_gym(self.a).modules["attendance"])
        self.settle_saas(next_invoice)
        self.assertTrue(access_for_gym(self.a,today=invoice.period_start).modules["attendance"])
        self.assertFalse(access_for_gym(self.a,today=invoice.period_end).modules["attendance"])
        self.assertEqual(next_invoice.amount,Decimal("3999.00"))
        self.assertEqual(subscription.invoices.filter(status="issued").count(),1)

    def test_saas_future_payment_does_not_unlock_current_uncovered_days(self):
        subscription,invoice=self.saas_assignment()
        invoice.period_start+=timedelta(days=1);invoice.save(update_fields=["period_start"])
        self.settle_saas(invoice)
        self.assertFalse(access_for_gym(self.a).allowed)
        self.assertTrue(access_for_gym(self.a,today=invoice.period_start).allowed)

    def test_saas_payment_does_not_remove_explicit_suspension_and_resume_checks_coverage(self):
        subscription,invoice=self.saas_assignment()
        change_saas_state(subscription.pk,"suspend",self.user.pk,"Operator suspension")
        self.settle_saas(invoice)
        self.assertEqual(access_for_gym(self.a).state,"Suspended")
        change_saas_state(subscription.pk,"resume",self.user.pk,"Issue resolved")
        self.assertTrue(access_for_gym(self.a).allowed)
        self.assertFalse(access_for_gym(self.a,today=invoice.period_end).allowed)

    def test_saas_cancellation_preserves_paid_history_and_gym_database(self):
        member,term,code=self.active_card()
        subscription,invoice=self.saas_assignment();self.settle_saas(invoice)
        unpaid=subscription.invoices.get(status="issued")
        change_saas_state(subscription.pk,"cancel",self.user.pk,"Agreement ended")
        unpaid.refresh_from_db();invoice.refresh_from_db()
        self.assertEqual(unpaid.status,"void");self.assertEqual(invoice.status,"paid")
        self.assertEqual(access_for_gym(self.a).state,"Cancelled")
        self.assertTrue(MemberProfile.objects.using(self.alias_a).filter(pk=member.pk).exists())
        with self.assertRaises(BillingError):self.settle_saas(unpaid)
        with self.assertRaises(BillingError):change_saas_state(subscription.pk,"resume",self.user.pk,"Cannot resume cancellation")

    def test_saas_expired_operations_block_before_tenant_queries_but_public_read_stays_live(self):
        member,term,code=self.active_card()
        subscription,invoice=self.saas_assignment()
        client=self.tenant_client()
        with patch("websites.branding.website_for_request",side_effect=AssertionError("Tenant query after gate")), patch(
                "gym_core.attendance.views.record_scan",side_effect=AssertionError("Attendance bypass")):
            response=client.post("/desk/",self.scan_data(code))
            self.assertEqual(response.status_code,503)
            self.assertIn(b"Open platform billing",response.content)
        self.assertFalse(AttendanceVisit.objects.using(self.alias_a).exists())
        self.assertEqual(client.get("/manage/").status_code,503)
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).get("/").status_code,200)
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).post("/join/",{}).status_code,503)
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).post("/contact/",{}).status_code,503)
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).get("/accounts/login/").status_code,200)
        self.assertEqual(client.get("/billing/").status_code,200)
        self.settle_saas(invoice)
        self.assertEqual(client.get("/desk/").status_code,200)

    def test_saas_module_entitlements_block_server_side_requests(self):
        member,term,code=self.active_card()
        plan=self.saas_plan(attendance=False,payments=False,memberships=False,websites=True)
        subscription,invoice=self.saas_assignment(plan=plan,trial=14)
        client=self.tenant_client()
        self.assertEqual(client.post("/desk/",self.scan_data(code)).status_code,503)
        self.assertEqual(client.get("/manage/members/").status_code,503)
        self.assertEqual(client.get("/manage/payments/").status_code,503)
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).post("/join/",{}).status_code,503)
        self.assertEqual(client.get("/manage/website/").status_code,200)
        self.assertEqual(client.get("/manage/").status_code,200)
        self.assertEqual(client.get("/billing/").status_code,200)

    def test_saas_existing_copay_callbacks_reconcile_without_paying_platform_invoice(self):
        member,term,order,config=self.copay_order()
        PaymentOrder.objects.using(self.alias_a).filter(pk=order.pk).update(status="submitting")
        subscription,invoice=self.saas_assignment()
        with patch("payments.services.config_for_gym",return_value=config),patch.object(CopayGateway,"requery",return_value=self.copay_result(order)):
            response=Client(HTTP_HOST=self.domain_a.hostname).post("/payments/copay/notify/",self.copay_callback(order,config),content_type="application/json")
        self.assertEqual(response.content,b"success")
        invoice.refresh_from_db();self.assertEqual(invoice.status,"issued")
        self.assertFalse(PlatformPayment.objects.filter(invoice=invoice).exists())
        order.refresh_from_db(using=self.alias_a);self.assertEqual(order.status,"paid")
        self.assertFalse(access_for_gym(self.a).allowed)

    def test_saas_owner_billing_is_host_scoped_and_does_not_need_tenant_database(self):
        subscription,invoice=self.saas_assignment()
        other_subscription,other_invoice=self.saas_assignment(gym=self.b)
        Membership.objects.create(organization=self.b,user=self.user,role="owner")
        Organization.objects.filter(pk=self.a.pk).update(database_ready=False)
        client=self.tenant_client()
        with patch("websites.branding.website_for_request",side_effect=AssertionError("Billing used tenant DB")):
            self.assertEqual(client.get("/billing/").status_code,200)
            self.assertEqual(client.get(f"/billing/invoices/{invoice.pk}/").status_code,200)
            self.assertEqual(client.get(f"/billing/invoices/{other_invoice.pk}/").status_code,404)
        self.assertEqual(client.post("/billing/",{}).status_code,405)
        self.assertEqual(client.post(f"/billing/invoices/{invoice.pk}/payment/",{}).status_code,404)

    def test_saas_staff_and_members_cannot_read_owner_billing_or_edit_platform(self):
        subscription,invoice=self.saas_assignment(trial=14)
        for role in ("staff","manager","member"):
            account=self.desk_account(role)
            client=Client(HTTP_HOST=self.domain_a.hostname);client.force_login(account)
            self.assertEqual(client.get("/billing/").status_code,404)
            self.assertEqual(client.get(f"/billing/invoices/{invoice.pk}/").status_code,404)
            platform=Client(HTTP_HOST="localhost");platform.force_login(account)
            self.assertEqual(platform.get("/billing/").status_code,403)

    def test_saas_mutations_require_csrf_post_reference_and_reason(self):
        subscription,invoice=self.saas_assignment()
        self.make_platform_admin()
        client=Client(HTTP_HOST="localhost",enforce_csrf_checks=True);client.force_login(self.user)
        path=f"/billing/invoices/{invoice.pk}/payment/"
        self.assertEqual(client.post(path,{"reference":"bad"}).status_code,403)
        self.assertEqual(self.client.get(path).status_code,405)
        self.client.post(path,{"reference":"MISSING-NOTE","method":"bank"})
        self.assertFalse(PlatformPayment.objects.filter(invoice=invoice).exists())
        self.client.post(f"/gyms/{self.a.slug}/subscription/state/",{"action":"suspend","note":""})
        subscription.refresh_from_db();self.assertIsNone(subscription.suspended_at)

    def test_saas_archive_does_not_reprice_or_cancel_existing_agreements(self):
        plan=self.saas_plan();subscription,invoice=self.saas_assignment(plan=plan,trial=14)
        SaaSPlan.objects.filter(pk=plan.pk).update(is_active=False,price="9999.00")
        self.assertTrue(access_for_gym(self.a).allowed)
        self.settle_saas(invoice)
        next_invoice=subscription.invoices.get(status="issued")
        self.assertEqual(next_invoice.amount,Decimal("2999.00"))
        self.make_platform_admin()
        response=self.client.post(f"/gyms/{self.b.slug}/subscription/",{"plan":str(plan.pk),"trial_days":"14","grace_days":"0"})
        self.assertEqual(response.status_code,200)
        self.assertFalse(GymSubscription.objects.filter(organization=self.b).exists())

    def test_saas_tables_exist_only_in_platform_database(self):
        central=connection.introspection.table_names()
        for table in ("platform_core_saasplan","platform_core_gymsubscription","platform_core_platforminvoice","platform_core_platformpayment","platform_core_subscriptionevent"):
            self.assertIn(table,central)
            self.assertNotIn(table,connections[self.alias_a].introspection.table_names())
            self.assertNotIn(table,connections[self.alias_b].introspection.table_names())

    def test_saas_concurrent_confirmations_do_not_duplicate_payments_or_next_invoice(self):
        subscription,invoice=self.saas_assignment()
        reference="CONCURRENT-"+uuid4().hex
        def pay(_):
            try:return self.settle_saas(invoice,reference).pk
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(pay,range(2)))
        self.assertEqual(results,[invoice.pk,invoice.pk])
        self.assertEqual(PlatformPayment.objects.filter(invoice=invoice).count(),1)
        self.assertEqual(subscription.invoices.filter(status="issued").count(),1)

    def test_saas_zero_price_requires_explicit_complimentary_confirmation(self):
        subscription,invoice=self.saas_assignment(plan=self.saas_plan(price=Decimal("0.00")))
        with self.assertRaises(BillingError):record_saas_payment(invoice.pk,self.user.pk,"NO-CHARGE-"+uuid4().hex,"bank","Wrong method")
        self.settle_saas(invoice)
        self.assertTrue(access_for_gym(self.a).allowed)
        self.assertEqual(PlatformPayment.objects.get(invoice=invoice).amount,Decimal("0.00"))


    def retail_product(self, alias=None, **updates):
        data={"sku":"SKU-"+uuid4().hex[:12],"name":"Test water bottle","price":Decimal("25.00")}
        data.update(updates)
        return Product.objects.using(alias or self.alias_a).create(**data)

    def retail_stock(self, product, quantity=10, branch=None):
        with gym_database(self.alias_a):
            return adjust_stock(self.alias_a,product.pk,(branch or self.branch_a).pk,quantity,"Initial received stock",uuid4(),self.user.pk)

    def retail_quote(self, product=None, quantity=1, actor=None, branch=None):
        product=product or self.retail_product()
        if not StockBalance.objects.using(self.alias_a).filter(product=product,location=branch or self.branch_a).exists():
            self.retail_stock(product,10,branch)
        with gym_database(self.alias_a):
            token,data=quote_cart(self.a,(actor or self.user).pk,(branch or self.branch_a).pk,[(product.pk,quantity)],self.a.name)
        return product,token,data

    def retail_sale(self, product=None, quantity=1, actor=None, branch=None):
        product,token,data=self.retail_quote(product,quantity,actor,branch)
        with gym_database(self.alias_a):
            sale=commit_sale(self.alias_a,self.a,(actor or self.user).pk,token,"cash",Decimal("1000.00"))
        return product,sale,token

    def test_pos_owner_manages_products_with_unique_sku_and_nonnegative_price(self):
        client=self.tenant_client()
        data={"sku":"wtr-"+uuid4().hex[:8],"name":"Water bottle","price":"25.00","low_stock_threshold":"4","is_active":"on"}
        self.assertEqual(client.post("/manage/products/new/",data).status_code,302)
        product=Product.objects.using(self.alias_a).get(sku=data["sku"].upper())
        data["sku"]=product.sku.lower()
        self.assertEqual(client.post("/manage/products/new/",data).status_code,200)
        data["price"]="-1"
        self.assertEqual(client.post(f"/manage/products/{product.pk}/",data).status_code,200)
        product.refresh_from_db(using=self.alias_a);self.assertEqual(product.price,Decimal("25.00"))
        self.assertContainsText(client.get("/manage/products/?q=Water"),"Water bottle")
        self.assertFalse(Product.objects.using(self.alias_b).exists())

    def test_pos_stock_adjustments_are_audited_idempotent_and_cannot_go_negative(self):
        product=self.retail_product();request_id=uuid4()
        with gym_database(self.alias_a):
            first=adjust_stock(self.alias_a,product.pk,self.branch_a.pk,10,"Supplier delivery",request_id,self.user.pk)
            self.assertEqual(adjust_stock(self.alias_a,product.pk,self.branch_a.pk,10,"Supplier delivery",request_id,self.user.pk).pk,first.pk)
            with self.assertRaises(PosError):adjust_stock(self.alias_a,product.pk,self.branch_a.pk,9,"Different",request_id,self.user.pk)
            with self.assertRaises(PosError):adjust_stock(self.alias_a,product.pk,self.branch_a.pk,-11,"Too much",uuid4(),self.user.pk)
            with self.assertRaises(PosError):adjust_stock(self.alias_a,product.pk,self.branch_a.pk,0,"Zero",uuid4(),self.user.pk)
        balance=StockBalance.objects.using(self.alias_a).get(product=product)
        self.assertEqual(balance.quantity,10)
        self.assertEqual(StockMovement.objects.using(self.alias_a).filter(balance=balance).count(),1)
        self.assertEqual(first.balance_after,10)

    def test_pos_cash_sale_uses_signed_prices_and_server_calculated_change(self):
        product,token,data=self.retail_quote(quantity=3)
        response=self.tenant_client().post("/pos/confirm/",{"quote":token,"method":"cash","tendered":"100.00","total":"0.01","unit_price":"0.01"})
        self.assertEqual(response.status_code,302)
        sale=Sale.objects.using(self.alias_a).get()
        self.assertEqual(sale.total,Decimal("75.00"));self.assertEqual(sale.change,Decimal("25.00"))
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=product).quantity,7)
        self.assertEqual(SaleLine.objects.using(self.alias_a).get(sale=sale).unit_price,Decimal("25.00"))
        self.assertEqual(StockMovement.objects.using(self.alias_a).get(sale=sale).delta,-3)
        self.assertContainsText(self.tenant_client().get(response["Location"]),"Retail payment receipt")

    def test_pos_cash_shortfall_and_unverified_bank_transfer_cannot_record_sale(self):
        product,token,data=self.retail_quote(quantity=2)
        with gym_database(self.alias_a):
            with self.assertRaises(PosError):commit_sale(self.alias_a,self.a,self.user.pk,token,"cash",Decimal("49.99"))
            with self.assertRaises(PosError):commit_sale(self.alias_a,self.a,self.user.pk,token,"bank",reference="BANK1",note="Not confirmed",confirmed=False)
        self.assertFalse(Sale.objects.using(self.alias_a).exists())
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=product).quantity,10)
        self.assertEqual(self.tenant_client().post("/pos/confirm/",{"quote":token,"method":"bank","reference":"BANK1","note":"Receipt"}).status_code,200)
        self.assertFalse(Sale.objects.using(self.alias_a).exists())

    def test_pos_same_checkout_is_idempotent_even_after_quote_expiry(self):
        product,sale,token=self.retail_sale(quantity=2)
        with gym_database(self.alias_a),patch("django.core.signing.time.time",return_value=time.time()+700):
            repeated=commit_sale(self.alias_a,self.a,self.user.pk,token,"cash",Decimal("1000.00"))
            self.assertEqual(repeated.pk,sale.pk)
            with self.assertRaises(PosError):commit_sale(self.alias_a,self.a,self.user.pk,token,"cash",Decimal("500.00"))
        self.assertEqual(Sale.objects.using(self.alias_a).count(),1)
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=product).quantity,8)

    def test_pos_expired_new_quote_and_tampered_or_foreign_quotes_are_rejected(self):
        product,token,data=self.retail_quote()
        with gym_database(self.alias_a),patch("django.core.signing.time.time",return_value=time.time()+700):
            with self.assertRaises(PosError):commit_sale(self.alias_a,self.a,self.user.pk,token,"cash",100)
        with gym_database(self.alias_a):
            with self.assertRaises(PosError):commit_sale(self.alias_a,self.a,self.user.pk,token[:-5]+"bogus","cash",100)
            with self.assertRaises(PosError):commit_sale(self.alias_a,self.b,self.user.pk,token,"cash",100)
            with self.assertRaises(PosError):commit_sale(self.alias_a,self.a,self.user.pk+100000,token,"cash",100)
        self.assertFalse(Sale.objects.using(self.alias_a).exists())

    def test_pos_price_changes_and_hidden_products_invalidate_unpaid_quote(self):
        product,token,data=self.retail_quote()
        Product.objects.using(self.alias_a).filter(pk=product.pk).update(price="30.00")
        with gym_database(self.alias_a):
            with self.assertRaises(PosError):commit_sale(self.alias_a,self.a,self.user.pk,token,"cash",100)
        Product.objects.using(self.alias_a).filter(pk=product.pk).update(price="25.00",is_active=False)
        with gym_database(self.alias_a):
            with self.assertRaises(PosError):commit_sale(self.alias_a,self.a,self.user.pk,token,"cash",100)
        self.assertFalse(Sale.objects.using(self.alias_a).exists())
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=product).quantity,10)

    def test_pos_multi_item_stock_failure_rolls_back_entire_sale(self):
        first=self.retail_product();second=self.retail_product(name="Protein bar",price=Decimal("40.00"))
        self.retail_stock(first,5);self.retail_stock(second,2)
        with gym_database(self.alias_a):
            token,data=quote_cart(self.a,self.user.pk,self.branch_a.pk,[(first.pk,2),(second.pk,2)],self.a.name)
            adjust_stock(self.alias_a,second.pk,self.branch_a.pk,-1,"One damaged item",uuid4(),self.user.pk)
            with self.assertRaises(PosError):commit_sale(self.alias_a,self.a,self.user.pk,token,"cash",1000)
        self.assertFalse(Sale.objects.using(self.alias_a).exists());self.assertFalse(SaleLine.objects.using(self.alias_a).exists())
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=first).quantity,5)
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=second).quantity,1)
        self.assertFalse(StockMovement.objects.using(self.alias_a).filter(kind="sale").exists())

    def test_pos_concurrent_sales_cannot_oversell_the_last_unit(self):
        product=self.retail_product();self.retail_stock(product,1)
        with gym_database(self.alias_a):
            tokens=[quote_cart(self.a,self.user.pk,self.branch_a.pk,[(product.pk,1)],self.a.name)[0] for _ in range(2)]
        def sell(token):
            try:
                with gym_database(self.alias_a):
                    try:return str(commit_sale(self.alias_a,self.a,self.user.pk,token,"cash",100).pk)
                    except PosError:return "out-of-stock"
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(sell,tokens))
        self.assertEqual(results.count("out-of-stock"),1)
        self.assertEqual(Sale.objects.using(self.alias_a).count(),1)
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=product).quantity,0)

    def test_pos_concurrent_retries_record_one_sale_and_one_stock_deduction(self):
        product,token,data=self.retail_quote()
        def sell(_):
            try:
                with gym_database(self.alias_a):return commit_sale(self.alias_a,self.a,self.user.pk,token,"cash",100).pk
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(sell,range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(Sale.objects.using(self.alias_a).count(),1)
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=product).quantity,9)
        self.assertEqual(StockMovement.objects.using(self.alias_a).filter(kind="sale").count(),1)

    def test_pos_duplicate_cart_products_merge_and_receipt_snapshots_survive_edits(self):
        product=self.retail_product();self.retail_stock(product,10)
        original_name=product.name;original_sku=product.sku
        with gym_database(self.alias_a):
            token,data=quote_cart(self.a,self.user.pk,self.branch_a.pk,[(product.pk,1),(product.pk,2)],"Original gym brand")
            sale=commit_sale(self.alias_a,self.a,self.user.pk,token,"cash",100)
        Product.objects.using(self.alias_a).filter(pk=product.pk).update(name="Renamed",sku="CHANGED",price="99.00",is_active=False)
        Location.objects.using(self.alias_a).filter(pk=self.branch_a.pk).update(name="Renamed branch")
        line=SaleLine.objects.using(self.alias_a).get(sale=sale)
        self.assertEqual(line.quantity,3);self.assertEqual(line.name,original_name);self.assertEqual(line.sku,original_sku)
        self.assertEqual(sale.gym_name,"Original gym brand");self.assertEqual(sale.branch_name,"Main")
        self.assertEqual(line.unit_price,Decimal("25.00"))

    def test_pos_bank_reference_is_verified_and_cannot_be_reused_case_insensitively(self):
        product,token,data=self.retail_quote()
        reference="Bank-"+uuid4().hex
        with gym_database(self.alias_a):
            sale=commit_sale(self.alias_a,self.a,self.user.pk,token,"bank",reference=reference,note="Verified receipt",confirmed=True)
            second_token,data=quote_cart(self.a,self.user.pk,self.branch_a.pk,[(product.pk,1)],self.a.name)
            with self.assertRaises(PosError):commit_sale(self.alias_a,self.a,self.user.pk,second_token,"bank",reference=reference.upper(),note="Same receipt",confirmed=True)
        self.assertEqual(sale.total,Decimal("25.00"));self.assertEqual(sale.change,0)
        self.assertEqual(Sale.objects.using(self.alias_a).count(),1)
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=product).quantity,9)

    def test_pos_staff_can_sell_but_only_see_their_own_receipts(self):
        account=self.desk_account();other=self.desk_account()
        product,sale,token=self.retail_sale(actor=account)
        client=Client(HTTP_HOST=self.domain_a.hostname);client.force_login(account)
        self.assertEqual(client.get("/pos/").status_code,200)
        self.assertContainsText(client.get("/pos/sales/"),sale.number)
        self.assertEqual(client.get(f"/pos/sales/{sale.pk}/").status_code,200)
        stranger=Client(HTTP_HOST=self.domain_a.hostname);stranger.force_login(other)
        self.assertEqual(stranger.get(f"/pos/sales/{sale.pk}/").status_code,404)
        self.assertNotIn(sale.number.encode(),stranger.get("/pos/sales/").content)
        self.assertEqual(self.tenant_client().get(f"/pos/sales/{sale.pk}/").status_code,200)
        Membership.objects.filter(organization=self.a,user=account).update(is_active=False)
        self.assertEqual(client.get("/pos/").status_code,404)
        self.assertEqual(client.get(f"/pos/sales/{sale.pk}/").status_code,404)

    def test_pos_owner_only_catalog_stock_refund_and_reports(self):
        product,sale,token=self.retail_sale()
        for role in ("staff","manager"):
            account=self.desk_account(role);client=Client(HTTP_HOST=self.domain_a.hostname);client.force_login(account)
            for path in ("/manage/products/","/manage/products/new/","/manage/inventory/","/manage/inventory/adjust/",f"/manage/sales/{sale.pk}/refund/","/manage/sales/report/"):
                self.assertEqual(client.get(path).status_code,404)
        account=self.desk_account("member");client=Client(HTTP_HOST=self.domain_a.hostname);client.force_login(account)
        self.assertEqual(client.get("/pos/").status_code,404)
        self.assertEqual(client.get(f"/pos/sales/{sale.pk}/").status_code,404)
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).get("/pos/").status_code,302)

    def test_pos_mutations_require_csrf_and_confirmation_is_post_only(self):
        product,token,data=self.retail_quote()
        client=self.tenant_client(csrf=True)
        self.assertEqual(client.post("/pos/confirm/",{"quote":token,"method":"cash","tendered":"100"}).status_code,403)
        self.assertEqual(client.post("/manage/inventory/adjust/",{}).status_code,403)
        self.assertEqual(client.post("/manage/products/new/",{}).status_code,403)
        self.assertEqual(self.tenant_client().get("/pos/confirm/").status_code,405)
        self.assertFalse(Sale.objects.using(self.alias_a).exists())

    def test_pos_full_refund_restocks_once_and_preserves_original_sale(self):
        product,sale,token=self.retail_sale(quantity=2)
        reference="REFUND-"+uuid4().hex
        with gym_database(self.alias_a):
            first=refund_sale(self.alias_a,sale.pk,self.user.pk,reference,"Customer return verified",True)
            self.assertEqual(refund_sale(self.alias_a,sale.pk,self.user.pk,reference,"Customer return verified",True).pk,first.pk)
            with self.assertRaises(PosError):refund_sale(self.alias_a,sale.pk,self.user.pk,reference,"Customer return verified",False)
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=product).quantity,10)
        self.assertEqual(SaleRefund.objects.using(self.alias_a).filter(sale=sale).count(),1)
        self.assertEqual(StockMovement.objects.using(self.alias_a).filter(sale=sale,kind="refund").count(),1)
        sale.refresh_from_db(using=self.alias_a);self.assertEqual(sale.total,Decimal("50.00"))
        self.assertContainsText(self.tenant_client().get(f"/pos/sales/{sale.pk}/"),"Full refund recorded")

    def test_pos_refund_without_restock_does_not_change_balance_or_hidden_product(self):
        product,sale,token=self.retail_sale()
        Product.objects.using(self.alias_a).filter(pk=product.pk).update(is_active=False)
        Location.objects.using(self.alias_a).filter(pk=self.branch_a.pk).update(is_active=False)
        data={"reference":"REF-"+uuid4().hex,"note":"Damaged item; refund paid","confirmed":"on","amount":"0.01"}
        self.assertEqual(self.tenant_client().post(f"/manage/sales/{sale.pk}/refund/",data).status_code,302)
        refund=SaleRefund.objects.using(self.alias_a).get(sale=sale)
        self.assertEqual(refund.amount,Decimal("25.00"));self.assertFalse(refund.restocked)
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=product).quantity,9)
        self.assertFalse(StockMovement.objects.using(self.alias_a).filter(sale=sale,kind="refund").exists())

    def test_pos_refund_confirmation_and_unique_reference_protect_stock(self):
        product,sale,token=self.retail_sale()
        product,second,token=self.retail_sale(product=product)
        reference="REF-"+uuid4().hex
        client=self.tenant_client()
        self.assertEqual(client.post(f"/manage/sales/{sale.pk}/refund/",{"reference":reference,"note":"No confirmation","restock":"on"}).status_code,200)
        self.assertFalse(SaleRefund.objects.using(self.alias_a).exists())
        with gym_database(self.alias_a):
            refund_sale(self.alias_a,sale.pk,self.user.pk,reference,"Refund paid",True)
            with self.assertRaises(PosError):refund_sale(self.alias_a,second.pk,self.user.pk,reference.upper(),"Duplicate reference",True)
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=product).quantity,9)
        self.assertEqual(SaleRefund.objects.using(self.alias_a).count(),1)

    def test_pos_inventory_is_branch_specific_and_low_stock_includes_missing_balances(self):
        product=self.retail_product();self.retail_stock(product,10)
        empty=self.retail_product(name="Empty product")
        second=Location.objects.using(self.alias_a).create(name="Second retail branch")
        self.retail_stock(product,2,second)
        response=self.tenant_client().get(f"/manage/inventory/?location={second.pk}&low_only=on")
        self.assertContainsText(response,"2 units available")
        self.assertContainsText(response,"Empty product")
        self.assertContainsText(response,"0 units available")
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=product,location=self.branch_a).quantity,10)
        with gym_database(self.alias_a):
            with self.assertRaises(PosError):quote_cart(self.a,self.user.pk,second.pk,[(product.pk,3)],self.a.name)

    def test_pos_retail_reports_exclude_other_billing_and_use_refund_recording_date(self):
        product,sale,token=self.retail_sale(quantity=3)
        with gym_database(self.alias_a):refund_sale(self.alias_a,sale.pk,self.user.pk,"REF-"+uuid4().hex,"Returned and refunded",True)
        response=self.tenant_client().get("/manage/sales/report/")
        self.assertContainsText(response,"₱75.00");self.assertContainsText(response,"₱0.00")
        yesterday=timezone.now()-timedelta(days=1)
        Sale.objects.using(self.alias_a).filter(pk=sale.pk).update(created_at=yesterday)
        response=self.tenant_client().get("/manage/sales/report/")
        self.assertContainsText(response,"₱-75.00")
        response=self.tenant_client().get("/manage/sales/report/?date=invalid")
        self.assertEqual(response.status_code,200);self.assertContainsText(response,"Enter a valid date")

    def test_pos_other_gym_product_and_sale_ids_do_not_cross_databases(self):
        product,sale,token=self.retail_sale()
        Membership.objects.create(organization=self.b,user=self.user,role="owner")
        other=self.tenant_client(self.domain_b)
        self.assertEqual(other.get(f"/pos/sales/{sale.pk}/").status_code,404)
        self.assertEqual(other.get(f"/manage/products/{product.pk}/").status_code,404)
        self.assertEqual(other.post("/pos/confirm/",{"quote":token,"method":"cash","tendered":"100"}).status_code,302)
        self.assertFalse(Sale.objects.using(self.alias_b).exists());self.assertFalse(Product.objects.using(self.alias_b).exists())
        for table in ("pos_product","pos_stockbalance","pos_stockmovement","pos_sale","pos_saleline","pos_salerefund"):
            self.assertNotIn(table,connection.introspection.table_names())
            self.assertIn(table,connections[self.alias_a].introspection.table_names())
            self.assertIn(table,connections[self.alias_b].introspection.table_names())

    def test_pos_saas_entitlements_gate_new_operations_but_keep_old_receipts_readable(self):
        product,sale,token=self.retail_sale()
        subscription,invoice=self.saas_assignment(plan=self.saas_plan(pos=False),trial=14)
        client=self.tenant_client()
        self.assertEqual(client.get("/pos/").status_code,503)
        self.assertEqual(client.post("/pos/confirm/",{"quote":token,"method":"cash","tendered":"1000"}).status_code,503)
        self.assertEqual(client.get("/manage/inventory/").status_code,503)
        self.assertEqual(client.get(f"/pos/sales/{sale.pk}/").status_code,200)
        change_saas_state(subscription.pk,"suspend",self.user.pk,"Service paused")
        self.assertEqual(client.get(f"/pos/sales/{sale.pk}/").status_code,200)
        self.assertEqual(client.get("/pos/").status_code,503)

    def test_pos_only_plan_can_manage_shared_branches_staff_and_redirect_cashiers(self):
        subscription,invoice=self.saas_assignment(plan=self.saas_plan(pos=True,attendance=False,memberships=False),trial=14)
        self.assertEqual(self.tenant_client().get("/manage/branches/").status_code,200)
        self.assertEqual(self.tenant_client().get("/manage/staff/").status_code,200)
        self.assertEqual(self.tenant_client().get("/pos/").status_code,200)
        self.assertEqual(self.tenant_client().get("/desk/").status_code,503)
        account=self.desk_account();client=Client(HTTP_HOST=self.domain_a.hostname)
        self.assertEqual(client.post("/accounts/login/",{"username":account.username,"password":"Desk-Strong-84!"})["Location"],"/pos/")

    def test_pos_ui_review_supports_multiple_rows_and_rejects_oversized_formsets(self):
        product=self.retail_product();self.retail_stock(product,10)
        data={"location":str(self.branch_a.pk),"cart-TOTAL_FORMS":"2","cart-INITIAL_FORMS":"0",
            "cart-MIN_NUM_FORMS":"0","cart-MAX_NUM_FORMS":"20","cart-0-product":str(product.pk),"cart-0-quantity":"2"}
        response=self.tenant_client().post("/pos/",data)
        self.assertContainsText(response,"Total: ₱50.00 PHP")
        self.assertIn(b'name="quote"',response.content)
        self.assertFalse(Sale.objects.using(self.alias_a).exists())
        data["cart-TOTAL_FORMS"]="1000"
        response=self.tenant_client().post("/pos/",data)
        self.assertContainsText(response,"Please submit at most 20 forms")
        self.assertFalse(Sale.objects.using(self.alias_a).exists())

    def test_pos_disabled_branch_cannot_complete_an_existing_quote(self):
        product,token,data=self.retail_quote()
        Location.objects.using(self.alias_a).filter(pk=self.branch_a.pk).update(is_active=False)
        with gym_database(self.alias_a):
            with self.assertRaises(PosError):commit_sale(self.alias_a,self.a,self.user.pk,token,"cash",100)
        self.assertFalse(Sale.objects.using(self.alias_a).exists())
        self.assertEqual(StockBalance.objects.using(self.alias_a).get(product=product).quantity,10)

    def test_pos_upgrade_button_installs_module_without_touching_members_or_branding(self):
        from django.db.migrations.recorder import MigrationRecorder
        member,term,code=self.active_card()
        WebsiteSettings.objects.using(self.alias_a).filter(pk=1).update(hero_title="Keep brand intact")
        with connections[self.alias_a].schema_editor() as editor:
            for model in (StockMovement,SaleRefund,SaleLine,StockBalance,Sale,Product):editor.delete_model(model)
        MigrationRecorder.Migration.objects.using(self.alias_a).filter(app="pos").delete()
        self.make_platform_admin()
        self.assertContainsText(self.client.get(f"/gyms/{self.a.slug}/"),"Retry setup")
        self.assertEqual(self.client.post(f"/gyms/{self.a.slug}/retry-setup/").status_code,302)
        self.assertEqual(Product.objects.using(self.alias_a).count(),0)
        self.assertTrue(MemberProfile.objects.using(self.alias_a).filter(pk=member.pk).exists())
        self.assertEqual(WebsiteSettings.objects.using(self.alias_a).get(pk=1).hero_title,"Keep brand intact")


    def class_fixture(self, capacity=1, hour=0, trainer=None):
        user = self.desk_account('member')
        with gym_database(self.alias_a):
            member = MemberProfile.objects.create(user_id=user.pk, full_name='Booking member',
                email=uuid4().hex+'@example.com', phone='09123456789')
            plan = MembershipPlan.objects.create(name='Class access', price=100)
            today = timezone.localdate()
            MemberSubscription.objects.create(member=member, plan=plan, plan_name=plan.name, price=100,
                period='month', status='active', starts_on=today, expires_on=today+timedelta(days=30))
            trainer = trainer or Trainer.objects.create(name='Trainer Alex')
            start = timezone.now()+timedelta(days=1, hours=hour)
            session = create_class(self.alias_a, dict(title='Strength fundamentals', description='Small group session',
                trainer=trainer, location=self.branch_a, starts_at=start, ends_at=start+timedelta(hours=1),
                capacity=capacity), self.user.pk)
        return user, member, session

    def another_class_member(self):
        user = self.desk_account('member')
        with gym_database(self.alias_a):
            first = MemberProfile.objects.first()
            term = first.subscriptions.first()
            member = MemberProfile.objects.create(user_id=user.pk, full_name='Second member',
                email=uuid4().hex+'@example.com', phone='09120000000')
            MemberSubscription.objects.create(member=member, plan=term.plan, plan_name=term.plan_name,
                price=100, period='month', status='active', starts_on=term.starts_on, expires_on=term.expires_on)
        return user, member

    def test_classes_booking_waitlist_and_cancellation_promotes_fifo(self):
        user, member, session = self.class_fixture()
        user2, member2 = self.another_class_member()
        one = book_class(self.alias_a, session.pk, member.pk, user.pk)
        two = book_class(self.alias_a, session.pk, member2.pk, user2.pk)
        self.assertEqual(one.status, 'confirmed')
        self.assertEqual(two.status, 'waitlisted')
        cancel_class_booking(self.alias_a, one.pk, user.pk)
        two.refresh_from_db(using=self.alias_a)
        self.assertEqual(two.status, 'confirmed')
        self.assertTrue(BookingEvent.objects.using(self.alias_a).filter(booking=two, action='promoted', actor_id=None).exists())

    def test_classes_duplicate_booking_and_cancel_are_idempotent(self):
        user, member, session = self.class_fixture()
        one = book_class(self.alias_a, session.pk, member.pk, user.pk)
        self.assertEqual(book_class(self.alias_a, session.pk, member.pk, user.pk).pk, one.pk)
        cancel_class_booking(self.alias_a, one.pk, user.pk)
        cancel_class_booking(self.alias_a, one.pk, user.pk)
        self.assertEqual(BookingEvent.objects.using(self.alias_a).count(), 2)

    def test_classes_membership_must_cover_session_and_be_active(self):
        user, member, session = self.class_fixture()
        for status in ['pending', 'frozen', 'cancelled']:
            MemberSubscription.objects.using(self.alias_a).filter(member=member).update(status=status)
            with self.assertRaises(BookingError):book_class(self.alias_a, session.pk, member.pk, user.pk)
        MemberSubscription.objects.using(self.alias_a).filter(member=member).update(status='active', starts_on=timezone.localdate()-timedelta(days=1), expires_on=timezone.localdate())
        with self.assertRaises(BookingError):book_class(self.alias_a, session.pk, member.pk, user.pk)

    def test_classes_member_overlap_across_trainers_is_blocked(self):
        user, member, session = self.class_fixture()
        _, _, other = self.class_fixture()
        book_class(self.alias_a, session.pk, member.pk, user.pk)
        with self.assertRaises(BookingError):book_class(self.alias_a, other.pk, member.pk, user.pk)

    def test_classes_trainer_overlap_and_invalid_schedule_rejected(self):
        _, _, session = self.class_fixture()
        with gym_database(self.alias_a):
            data = dict(title='Conflict', description='', trainer=session.trainer, location=self.branch_a,
                starts_at=session.starts_at, ends_at=session.ends_at, capacity=5)
            with self.assertRaises(BookingError):create_class(self.alias_a, data, self.user.pk)
            data['starts_at'] = session.ends_at
            data['ends_at'] = session.ends_at+timedelta(hours=1)
            create_class(self.alias_a, data, self.user.pk)
            data['capacity'] = 0
            with self.assertRaises(BookingError):create_class(self.alias_a, data, self.user.pk)

    def test_classes_session_cancel_preserves_history_and_cancels_waitlist(self):
        user, member, session = self.class_fixture()
        user2, member2 = self.another_class_member()
        book_class(self.alias_a, session.pk, member.pk, user.pk)
        book_class(self.alias_a, session.pk, member2.pk, user2.pk)
        cancel_class_session(self.alias_a, session.pk, 'Trainer unavailable', self.user.pk)
        self.assertEqual(ClassBooking.objects.using(self.alias_a).filter(status='cancelled').count(), 2)
        cancel_class_session(self.alias_a, session.pk, 'Duplicate retry', self.user.pk)
        self.assertEqual(BookingEvent.objects.using(self.alias_a).filter(action='session_cancelled').count(), 2)
        with self.assertRaises(BookingError):book_class(self.alias_a, session.pk, member.pk, user.pk)

    def test_classes_ineligible_waitlist_skipped_without_taking_seat(self):
        user, member, session = self.class_fixture()
        user2, member2 = self.another_class_member()
        user3, member3 = self.another_class_member()
        one = book_class(self.alias_a, session.pk, member.pk, user.pk)
        two = book_class(self.alias_a, session.pk, member2.pk, user2.pk)
        three = book_class(self.alias_a, session.pk, member3.pk, user3.pk)
        MemberProfile.objects.using(self.alias_a).filter(pk=member2.pk).update(is_active=False)
        cancel_class_booking(self.alias_a, one.pk, user.pk)
        two.refresh_from_db(using=self.alias_a); three.refresh_from_db(using=self.alias_a)
        self.assertEqual(two.status, 'waitlisted'); self.assertEqual(three.status, 'confirmed')

    def test_classes_concurrent_last_seat_never_overbooks(self):
        user, member, session = self.class_fixture()
        user2, member2 = self.another_class_member()
        def reserve(pair):
            try:return book_class(self.alias_a, session.pk, pair[1], pair[0]).status
            finally:connections[self.alias_a].close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, [(user.pk, member.pk), (user2.pk, member2.pk)]))
        self.assertCountEqual(results, ['confirmed', 'waitlisted'])

    def test_classes_member_cannot_cancel_another_members_booking(self):
        user, member, session = self.class_fixture()
        other, _ = self.another_class_member()
        booking = book_class(self.alias_a, session.pk, member.pk, user.pk)
        client = Client(HTTP_HOST=self.domain_a.hostname); client.force_login(other)
        self.assertEqual(client.post(f'/portal/bookings/{booking.pk}/cancel/').status_code, 404)
        booking.refresh_from_db(using=self.alias_a); self.assertEqual(booking.status, 'confirmed')

    def test_classes_owner_pages_and_staff_roster_permissions(self):
        _, _, session = self.class_fixture()
        for url in ['/manage/trainers/', '/manage/classes/new/', '/desk/classes/', f'/desk/classes/{session.pk}/']:
            self.assertEqual(self.tenant_client().get(url).status_code, 200)
        staff = self.desk_account()
        client = Client(HTTP_HOST=self.domain_a.hostname); client.force_login(staff)
        self.assertEqual(client.get(f'/desk/classes/{session.pk}/').status_code, 200)
        self.assertEqual(client.get('/manage/trainers/').status_code, 404)
        self.assertEqual(client.post(f'/manage/classes/{session.pk}/cancel/', {'reason':'No'}).status_code, 404)

    def test_classes_member_portal_book_and_cancel_and_csrf(self):
        user, member, session = self.class_fixture()
        client = Client(HTTP_HOST=self.domain_a.hostname); client.force_login(user)
        self.assertContainsText(client.get('/portal/classes/'), 'Strength fundamentals')
        self.assertEqual(client.get(f'/portal/classes/{session.pk}/book/').status_code, 405)
        self.assertEqual(client.post(f'/portal/classes/{session.pk}/book/').status_code, 302)
        booking = ClassBooking.objects.using(self.alias_a).get()
        self.assertContainsText(client.get('/portal/classes/'), 'Confirmed')
        self.assertEqual(client.post(f'/portal/bookings/{booking.pk}/cancel/').status_code, 302)
        secure = Client(HTTP_HOST=self.domain_a.hostname, enforce_csrf_checks=True); secure.force_login(user)
        self.assertEqual(secure.post(f'/portal/classes/{session.pk}/book/').status_code, 403)

    def test_classes_cross_gym_host_and_ids_fail_closed(self):
        user, member, session = self.class_fixture()
        Membership.objects.create(user=user, organization=self.b, role='member')
        MemberProfile.objects.using(self.alias_b).create(user_id=user.pk, full_name='Gym B member', email='b@example.com', phone='0')
        client = Client(HTTP_HOST=self.domain_b.hostname); client.force_login(user)
        self.assertNotIn(b'Strength fundamentals', client.get('/portal/classes/').content)
        self.assertEqual(client.post(f'/portal/classes/{session.pk}/book/').status_code, 404)
        self.assertEqual(self.tenant_client(self.domain_b).get('/desk/classes/').status_code, 404)
        self.assertFalse(ClassBooking.objects.using(self.alias_b).exists())

    def test_classes_disabled_trainer_branch_and_started_session_reject_booking(self):
        user, member, session = self.class_fixture()
        Trainer.objects.using(self.alias_a).filter(pk=session.trainer_id).update(is_active=False)
        with self.assertRaises(BookingError):book_class(self.alias_a, session.pk, member.pk, user.pk)
        Trainer.objects.using(self.alias_a).filter(pk=session.trainer_id).update(is_active=True)
        Location.objects.using(self.alias_a).filter(pk=session.location_id).update(is_active=False)
        with self.assertRaises(BookingError):book_class(self.alias_a, session.pk, member.pk, user.pk)
        Location.objects.using(self.alias_a).filter(pk=session.location_id).update(is_active=True)
        ClassSession.objects.using(self.alias_a).filter(pk=session.pk).update(starts_at=timezone.now()-timedelta(minutes=1))
        with self.assertRaises(BookingError):book_class(self.alias_a, session.pk, member.pk, user.pk)

    def test_classes_cancelled_rebooking_goes_to_back_of_queue(self):
        user, member, session = self.class_fixture()
        user2, member2 = self.another_class_member()
        one = book_class(self.alias_a, session.pk, member.pk, user.pk)
        two = book_class(self.alias_a, session.pk, member2.pk, user2.pk)
        cancel_class_booking(self.alias_a, two.pk, user2.pk)
        rebooked = book_class(self.alias_a, session.pk, member2.pk, user2.pk)
        self.assertEqual(rebooked.pk, two.pk)
        self.assertEqual(rebooked.status, 'waitlisted')
        self.assertGreater(rebooked.queued_at, two.queued_at)

    def test_classes_managed_membership_module_controls_access(self):
        user, member, session = self.class_fixture()
        plan = SaaSPlan.objects.create(name='No membership '+uuid4().hex, price=100, memberships=False)
        assign_saas_plan(self.a.pk, plan.pk, 14, 0, self.user.pk)
        client = Client(HTTP_HOST=self.domain_a.hostname); client.force_login(user)
        self.assertEqual(client.get('/portal/classes/').status_code, 503)
        self.assertEqual(self.tenant_client().get('/desk/classes/').status_code, 503)
        self.assertEqual(client.post(f'/portal/classes/{session.pk}/book/').status_code, 503)
        self.assertFalse(ClassBooking.objects.using(self.alias_a).exists())


    def test_classes_owner_can_publish_and_cancel_through_forms(self):
        client = self.tenant_client()
        self.assertEqual(client.post('/manage/trainers/', {'name':'Mia','specialty':'Yoga','bio':'Gentle practice','is_active':'on'}).status_code,302)
        trainer = Trainer.objects.using(self.alias_a).get()
        start = timezone.localtime(timezone.now()+timedelta(days=2)).replace(second=0,microsecond=0)
        data = {'title':'Morning yoga','description':'All levels','trainer':str(trainer.pk),'location':str(self.branch_a.pk),
            'starts_at':start.strftime('%Y-%m-%dT%H:%M'),'ends_at':(start+timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),'capacity':'12'}
        response = client.post('/manage/classes/new/',data)
        self.assertEqual(response.status_code,302)
        session = ClassSession.objects.using(self.alias_a).get()
        self.assertEqual(session.starts_at,start)
        self.assertEqual(session.created_by_id,self.user.pk)
        self.assertContainsText(client.get(response['Location']),'Morning yoga')
        self.assertEqual(client.post(f'/manage/classes/{session.pk}/cancel/',{'reason':'Holiday closure'}).status_code,302)
        session.refresh_from_db(using=self.alias_a)
        self.assertEqual(session.cancellation_reason,'Holiday closure')

    def test_classes_upgrade_button_installs_tables_and_preserves_members(self):
        from django.db.migrations.recorder import MigrationRecorder
        member,term,code = self.active_card()
        with connections[self.alias_a].schema_editor() as editor:
            for model in (RenewalRequest,MemberProfileChange,EmailAttempt,EmailNotification,NotificationSettings,BookingEvent,ClassBooking,ClassSession,Trainer):editor.delete_model(model)
            editor.remove_field(MemberProfile,MemberProfile._meta.get_field("email_notifications"))
        MigrationRecorder.Migration.objects.using(self.alias_a).filter(app='gym_core',name__in=['0005_trainer_classbooking_bookingevent_classsession_and_more',
            '0006_memberprofile_email_notifications_emailnotification_and_more',
            '0007_notificationsettings_membership_updates_and_more']).delete()
        self.make_platform_admin()
        self.assertContainsText(self.client.get(f'/gyms/{self.a.slug}/'),'Retry setup')
        self.assertEqual(self.client.post(f'/gyms/{self.a.slug}/retry-setup/').status_code,302)
        self.assertEqual(ClassSession.objects.using(self.alias_a).count(),0)
        self.assertTrue(MemberProfile.objects.using(self.alias_a).filter(pk=member.pk).exists())

    def test_classes_membership_only_plan_supports_branch_setup_and_staff_login(self):
        plan = self.saas_plan(memberships=True,attendance=False,pos=False)
        self.saas_assignment(plan=plan,trial=14)
        self.assertEqual(self.tenant_client().get('/manage/branches/').status_code,200)
        self.assertEqual(self.tenant_client().get('/desk/classes/').status_code,200)
        account = self.desk_account()
        client = Client(HTTP_HOST=self.domain_a.hostname)
        response = client.post('/accounts/login/',{'username':account.username,'password':'Desk-Strong-84!'})
        self.assertEqual(response['Location'],'/desk/classes/')


    def enable_notifications(self, alias=None, domain=None):
        return NotificationSettings.objects.using(alias or self.alias_a).create(pk=1,email_enabled=True,
            owner_email='owner@example.com',public_origin='https://'+(domain or self.domain_a).hostname)

    def mail_transport(self):
        from django.core import mail
        mail.outbox = []
        return override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
            NOTIFICATION_DELIVERY_ENABLED=True,EMAIL_HOST='smtp.test',DEFAULT_FROM_EMAIL='service@mailer.test')

    def queued_notice(self, key=None, member=None, **kwargs):
        return queue_notice(self.alias_a,key=key or 'fixture:'+uuid4().hex,category='bookings',
            title='Class update',message='Your class has been updated.',member=member,**kwargs)

    def test_notifications_disabled_by_default_and_no_smtp_in_request(self):
        user,member,session = self.class_fixture()
        with patch('django.core.mail.EmailMultiAlternatives.send') as send:
            book_class(self.alias_a,session.pk,member.pk,user.pk)
        self.assertFalse(EmailNotification.objects.using(self.alias_a).exists())
        send.assert_not_called()

    def test_notifications_booking_events_queue_in_same_transaction_and_deduplicate(self):
        user,member,session = self.class_fixture()
        self.enable_notifications()
        with patch('django.core.mail.EmailMultiAlternatives.send') as send:
            booking = book_class(self.alias_a,session.pk,member.pk,user.pk)
            book_class(self.alias_a,session.pk,member.pk,user.pk)
        send.assert_not_called()
        row = EmailNotification.objects.using(self.alias_a).get()
        self.assertEqual(row.recipient,member.email)
        self.assertEqual(row.expected_status,'confirmed')
        self.assertIn(self.domain_a.hostname,row.text_body)
        self.assertNotIn(self.domain_b.hostname,row.text_body)
        try:
            with gym_database(self.alias_a),transaction.atomic(using=self.alias_a):
                BookingEvent.objects.create(booking=booking,action='confirmed',actor_id=user.pk)
                raise ValueError('rollback')
        except ValueError:pass
        self.assertEqual(EmailNotification.objects.using(self.alias_a).count(),1)

    def test_notifications_waitlist_promotion_and_class_cancellation(self):
        user,member,session = self.class_fixture()
        user2,member2 = self.another_class_member()
        self.enable_notifications()
        one = book_class(self.alias_a,session.pk,member.pk,user.pk)
        book_class(self.alias_a,session.pk,member2.pk,user2.pk)
        cancel_class_booking(self.alias_a,one.pk,user.pk)
        cancel_class_session(self.alias_a,session.pk,'Trainer ill',self.user.pk)
        rows = EmailNotification.objects.using(self.alias_a)
        self.assertTrue(rows.filter(subject__contains='place is now confirmed',member=member2).exists())
        self.assertTrue(rows.filter(text_body__contains='Trainer ill',member=member2).exists())
        self.assertEqual(rows.count(),5)

    def test_notifications_successful_delivery_and_repeated_worker_does_not_resend(self):
        from django.core import mail
        self.enable_notifications()
        row = self.queued_notice()
        with self.mail_transport():
            self.assertEqual(deliver_email_batch(self.a,self.alias_a),1)
            self.assertEqual(deliver_email_batch(self.a,self.alias_a),0)
            self.assertEqual(len(mail.outbox),1)
            self.assertEqual(mail.outbox[0].to,['owner@example.com'])
            self.assertIn(str(row.pk),mail.outbox[0].extra_headers['Message-ID'])
            self.assertIn('text/html',str(mail.outbox[0].alternatives[0]))
        row.refresh_from_db(using=self.alias_a)
        self.assertEqual(row.status,'sent')
        self.assertEqual(row.attempts,1)
        self.assertEqual(EmailAttempt.objects.using(self.alias_a).get().outcome,'accepted_by_mail_server')

    def test_notifications_transport_off_preserves_queue_without_attempt(self):
        self.enable_notifications(); row = self.queued_notice()
        with override_settings(NOTIFICATION_DELIVERY_ENABLED=False):
            self.assertEqual(deliver_email_batch(self.a,self.alias_a),0)
        row.refresh_from_db(using=self.alias_a)
        self.assertEqual(row.status,'queued'); self.assertEqual(row.attempts,0)

    def test_notifications_transport_error_retries_with_safe_history(self):
        self.enable_notifications(); row = self.queued_notice()
        with self.mail_transport(),patch('django.core.mail.EmailMultiAlternatives.send',side_effect=RuntimeError('private-provider-password')):
            self.assertEqual(deliver_email_batch(self.a,self.alias_a),0)
        row.refresh_from_db(using=self.alias_a)
        self.assertEqual(row.status,'queued'); self.assertGreater(row.available_at,timezone.now())
        self.assertEqual(row.outcome,'transport_error')
        self.assertNotIn('private-provider-password',EmailAttempt.objects.using(self.alias_a).get().outcome)
        EmailNotification.objects.using(self.alias_a).filter(pk=row.pk).update(available_at=timezone.now())
        with self.mail_transport():self.assertEqual(deliver_email_batch(self.a,self.alias_a),1)
        self.assertEqual(EmailAttempt.objects.using(self.alias_a).count(),2)

    def test_notifications_permanent_rejection_and_owner_retry_preserves_attempts(self):
        import smtplib
        self.enable_notifications(); row = self.queued_notice()
        with self.mail_transport(),patch('django.core.mail.EmailMultiAlternatives.send',side_effect=smtplib.SMTPRecipientsRefused({'private@example.com':(550,b'no')})):
            deliver_email_batch(self.a,self.alias_a)
        row.refresh_from_db(using=self.alias_a); self.assertEqual(row.status,'failed')
        client = self.tenant_client()
        self.assertEqual(client.get(f'/manage/notifications/{row.pk}/retry/').status_code,405)
        self.assertEqual(client.post(f'/manage/notifications/{row.pk}/retry/').status_code,302)
        with self.mail_transport():self.assertEqual(deliver_email_batch(self.a,self.alias_a),1)
        self.assertEqual(EmailAttempt.objects.using(self.alias_a).count(),2)

    def test_notifications_opt_out_suppresses_already_queued_member_message(self):
        user,member,session = self.class_fixture(); self.enable_notifications()
        row = self.queued_notice(member=member)
        client = Client(HTTP_HOST=self.domain_a.hostname); client.force_login(user)
        from gym_core.notifications.forms import MemberEmailForm
        original_validation = MemberEmailForm.is_valid
        replacement_qr = uuid4()
        def concurrent_owner_update(form):
            valid = original_validation(form)
            MemberProfile.objects.using(self.alias_a).filter(pk=member.pk).update(is_active=False,qr_version=replacement_qr)
            return valid
        with patch('gym_core.notifications.forms.MemberEmailForm.is_valid',autospec=True,side_effect=concurrent_owner_update):
            self.assertEqual(client.post('/portal/notifications/',{}).status_code,302)
        member.refresh_from_db(using=self.alias_a)
        self.assertFalse(member.is_active)
        self.assertEqual(member.qr_version,replacement_qr)
        self.assertFalse(member.email_notifications)
        with self.mail_transport(),patch('django.core.mail.EmailMultiAlternatives.send') as send:
            deliver_email_batch(self.a,self.alias_a)
        send.assert_not_called(); row.refresh_from_db(using=self.alias_a)
        self.assertEqual(row.status,'skipped')
        self.assertIn('opted_out',row.outcome)

    def test_notifications_disabled_category_or_changed_recipient_suppresses_queue(self):
        self.enable_notifications(); row = self.queued_notice()
        NotificationSettings.objects.using(self.alias_a).filter(pk=1).update(bookings=False)
        with self.mail_transport():deliver_email_batch(self.a,self.alias_a)
        row.refresh_from_db(using=self.alias_a); self.assertEqual(row.outcome,'gym_or_category_disabled')
        NotificationSettings.objects.using(self.alias_a).filter(pk=1).update(bookings=True)
        row2 = self.queued_notice()
        NotificationSettings.objects.using(self.alias_a).filter(pk=1).update(owner_email='new@example.com')
        with self.mail_transport():deliver_email_batch(self.a,self.alias_a)
        row2.refresh_from_db(using=self.alias_a); self.assertEqual(row2.outcome,'owner_inbox_changed')

    def test_notifications_stale_booking_confirmation_not_sent_after_cancel(self):
        user,member,session = self.class_fixture(); self.enable_notifications()
        booking = book_class(self.alias_a,session.pk,member.pk,user.pk)
        cancel_class_booking(self.alias_a,booking.pk,user.pk)
        with self.mail_transport():self.assertEqual(deliver_email_batch(self.a,self.alias_a),1)
        self.assertEqual(EmailNotification.objects.using(self.alias_a).filter(status='skipped',outcome='booking_changed').count(),1)

    def test_notifications_renewal_and_class_reminders_deduplicate_and_recheck(self):
        user,member,session = self.class_fixture(); self.enable_notifications()
        MemberSubscription.objects.using(self.alias_a).filter(member=member).update(expires_on=timezone.localdate()+timedelta(days=7))
        ClassSession.objects.using(self.alias_a).filter(pk=session.pk).update(starts_at=timezone.now()+timedelta(hours=12))
        book_class(self.alias_a,session.pk,member.pk,user.pk)
        queue_email_reminders(self.alias_a); queue_email_reminders(self.alias_a)
        self.assertEqual(EmailNotification.objects.using(self.alias_a).filter(category='membership_reminders').count(),1)
        self.assertEqual(EmailNotification.objects.using(self.alias_a).filter(category='class_reminders').count(),1)
        MemberSubscription.objects.using(self.alias_a).filter(member=member).update(expires_on=timezone.localdate()+timedelta(days=30))
        with self.mail_transport():deliver_email_batch(self.a,self.alias_a)
        reminder = EmailNotification.objects.using(self.alias_a).get(category='membership_reminders')
        self.assertEqual(reminder.outcome,'membership_changed')

    def test_notifications_active_renewal_suppresses_expiry_reminder(self):
        user,member,session = self.class_fixture(); self.enable_notifications()
        term = MemberSubscription.objects.using(self.alias_a).get(member=member)
        expiry = timezone.localdate()+timedelta(days=7)
        MemberSubscription.objects.using(self.alias_a).filter(pk=term.pk).update(expires_on=expiry)
        queue_email_reminders(self.alias_a)
        MemberSubscription.objects.using(self.alias_a).create(member_id=member.pk,plan_id=term.plan_id,plan_name=term.plan_name,
            price=100,period='month',status='active',starts_on=expiry,expires_on=expiry+timedelta(days=30))
        with self.mail_transport():self.assertEqual(deliver_email_batch(self.a,self.alias_a),0)
        self.assertEqual(EmailNotification.objects.using(self.alias_a).get().outcome,'membership_already_renewed')

    def test_notifications_expired_message_and_domain_removal_fail_closed(self):
        self.enable_notifications()
        row = self.queued_notice(expires_at=timezone.now()-timedelta(seconds=1))
        row2 = self.queued_notice()
        GymDomain.objects.filter(pk=self.domain_a.pk).update(is_active=False)
        with self.mail_transport(),patch('django.core.mail.EmailMultiAlternatives.send') as send:
            deliver_email_batch(self.a,self.alias_a)
        send.assert_not_called(); row.refresh_from_db(using=self.alias_a); row2.refresh_from_db(using=self.alias_a)
        self.assertEqual(row.outcome,'notification_expired'); self.assertEqual(row2.outcome,'gym_domain_unverified')

    def test_notifications_concurrent_workers_claim_only_once(self):
        self.enable_notifications(); self.queued_notice()
        def deliver(_):
            try:return deliver_email_batch(self.a,self.alias_a)
            finally:connections.close_all()
        with self.mail_transport(),patch('django.core.mail.EmailMultiAlternatives.send',return_value=1) as send:
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(deliver,range(2)))
        self.assertEqual(sum(results),1); self.assertEqual(send.call_count,1)

    def test_notifications_stale_lease_recovery_records_attempt_and_delivers(self):
        self.enable_notifications(); row = self.queued_notice()
        now = timezone.now()
        EmailNotification.objects.using(self.alias_a).filter(pk=row.pk).update(status='sending',attempts=1,claim_token=uuid4(),lease_until=now-timedelta(seconds=1))
        EmailAttempt.objects.using(self.alias_a).create(notification_id=row.pk,number=1,outcome='sending',started_at=now-timedelta(minutes=10))
        with self.mail_transport():self.assertEqual(deliver_email_batch(self.a,self.alias_a),1)
        self.assertTrue(EmailAttempt.objects.using(self.alias_a).filter(number=1,outcome='worker_lease_expired',finished_at__isnull=False).exists())

    def test_notifications_owner_permissions_and_history_isolation_and_csrf(self):
        self.enable_notifications(); row = self.queued_notice()
        client = self.tenant_client()
        self.assertContainsText(client.get('/manage/notifications/'),'Class update')
        self.assertContainsText(client.get(f'/manage/notifications/{row.pk}/'),'Your class has been updated')
        self.assertEqual(self.tenant_client(csrf=True).post('/manage/notifications/test/').status_code,403)
        staff = self.desk_account(); other = Client(HTTP_HOST=self.domain_a.hostname); other.force_login(staff)
        self.assertEqual(other.get('/manage/notifications/').status_code,404)
        self.assertEqual(self.tenant_client(self.domain_b).get(f'/manage/notifications/{row.pk}/').status_code,404)
        self.assertFalse(EmailNotification.objects.using(self.alias_b).exists())

    def test_notifications_settings_reject_foreign_domain_and_test_queues_only(self):
        client = self.tenant_client()
        data = {'email_enabled':'on','public_origin':'https://'+self.domain_b.hostname,'owner_email':'owner@example.com','owner_alerts':'on'}
        self.assertContainsText(client.post('/manage/notifications/',data),'active verified domain belonging to this gym')
        data['public_origin'] = 'https://'+self.domain_a.hostname
        self.assertEqual(client.post('/manage/notifications/',data).status_code,302)
        with patch('django.core.mail.EmailMultiAlternatives.send') as send:
            self.assertEqual(client.post('/manage/notifications/test/').status_code,302)
        send.assert_not_called(); self.assertEqual(EmailNotification.objects.using(self.alias_a).count(),1)

    def test_notifications_payment_production_only_deduplicates_and_alerts_review(self):
        user,member,session = self.class_fixture(); self.enable_notifications()
        term = MemberSubscription.objects.using(self.alias_a).get(member=member)
        order = PaymentOrder.objects.using(self.alias_a).create(subscription_id=term.pk,merchant_reference='N'+uuid4().hex,
            merchant_code='test',environment='sandbox',payment_method='TEST',amount=100,status='paid')
        self.assertFalse(EmailNotification.objects.using(self.alias_a).exists())
        order.environment='production'; order.access_note='Owner review required: term not eligible.'; order.save(using=self.alias_a)
        order.save(using=self.alias_a)
        self.assertEqual(EmailNotification.objects.using(self.alias_a).filter(category='payment_confirmations').count(),1)
        self.assertEqual(EmailNotification.objects.using(self.alias_a).filter(category='owner_alerts').count(),1)

    def test_notifications_new_application_alert_branded_and_html_escaped(self):
        self.enable_notifications()
        WebsiteSettings.objects.using(self.alias_a).filter(pk=1).update(display_name='Iron & Fit')
        with gym_database(self.alias_a):
            plan = MembershipPlan.objects.create(name='Unlimited',price=100)
            application = MembershipApplication.objects.create(plan=plan,full_name='<script>alert(1)</script>',email='app@example.com',phone='0',
                quoted_plan_name=plan.name,quoted_price=100,quoted_period='month')
        row = EmailNotification.objects.using(self.alias_a).get()
        self.assertEqual(row.recipient,'owner@example.com')
        self.assertIn('Iron & Fit',row.subject)
        self.assertNotIn('<script>',row.html_body)
        self.assertIn('&lt;script&gt;',row.html_body)

    def test_notifications_suspended_gym_suppresses_but_preferences_remain_available(self):
        user,member,session = self.class_fixture(); self.enable_notifications(); row=self.queued_notice(member=member)
        subscription,invoice = self.saas_assignment(trial=14)
        change_saas_state(subscription.pk,'suspend',self.user.pk,'Support pause')
        client = Client(HTTP_HOST=self.domain_a.hostname); client.force_login(user)
        self.assertEqual(client.get('/portal/notifications/').status_code,200)
        self.assertEqual(self.tenant_client().get('/manage/notifications/').status_code,200)
        with self.mail_transport():self.assertEqual(deliver_email_batch(self.a,self.alias_a),0)
        row.refresh_from_db(using=self.alias_a); self.assertEqual(row.outcome,'subscription_or_module_unavailable')

    def test_notifications_scheduler_uses_registered_ids_and_gym_worker_scopes(self):
        self.enable_notifications()
        from gym_core.notifications.tasks import sweep_notifications,process_gym_notifications
        with patch('gym_core.notifications.tasks.process_gym_notifications.delay') as dispatch:
            sweep_notifications.run()
        keys=[call.args[0] for call in dispatch.call_args_list]
        self.assertIn(str(self.a.database_key),keys)
        with patch('gym_core.notifications.tasks.queue_reminders') as reminders,patch('gym_core.notifications.tasks.deliver_batch') as deliver:
            process_gym_notifications.run(str(self.a.database_key))
        reminders.assert_called_once_with(self.alias_a)
        self.assertEqual(deliver.call_args.args[:2],(self.a,self.alias_a))
        self.assertIsNotNone(NotificationSettings.objects.using(self.alias_a).get().last_worker_check)

    def test_notifications_upgrade_button_preserves_classes_and_members(self):
        from django.db.migrations.recorder import MigrationRecorder
        user,member,session = self.class_fixture()
        with connections[self.alias_a].schema_editor() as editor:
            for model in (RenewalRequest,MemberProfileChange,EmailAttempt,EmailNotification,NotificationSettings):editor.delete_model(model)
            editor.remove_field(MemberProfile,MemberProfile._meta.get_field('email_notifications'))
        MigrationRecorder.Migration.objects.using(self.alias_a).filter(app='gym_core',name__in=['0006_memberprofile_email_notifications_emailnotification_and_more',
            '0007_notificationsettings_membership_updates_and_more']).delete()
        self.make_platform_admin()
        self.assertContainsText(self.client.get(f'/gyms/{self.a.slug}/'),'Retry setup')
        self.assertEqual(self.client.post(f'/gyms/{self.a.slug}/retry-setup/').status_code,302)
        self.assertTrue(ClassSession.objects.using(self.alias_a).filter(pk=session.pk).exists())
        self.assertTrue(MemberProfile.objects.using(self.alias_a).get(pk=member.pk).email_notifications)
        self.assertFalse(EmailNotification.objects.using(self.alias_a).exists())


    def member_client_for(self,user,domain=None,csrf=False):
        client=Client(HTTP_HOST=(domain or self.domain_a).hostname,enforce_csrf_checks=csrf)
        client.force_login(user)
        return client

    def renewal_fixture(self):
        user,member,session=self.class_fixture()
        plan=MembershipPlan.objects.using(self.alias_a).get()
        term=MemberSubscription.objects.using(self.alias_a).get(member=member)
        return user,member,plan,term.expires_on

    def test_self_service_profile_updates_allowlist_and_audit(self):
        user,member,session=self.class_fixture()
        original_qr=member.qr_version
        data={'full_name':'Taylor Updated','phone':'09129999999','date_of_birth':'2000-01-02',
            'emergency_contact_name':'Alex','emergency_contact_phone':'09120000000',
            'email':'attacker@example.com','is_active':'','user_id':self.user.pk,'qr_version':str(uuid4()),'waiver_text':'Forged'}
        client=self.member_client_for(user)
        self.assertEqual(client.post('/portal/profile/',data).status_code,302)
        fresh=MemberProfile.objects.using(self.alias_a).get(pk=member.pk)
        self.assertEqual(fresh.full_name,'Taylor Updated');self.assertEqual(fresh.email,member.email)
        self.assertEqual(fresh.qr_version,original_qr);self.assertTrue(fresh.is_active)
        self.assertEqual(fresh.user_id,user.pk);self.assertEqual(fresh.waiver_text,'')
        audit=MemberProfileChange.objects.using(self.alias_a).get()
        self.assertEqual(audit.actor_id,user.pk)
        self.assertNotIn('email',audit.changes)
        self.assertContainsText(self.tenant_client().get(f'/manage/members/{member.pk}/'),'Member profile updates')
        self.assertEqual(client.post('/portal/profile/',data).status_code,302)
        self.assertEqual(MemberProfileChange.objects.using(self.alias_a).count(),1)

    def test_self_service_profile_validation_and_csrf(self):
        user,member,session=self.class_fixture()
        client=self.member_client_for(user)
        data={'full_name':'Test','phone':'0','date_of_birth':str(timezone.localdate()+timedelta(days=1)),
              'emergency_contact_name':'Incomplete','emergency_contact_phone':''}
        self.assertContainsText(client.post('/portal/profile/',data),'Birth date cannot be in the future')
        self.assertFalse(MemberProfileChange.objects.using(self.alias_a).exists())
        self.assertEqual(self.member_client_for(user,csrf=True).post('/portal/profile/',data).status_code,403)

    def test_self_service_renewal_approval_preserves_quote_and_stays_pending(self):
        user,member,plan,start=self.renewal_fixture()
        client=self.member_client_for(user)
        data={'plan':str(plan.pk),'starts_on':str(start),'note':'Please renew','price':'0','status':'active'}
        self.assertEqual(client.post('/portal/renewals/',data).status_code,302)
        row=RenewalRequest.objects.using(self.alias_a).get()
        self.assertEqual(row.price,plan.price)
        MembershipPlan.objects.using(self.alias_a).filter(pk=plan.pk).update(price=900,name='Changed catalog')
        response=self.tenant_client().post(f'/manage/renewals/{row.pk}/review/',{
            f'{row.pk}-action':'approve',f'{row.pk}-note':'Original quote accepted'})
        self.assertEqual(response.status_code,302)
        row=RenewalRequest.objects.using(self.alias_a).select_related('subscription').get()
        self.assertEqual(row.status,'approved');self.assertEqual(row.subscription.status,'pending')
        self.assertEqual(row.subscription.price,plan.price);self.assertEqual(row.subscription.plan_name,plan.name)
        self.assertFalse(PaymentOrder.objects.using(self.alias_a).exists())
        self.assertContainsText(client.get('/portal/renewals/'),'Original quote accepted')
        with gym_database(self.alias_a):
            again=review_renewal(self.alias_a,row.pk,'approve','Retry',self.user.pk)
        self.assertEqual(again.subscription_id,row.subscription_id)
        self.assertEqual(MemberSubscription.objects.using(self.alias_a).count(),2)

    def test_self_service_duplicate_renewal_and_single_pending_request(self):
        user,member,plan,start=self.renewal_fixture()
        first=request_renewal(self.alias_a,member.pk,user.pk,plan.pk,start,'Please')
        second=request_renewal(self.alias_a,member.pk,user.pk,plan.pk,start,'Repeated')
        self.assertEqual(first.pk,second.pk)
        with self.assertRaises(EnrollmentError):request_renewal(self.alias_a,member.pk,user.pk,plan.pk,start+timedelta(days=1),'Different')
        self.assertEqual(RenewalRequest.objects.using(self.alias_a).count(),1)

    def test_self_service_renewal_conflict_dates_and_frozen_term(self):
        user,member,plan,start=self.renewal_fixture()
        for invalid in [timezone.localdate()-timedelta(days=1),timezone.localdate(),timezone.localdate()+timedelta(days=366)]:
            with self.assertRaises(EnrollmentError):request_renewal(self.alias_a,member.pk,user.pk,plan.pk,invalid,'')
        MemberSubscription.objects.using(self.alias_a).filter(member=member).update(status='frozen')
        with self.assertRaises(EnrollmentError):request_renewal(self.alias_a,member.pk,user.pk,plan.pk,start,'')
        self.assertFalse(RenewalRequest.objects.using(self.alias_a).exists())

    def test_self_service_approval_rechecks_new_pending_terms(self):
        user,member,plan,start=self.renewal_fixture()
        row=request_renewal(self.alias_a,member.pk,user.pk,plan.pk,start,'')
        with gym_database(self.alias_a):assign_plan(self.alias_a,member.pk,plan.pk,start,self.user.pk)
        with self.assertRaises(EnrollmentError):review_renewal(self.alias_a,row.pk,'approve','Review',self.user.pk)
        row.refresh_from_db(using=self.alias_a);self.assertEqual(row.status,'pending')

    def test_self_service_cancel_and_decline_never_create_terms(self):
        user,member,plan,start=self.renewal_fixture()
        row=request_renewal(self.alias_a,member.pk,user.pk,plan.pk,start,'')
        client=self.member_client_for(user)
        self.assertEqual(client.get(f'/portal/renewals/{row.pk}/cancel/').status_code,405)
        self.assertEqual(client.post(f'/portal/renewals/{row.pk}/cancel/').status_code,302)
        self.assertEqual(client.post(f'/portal/renewals/{row.pk}/cancel/').status_code,302)
        row2=request_renewal(self.alias_a,member.pk,user.pk,plan.pk,start,'New')
        review_renewal(self.alias_a,row2.pk,'decline','Contact the desk',self.user.pk)
        self.assertEqual(MemberSubscription.objects.using(self.alias_a).count(),1)

    def test_self_service_concurrent_renewal_submission_creates_one_request(self):
        user,member,plan,start=self.renewal_fixture()
        def submit(_):
            try:return request_renewal(self.alias_a,member.pk,user.pk,plan.pk,start,'Same').pk
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(submit,range(2)))
        self.assertEqual(ids[0],ids[1]);self.assertEqual(RenewalRequest.objects.using(self.alias_a).count(),1)

    def test_self_service_concurrent_approvals_create_one_pending_term(self):
        user,member,plan,start=self.renewal_fixture()
        row=request_renewal(self.alias_a,member.pk,user.pk,plan.pk,start,'')
        def approve(_):
            try:return review_renewal(self.alias_a,row.pk,'approve','Accepted',self.user.pk).subscription_id
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(approve,range(2)))
        self.assertEqual(ids[0],ids[1]);self.assertEqual(MemberSubscription.objects.using(self.alias_a).filter(status='pending').count(),1)

    def test_self_service_permissions_member_ownership_and_tenant_isolation(self):
        user,member,plan,start=self.renewal_fixture()
        other,other_member=self.another_class_member()
        row=request_renewal(self.alias_a,member.pk,user.pk,plan.pk,start,'Private renewal')
        client=self.member_client_for(other)
        self.assertNotIn(b'Private renewal',client.get('/portal/renewals/').content)
        self.assertEqual(client.post(f'/portal/renewals/{row.pk}/cancel/').status_code,404)
        self.assertEqual(client.get('/manage/renewals/').status_code,404)
        self.assertEqual(self.tenant_client(self.domain_b).get('/manage/renewals/').status_code,404)
        Membership.objects.create(user=user,organization=self.b,role='member')
        MemberProfile.objects.using(self.alias_b).create(user_id=user.pk,full_name='Gym B profile',email='b@example.com',phone='0')
        self.assertNotIn(b'Private renewal',self.member_client_for(user,self.domain_b).get('/portal/renewals/').content)

    def test_self_service_renewal_notification_and_profile_password_change(self):
        user,member,plan,start=self.renewal_fixture();self.enable_notifications()
        row=request_renewal(self.alias_a,member.pk,user.pk,plan.pk,start,'')
        review_renewal(self.alias_a,row.pk,'approve','Pay at the desk',self.user.pk)
        self.assertTrue(EmailNotification.objects.using(self.alias_a).filter(category='membership_updates',member=member).exists())
        client=self.member_client_for(user)
        self.assertEqual(client.post('/portal/password/',{'old_password':'Wrong','new_password1':'Member-New-Password-92!','new_password2':'Member-New-Password-92!'}).status_code,200)
        self.assertEqual(client.post('/portal/password/',{'old_password':'Desk-Strong-84!','new_password1':'Member-New-Password-92!','new_password2':'Member-New-Password-92!'}).status_code,302)
        user.refresh_from_db();self.assertTrue(user.check_password('Member-New-Password-92!'))
        self.assertEqual(client.get('/portal/profile/').status_code,200)

    def test_self_service_payment_history_is_own_member_only(self):
        user,member,plan,start=self.renewal_fixture()
        _,other=self.another_class_member()
        term=MemberSubscription.objects.using(self.alias_a).get(member=other)
        with gym_database(self.alias_a):
            PaymentOrder.objects.create(subscription=term,merchant_reference='PRIVATE'+uuid4().hex,merchant_code='m',environment='sandbox',payment_method='T',amount=999)
        self.assertContainsText(self.member_client_for(user).get('/portal/payments/'),'No membership payment records yet')
        self.assertEqual(self.tenant_client().get('/portal/payments/').status_code,404)

    def recovery_fixture(self):
        user,member,session=self.class_fixture()
        user.email='identity-'+uuid4().hex+'@example.com';user.save(update_fields=['email'])
        origin='https://'+self.domain_a.hostname
        return user,member,origin

    def test_self_service_recovery_generic_response_and_no_smtp_or_tenant_token(self):
        user,member,origin=self.recovery_fixture()
        client=Client(HTTP_HOST=self.domain_a.hostname)
        with patch('django.core.mail.EmailMultiAlternatives.send') as send:
            response=client.post('/accounts/password-reset/',{'username':user.username,'email':user.email})
            unknown=client.post('/accounts/password-reset/',{'username':'not-an-account','email':'unknown@example.com'})
        self.assertEqual(response.status_code,302);self.assertEqual(response['Location'],unknown['Location'])
        send.assert_not_called()
        row=PasswordRecovery.objects.get(user=user)
        self.assertEqual(row.recipient,user.email);self.assertNotEqual(row.recipient,member.email)
        self.assertFalse(EmailNotification.objects.using(self.alias_a).exists())
        self.assertNotIn('token',[field.name for field in PasswordRecovery._meta.fields])
        production_domain=GymDomain.objects.create(organization=self.a,hostname='recover-'+uuid4().hex+'.example.com',verified_at=timezone.now())
        PasswordRecovery.objects.filter(user=user).delete()
        with override_settings(DEBUG=False):
            production=Client(HTTP_HOST=production_domain.hostname)
            self.assertEqual(production.post('/accounts/password-reset/',{'username':user.username,'email':user.email}).status_code,302)
        self.assertEqual(PasswordRecovery.objects.get(user=user).origin,'https://'+production_domain.hostname)

    def test_self_service_recovery_uses_identity_email_not_gym_contact_email(self):
        user,member,origin=self.recovery_fixture()
        MemberProfile.objects.using(self.alias_a).filter(pk=member.pk).update(email='gym-owner-changed@example.com')
        queue_recovery(self.a,self.alias_a,user.username,'gym-owner-changed@example.com',origin,'127.0.0.1')
        self.assertFalse(PasswordRecovery.objects.filter(user=user).exists())
        queue_recovery(self.a,self.alias_a,user.username,user.email,origin,'127.0.0.1')
        self.assertEqual(PasswordRecovery.objects.get(user=user).recipient,user.email)

    def test_self_service_recovery_sends_central_email_and_link_hidden_from_owner(self):
        from django.core import mail
        user,member,origin=self.recovery_fixture();self.enable_notifications()
        MemberProfile.objects.using(self.alias_a).filter(pk=member.pk).update(email_notifications=False)
        queue_recovery(self.a,self.alias_a,user.username,user.email,origin,'127.0.0.1')
        row=PasswordRecovery.objects.get(user=user)
        with self.mail_transport():deliver_recovery(row.pk)
        self.assertEqual(len(mail.outbox),1);self.assertEqual(mail.outbox[0].to,[user.email])
        self.assertIn(f'/accounts/reset/{row.pk}/',mail.outbox[0].body)
        self.assertFalse(EmailNotification.objects.using(self.alias_a).exists())
        self.assertNotIn(str(row.pk).encode(),self.tenant_client().get('/manage/notifications/').content)

    def test_self_service_recovery_token_single_use_and_wrong_gym_rejected(self):
        user,member,origin=self.recovery_fixture()
        queue_recovery(self.a,self.alias_a,user.username,user.email,origin,'127.0.0.1')
        row=PasswordRecovery.objects.get(user=user);token=token_generator(self.a).make_token(user)
        path=f'/accounts/reset/{row.pk}/{token}/'
        wrong=Client(HTTP_HOST=self.domain_b.hostname)
        self.assertEqual(wrong.get(path).status_code,404)
        client=Client(HTTP_HOST=self.domain_a.hostname)
        response=client.get(path)
        self.assertEqual(response.status_code,302);self.assertIn('set-password',response['Location'])
        clean_url=response['Location']
        self.assertContainsText(client.get(clean_url),'Choose a new password')
        self.assertEqual(client.get(clean_url)['Referrer-Policy'],'no-referrer')
        data={'new_password1':'Recovered-Member-Password-93!','new_password2':'Recovered-Member-Password-93!'}
        self.assertEqual(client.post(clean_url,data).status_code,302)
        user.refresh_from_db();self.assertTrue(user.check_password(data['new_password1']))
        row.refresh_from_db();self.assertEqual(row.status,'used')
        self.assertContainsText(client.get(clean_url),'unavailable or expired')
        self.assertContainsText(client.get(path),'unavailable or expired')

    def test_self_service_recovery_expiry_and_password_changes_invalidate_link(self):
        user,member,origin=self.recovery_fixture()
        queue_recovery(self.a,self.alias_a,user.username,user.email,origin,'127.0.0.1')
        row=PasswordRecovery.objects.get(user=user);token=token_generator(self.a).make_token(user)
        PasswordRecovery.objects.filter(pk=row.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        client=Client(HTTP_HOST=self.domain_a.hostname)
        self.assertContainsText(client.get(f'/accounts/reset/{row.pk}/{token}/'),'unavailable or expired')
        PasswordRecovery.objects.filter(pk=row.pk).update(expires_at=timezone.now()+timedelta(minutes=30))
        user.set_password('Changed-Elsewhere-Password-94!');user.save(update_fields=['password'])
        self.assertContainsText(client.get(f'/accounts/reset/{row.pk}/{token}/'),'unavailable or expired')

    def test_self_service_recovery_staff_owner_disabled_and_foreign_accounts_denied(self):
        user,member,origin=self.recovery_fixture()
        for field,value in [('is_staff',True),('is_superuser',True),('is_active',False)]:
            get_user_model().objects.filter(pk=user.pk).update(**{field:value})
            queue_recovery(self.a,self.alias_a,user.username,user.email,origin,'127.0.0.1')
            get_user_model().objects.filter(pk=user.pk).update(**{field:False if field!='is_active' else True})
        Membership.objects.filter(user=user,organization=self.a).update(is_active=False)
        queue_recovery(self.a,self.alias_a,user.username,user.email,origin,'127.0.0.1')
        self.assertFalse(PasswordRecovery.objects.filter(user=user).exists())
        Membership.objects.filter(user=user,organization=self.a).update(is_active=True)
        queue_recovery(self.b,self.alias_b,user.username,user.email,'https://'+self.domain_b.hostname,'127.0.0.1')
        self.assertFalse(PasswordRecovery.objects.filter(user=user).exists())

    def test_self_service_recovery_throttles_without_exposing_account(self):
        user,member,origin=self.recovery_fixture()
        for _ in range(3):queue_recovery(self.a,self.alias_a,user.username,user.email,origin,'127.0.0.1')
        self.assertEqual(PasswordRecovery.objects.filter(user=user).count(),1)
        for _ in range(12):queue_recovery(self.a,self.alias_a,'unknown','unknown@example.com',origin,'192.0.2.4')
        self.assertLessEqual(RecoveryThrottle.objects.aggregate(value=django.db.models.Max('count'))['value'],10)
        self.assertNotIn('192.0.2.4',str(list(RecoveryThrottle.objects.values_list('bucket',flat=True))))

    def test_self_service_saas_blocks_renewals_but_allows_account_recovery(self):
        user,member,plan,start=self.renewal_fixture()
        subscription,invoice=self.saas_assignment(trial=14)
        change_saas_state(subscription.pk,'suspend',self.user.pk,'Pause')
        client=self.member_client_for(user)
        self.assertEqual(client.get('/portal/renewals/').status_code,503)
        self.assertEqual(client.get('/portal/profile/').status_code,200)
        self.assertEqual(client.get('/portal/password/').status_code,200)
        self.assertEqual(client.get('/accounts/password-reset/').status_code,200)

    def test_self_service_upgrade_preserves_class_and_member_records(self):
        from django.db.migrations.recorder import MigrationRecorder
        user,member,session=self.class_fixture()
        NotificationSettings.objects.using(self.alias_a).create(pk=1)
        with connections[self.alias_a].schema_editor() as editor:
            editor.delete_model(RenewalRequest);editor.delete_model(MemberProfileChange)
            editor.remove_field(NotificationSettings,NotificationSettings._meta.get_field('membership_updates'))
        MigrationRecorder.Migration.objects.using(self.alias_a).filter(app='gym_core',name__startswith='0007_').delete()
        self.make_platform_admin()
        self.assertContainsText(self.client.get(f'/gyms/{self.a.slug}/'),'Retry setup')
        self.assertEqual(self.client.post(f'/gyms/{self.a.slug}/retry-setup/').status_code,302)
        self.assertTrue(ClassSession.objects.using(self.alias_a).filter(pk=session.pk).exists())
        self.assertTrue(MemberProfile.objects.using(self.alias_a).filter(pk=member.pk).exists())
        self.assertEqual(RenewalRequest.objects.using(self.alias_a).count(),0)


    def test_self_service_concurrent_password_resets_consume_once(self):
        user,member,origin=self.recovery_fixture()
        queue_recovery(self.a,self.alias_a,user.username,user.email,origin,'127.0.0.1')
        row=PasswordRecovery.objects.get(user=user);token=token_generator(self.a).make_token(user)
        clients=[Client(HTTP_HOST=self.domain_a.hostname) for _ in range(2)]
        urls=[client.get(f'/accounts/reset/{row.pk}/{token}/')['Location'] for client in clients]
        def reset(index):
            password=f'Concurrent-Reset-Password-{index}-92!'
            try:return clients[index].post(urls[index],{'new_password1':password,'new_password2':password}).status_code
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reset,range(2)))
        self.assertCountEqual(results,[302,200])
        row.refresh_from_db();self.assertEqual(row.status,'used')

    def test_self_service_recovery_transport_disabled_and_safe_retry(self):
        user,member,origin=self.recovery_fixture()
        queue_recovery(self.a,self.alias_a,user.username,user.email,origin,'127.0.0.1')
        row=PasswordRecovery.objects.get(user=user)
        with override_settings(NOTIFICATION_DELIVERY_ENABLED=False):deliver_recovery(row.pk)
        row.refresh_from_db();self.assertEqual(row.attempts,0)
        with self.mail_transport(),patch('django.core.mail.EmailMultiAlternatives.send',side_effect=RuntimeError('smtp-secret-private')):
            deliver_recovery(row.pk)
        row.refresh_from_db();self.assertEqual(row.status,'queued');self.assertEqual(row.outcome,'transport_error')
        self.assertGreater(row.available_at,timezone.now())
        PasswordRecovery.objects.filter(pk=row.pk).update(available_at=timezone.now())
        with self.mail_transport():deliver_recovery(row.pk)
        row.refresh_from_db();self.assertEqual(row.status,'sent');self.assertEqual(row.attempts,2)

    def test_self_service_scheduler_dispatches_only_recovery_ids_and_expires_old_requests(self):
        from gym_core.notifications.tasks import sweep_notifications
        user,member,origin=self.recovery_fixture()
        queue_recovery(self.a,self.alias_a,user.username,user.email,origin,'127.0.0.1')
        row=PasswordRecovery.objects.get(user=user)
        with patch('gym_core.notifications.tasks.process_password_recovery.delay') as dispatch,patch('gym_core.notifications.tasks.process_gym_notifications.delay'):
            sweep_notifications.run()
        self.assertIn(str(row.pk),[call.args[0] for call in dispatch.call_args_list])
        self.assertTrue(all(len(call.args)==1 for call in dispatch.call_args_list))
        PasswordRecovery.objects.filter(pk=row.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        with patch('gym_core.notifications.tasks.process_password_recovery.delay') as dispatch,patch('gym_core.notifications.tasks.process_gym_notifications.delay'):
            sweep_notifications.run()
        self.assertNotIn(str(row.pk),[call.args[0] for call in dispatch.call_args_list])
        row.refresh_from_db();self.assertEqual(row.status,'expired')


    def test_dashboard_owner_summaries_use_own_records_and_production_receipts(self):
        from gym_core.dashboard_data import owner_overview
        _,member,session=self.class_fixture()
        _,sandbox_member,_=self.class_fixture(hour=2)
        term=MemberSubscription.objects.using(self.alias_a).get(member=member)
        sandbox_term=MemberSubscription.objects.using(self.alias_a).get(member=sandbox_member)
        with gym_database(self.alias_a):
            PaymentOrder.objects.create(subscription=term,merchant_reference='LIVE'+uuid4().hex,merchant_code='fixture',
                environment='production',payment_method='TEST',amount=100,net_amount=90,status='paid',paid_at=timezone.now())
            PaymentOrder.objects.create(subscription=sandbox_term,merchant_reference='TEST'+uuid4().hex,merchant_code='fixture',
                environment='sandbox',payment_method='TEST',amount=100,net_amount=100,status='paid',paid_at=timezone.now())
            data=owner_overview({'memberships':True,'payments':True,'attendance':True,'websites':True})
        self.assertEqual(data['active_members'],2)
        self.assertEqual(data['month_receipts'],90)
        self.assertEqual(len(data['recent_members']),2)
        self.assertEqual(len(data['attendance_chart']),7)
        with gym_database(self.alias_b):
            other=owner_overview({'memberships':True,'payments':True,'attendance':True,'websites':True})
        self.assertEqual(other['active_members'],0);self.assertEqual(other['month_receipts'],0)
        response=self.tenant_client().get('/manage/')
        self.assertIn('no-store',response['Cache-Control'])
        self.assertContainsText(response,'COPAY received')
        self.assertContainsText(response,'90.00')
        self.assertNotIn(b'190.00',response.content)

    def test_dashboard_attendance_periods_count_actual_local_visits(self):
        from gym_core.dashboard_data import owner_overview
        member,term,code=self.active_card()
        self.scan_card(code)
        AttendanceVisit.objects.using(self.alias_a).update(checked_in_at=timezone.now()-timedelta(days=20))
        modules={'attendance':True}
        with gym_database(self.alias_a):
            week=owner_overview(modules,7);month=owner_overview(modules,30)
        self.assertEqual(week['period_checkins'],0);self.assertEqual(month['period_checkins'],1)
        self.assertEqual(sum(day['value'] for day in month['attendance_chart']),1)
        self.assertEqual(sum(day['height']>0 for day in month['attendance_chart']),1)
        self.assertContainsText(self.tenant_client().get('/manage/?period=30'),'check-ins in the last 30 days')

    def test_dashboard_member_overview_and_navigation_do_not_expose_management(self):
        user,member,session=self.class_fixture()
        book_class(self.alias_a,session.pk,member.pk,user.pk)
        client=self.member_client_for(user)
        response=client.get('/portal/')
        self.assertIn('no-store',response['Cache-Control'])
        self.assertContainsText(response,'Strength fundamentals')
        self.assertContainsText(response,'Main')
        self.assertContainsText(response,'Your memberships')
        self.assertNotIn(b'href="/manage/members/"',response.content)
        self.assertNotIn(b'href="/manage/notifications/"',response.content)
        staff=self.desk_account()
        desk=Client(HTTP_HOST=self.domain_a.hostname);desk.force_login(staff)
        response=desk.get('/desk/')
        self.assertIn(b'href="/desk/classes/"',response.content)
        self.assertNotIn(b'href="/manage/members/"',response.content)
        self.assertNotIn(b'Administration',response.content)

    def test_dashboard_platform_portfolio_search_and_setup_filters(self):
        self.make_platform_admin()
        response=self.client.get('/',{'q':self.a.slug})
        self.assertContainsText(response,self.a.slug)
        self.assertNotIn(self.b.slug.encode(),response.content)
        self.assertContainsText(response,'Add another gym')
        Organization.objects.filter(pk=self.a.pk).update(database_ready=False)
        response=self.client.get('/',{'q':self.a.slug,'status':'setup'})
        self.assertContainsText(response,self.a.slug)
        self.assertContainsText(response,'Needs setup')
        response=self.client.get('/',{'q':self.a.slug,'status':'ready'})
        self.assertNotIn(f'<strong>{self.a.name}</strong>'.encode(),response.content)
        self.assertContainsText(response,'No gyms match this view')


if __name__ == "__main__":
    try:
        with override_settings(ALLOWED_HOSTS=["*"], PLATFORM_HOSTS=["localhost", "testserver"]):
            unittest.main(verbosity=2)
    finally:
        # Only delete databases created by this isolated test run.
        central = settings.DATABASES["default"]
        if central["NAME"].startswith("test_gym_platform_"):
            names = [f"gym_{key.hex}" for key in Organization.objects.values_list("database_key", flat=True)]
            connections.close_all()
            with psycopg.connect(**settings.GYM_PROVISIONER, autocommit=True) as admin:
                for name in names + [central["NAME"]]:
                    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
                    admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
