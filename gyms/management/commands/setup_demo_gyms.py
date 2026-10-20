"""Interactive, local-only sample onboarding without hardcoded passwords."""
from getpass import getpass

import psycopg
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction
from django.utils import timezone

from gyms.databases import GymDatabaseUnavailable, provision_database
from gyms.models import GymDomain, Membership, Organization, normalize_hostname


DEMOS = (
    {"slug": "demo-ironfit", "name": "IronFit Demo", "color": "#28734e",
     "username": "demo_ironfit_owner", "hostname": "demo-ironfit.test"},
    {"slug": "demo-titans", "name": "Titans Demo", "color": "#7145a3",
     "username": "demo_titans_owner", "hostname": "demo-titans.test"},
)


class Command(BaseCommand):
    help = "Create two local sample gyms and owners; prompts securely for a demo password."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Sample onboarding is available only with DJANGO_DEBUG=true")
        User = get_user_model()
        planned = []
        # Check both identities/domains first; do not take over existing records.
        for demo in DEMOS:
            try:
                normalize_hostname(demo["hostname"])
            except ValidationError as error:
                raise CommandError("Demo hostname is reserved by platform settings") from error
            gym = Organization.objects.filter(slug=demo["slug"]).first()
            owner = User.objects.filter(username=demo["username"]).first()
            domain = GymDomain.objects.filter(hostname=demo["hostname"]).first()
            if gym and gym.name != demo["name"]:
                raise CommandError(f"Gym identifier {demo['slug']} is already in use")
            if bool(gym) != bool(owner) or (owner and (
                owner.is_staff or owner.is_superuser or not owner.is_active or not
                Membership.objects.filter(organization=gym, user=owner, role="owner", is_active=True).exists()
            )):
                raise CommandError(f"Demo identity {demo['username']} conflicts with existing data")
            if domain and (not gym or domain.organization_id != gym.pk or not domain.is_active):
                raise CommandError(f"Domain {demo['hostname']} is already in use or disabled")
            planned.append((demo, gym, owner))

        password = None
        if any(owner is None for _, _, owner in planned):
            self.stdout.write("Choose a local demo password for both new sample owners (input is hidden).")
            try:
                password = getpass("Demo owner password: ")
                if password != getpass("Confirm demo owner password: "):
                    raise CommandError("Passwords do not match; no demo records were created")
                for demo, _, owner in planned:
                    if owner is None:
                        validate_password(password, User(username=demo["username"]))
            except (EOFError, KeyboardInterrupt) as error:
                raise CommandError("Run this command interactively to set demo owner passwords") from error
            except ValidationError as error:
                raise CommandError(" ".join(error.messages)) from error

        gyms = []
        with transaction.atomic():
            for demo, gym, owner in planned:
                if gym is None:
                    gym = Organization.objects.create(
                        slug=demo["slug"], name=demo["name"], primary_color=demo["color"]
                    )
                    owner = User.objects.create_user(
                        username=demo["username"], password=password,
                        email=f"{demo['slug']}@example.invalid",
                    )
                    Membership.objects.create(organization=gym, user=owner, role="owner")
                gyms.append((demo, gym))
        for demo, gym in gyms:
            try:
                provision_database(gym)
            except (GymDatabaseUnavailable, DatabaseError, psycopg.Error, OSError) as error:
                raise CommandError(
                    f"Could not provision {gym.slug}. Existing records were preserved; fix the database connection and rerun."
                ) from error
            domain, _ = GymDomain.objects.get_or_create(
                hostname=demo["hostname"], defaults={"organization": gym}
            )
            if domain.verified_at is None:
                domain.verified_at = timezone.now()
                domain.save(update_fields=["verified_at"])
            self.stdout.write(f"{demo['name']}: {demo['hostname']} | owner: {demo['username']}")
        self.stdout.write("Existing owner passwords and website customizations are preserved on reruns.")
        self.stdout.write("Add this hosts-file entry on your PC:")
        self.stdout.write("127.0.0.1 demo-ironfit.test demo-titans.test")
        self.stdout.write("Then start runserver and visit http://demo-ironfit.test:8000/ or http://demo-titans.test:8000/")
