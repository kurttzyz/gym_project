"""Real PostgreSQL integration suite, isolated from Django's test database aliases."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
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
from gyms.models import Organization, Membership
from tenant_data.models import Location


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
        for alias in (self.alias_a, self.alias_b):
            tables = connections[alias].introspection.table_names()
            self.assertIn("tenant_data_location", tables)
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
        with patch("gyms.views.ready_database") as lookup:
            self.assertEqual(self.client.get(f"/gyms/{self.b.slug}/").status_code, 404)
            lookup.assert_not_called()
        response = self.client.get(f"/gyms/{self.a.slug}/")
        self.assertContainsText(response, "Address A")
        self.assertNotIn(b"Private address B", response.content)
        Membership.objects.filter(user=self.user).update(is_active=False)
        self.assertEqual(self.client.get(f"/gyms/{self.a.slug}/").status_code, 404)

    def assertContainsText(self, response, text):
        self.assertEqual(response.status_code, 200)
        self.assertIn(text.encode(), response.content)

    def test_platform_creation_and_retry_are_repeatable(self):
        self.user.is_superuser = True
        self.user.save()
        slug = uuid4().hex
        response = self.client.post("/gyms/new/", {"name": "New gym", "slug": slug})
        self.assertEqual(response.status_code, 302)
        gym = Organization.objects.get(slug=slug)
        alias = ready_database(gym)
        with gym_database(alias):
            Location.objects.create(name="Retained")
        provision_database(gym)
        with gym_database(alias):
            self.assertEqual(Location.objects.get().name, "Retained")
        response = self.client.post("/gyms/new/", {"name": "Duplicate", "slug": slug})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Organization.objects.filter(slug=slug).count(), 1)

    def test_members_cannot_provision_or_view_other_gym_in_list(self):
        response = self.client.post("/gyms/new/", {"name": "Forbidden", "slug": uuid4().hex})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/organizations/").json()["organizations"], [
            {"id": self.a.id, "name": self.a.name, "slug": self.a.slug}
        ])
        self.user.is_staff = True
        self.user.save()
        from django.contrib.auth.models import Permission
        self.user.user_permissions.add(Permission.objects.get(codename="view_organization"))
        self.assertEqual(self.client.get("/admin/gyms/organization/").status_code, 403)

    def test_missing_database_fails_without_recreating(self):
        _, name = register_database(self.a)
        connections[self.alias_a].close()
        with psycopg.connect(**settings.GYM_PROVISIONER, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        self.assertEqual(self.client.get(f"/gyms/{self.a.slug}/").status_code, 503)
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
        with patch("gyms.views.provision_database", side_effect=DatabaseError("unavailable")):
            self.assertEqual(self.client.post("/gyms/new/", {"name": "Retry", "slug": slug}).status_code, 503)
        gym = Organization.objects.get(slug=slug)
        self.assertFalse(gym.database_ready)
        call_command("migrate_gym_databases", slug=slug, verbosity=0)
        gym.refresh_from_db()
        self.assertTrue(gym.database_ready)

    def test_login_and_health(self):
        self.client.logout()
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/health/").json(), {"status": "ok"})
        self.assertEqual(self.client.post("/accounts/login/", {
            "username": self.user.username, "password": "test-password"
        }).status_code, 302)
        self.assertContainsText(self.client.get("/"), self.a.name)


if __name__ == "__main__":
    try:
        with override_settings(ALLOWED_HOSTS=["localhost", "testserver"]):
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
