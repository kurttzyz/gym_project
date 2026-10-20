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
from websites.models import WebsiteSettings
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
        for alias in (self.alias_a, self.alias_b):
            tables = connections[alias].introspection.table_names()
            self.assertIn("tenant_data_location", tables)
            self.assertIn("websites_websitesettings", tables)
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
            self.assertEqual(self.client.post("/gyms/new/", {"name": "Retry", "slug": slug, "primary_color": "#245ac5", **self.owner_payload()}).status_code, 503)
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
        self.domain_a.verified_at = None
        self.domain_a.save()
        with patch("gyms.management.commands.verify_gym_domain.dns.resolver.resolve", return_value=[SimpleNamespace(strings=[b"wrong"])]) as resolve:
            with self.assertRaises(CommandError):
                call_command("verify_gym_domain", self.domain_a.hostname)
            resolve.assert_called_once_with(f"_powerhouse-verification.{self.domain_a.hostname}.", "TXT", lifetime=5)
        self.domain_a.refresh_from_db()
        self.assertIsNone(self.domain_a.verified_at)
        token = self.domain_a.verification_token.encode()
        with patch("gyms.management.commands.verify_gym_domain.dns.resolver.resolve", return_value=[SimpleNamespace(strings=[token[:10], token[10:]])]):
            call_command("verify_gym_domain", self.domain_a.hostname)
        self.domain_a.refresh_from_db()
        self.assertIsNotNone(self.domain_a.verified_at)

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
            editor.delete_model(WebsiteSettings)
        MigrationRecorder.Migration.objects.using(self.alias_a).filter(app="websites").delete()
        self.assertEqual(Client(HTTP_HOST=self.domain_a.hostname).get("/").status_code, 503)
        call_command("migrate_gym_databases", slug=self.a.slug, verbosity=0)
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
