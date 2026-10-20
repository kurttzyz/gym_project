from types import SimpleNamespace
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from config.database_router import GymDatabaseRouter, gym_database


class ModuleDatabaseBoundaryTests(SimpleTestCase):
    def test_operational_apps_cannot_fall_back_to_central_database(self):
        router = GymDatabaseRouter()
        for label in ("gym_core", "pos", "payments", "websites", "tenant_data"):
            model = SimpleNamespace(_meta=SimpleNamespace(app_label=label))
            with self.subTest(app=label):
                with self.assertRaises(ImproperlyConfigured):
                    router.db_for_read(model)
                with self.assertRaises(ImproperlyConfigured):
                    router.db_for_write(model)
                with gym_database("gym_test_boundary"):
                    self.assertEqual(router.db_for_read(model), "gym_test_boundary")
                self.assertFalse(router.allow_migrate("default", label))
                self.assertTrue(router.allow_migrate("gym_test_boundary", label))

    def test_control_plane_stays_central_inside_tenant_scope(self):
        router = GymDatabaseRouter()
        with gym_database("gym_test_boundary"):
            for label in ("platform_core", "accounts", "gyms", "sessions"):
                with self.subTest(app=label):
                    model = SimpleNamespace(_meta=SimpleNamespace(app_label=label))
                    self.assertEqual(router.db_for_read(model), "default")
                    self.assertEqual(router.db_for_write(model), "default")
                    self.assertTrue(router.allow_migrate("default", label))
                    self.assertFalse(router.allow_migrate("gym_test_boundary", label))
