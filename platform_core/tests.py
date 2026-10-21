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


class SaaSCalendarTests(SimpleTestCase):
    def test_renewals_preserve_anchor_across_short_months_and_leap_years(self):
        from datetime import date
        from platform_core.billing.services import end_of_period
        february = end_of_period(date(2026, 1, 31), "month", 31)
        self.assertEqual(february, date(2026, 2, 28))
        self.assertEqual(end_of_period(february, "month", 31), date(2026, 3, 31))
        self.assertEqual(end_of_period(date(2027, 2, 28), "year", 29), date(2028, 2, 29))


class SubscriptionGateRenderingTests(SimpleTestCase):
    def test_early_tenant_response_uses_gym_routes_without_tenant_database_queries(self):
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory
        from django.urls import get_urlconf, set_urlconf
        from gyms.models import Organization
        from tenants.subscription_middleware import TenantSubscriptionMiddleware
        request = RequestFactory().get("/desk/")
        request.tenant = Organization(name="Test gym", slug="test-gym")
        request.user = AnonymousUser()
        request.urlconf = "gym_core.urls"
        previous = get_urlconf()
        try:
            set_urlconf("platform_config.urls")
            response = TenantSubscriptionMiddleware(lambda request: None).block(request)
            self.assertEqual(response.status_code, 503)
            self.assertIn(b"Gym owner sign-in", response.content)
            self.assertIn("no-store", response["Cache-Control"])
            self.assertEqual(get_urlconf(), "platform_config.urls")
        finally:
            set_urlconf(previous)
