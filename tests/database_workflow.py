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
from django.db import connection, connections, DatabaseError, IntegrityError
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
from gym_core.members.services import issue_invitation, end_of_term
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
from websites.forms import WebsiteSettingsForm
from django.forms.models import model_to_dict


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
        self.assertEqual(self.client.get("/admin/gyms/organization/").status_code, 302)

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
        for path in ("/admin/", "/gyms/new/", f"/gyms/{self.b.slug}/", "/api/organizations/"):
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
        self.assertEqual(client.get("/admin/").status_code, 404)

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
            self.assertIn(f"background:{original.primary_color}".encode(), response.content)

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
                 f"/gyms/{self.a.slug}/domains/{self.domain_a.pk}/verify/")
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
            editor.delete_model(AttendanceScan);editor.delete_model(AttendanceVisit)
            editor.remove_field(MemberProfile,MemberProfile._meta.get_field("qr_version"))
        MigrationRecorder.Migration.objects.using(self.alias_a).filter(app="gym_core",name="0004_memberprofile_qr_version_attendancevisit_and_more").delete()
        self.user.is_superuser=True;self.user.save()
        self.assertContainsText(self.client.get(f"/gyms/{self.a.slug}/"),"Retry setup")
        self.assertEqual(self.client.post(f"/gyms/{self.a.slug}/retry-setup/").status_code,302)
        member.refresh_from_db(using=self.alias_a)
        self.assertEqual(member.full_name,"Jamie Applicant")
        self.assertIsNotNone(member.qr_version)
        self.assertEqual(AttendanceVisit.objects.using(self.alias_a).count(),0)
        self.assertEqual(MemberSubscription.objects.using(self.alias_a).count(),1)



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
