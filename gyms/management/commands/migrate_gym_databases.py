from django.core.management.base import BaseCommand
from gyms.databases import provision_database
from gyms.models import Organization


class Command(BaseCommand):
    help = "Provision or migrate each registered gym database; optionally select --slug."

    def add_arguments(self, parser):
        parser.add_argument("--slug")

    def handle(self, *args, **options):
        gyms = Organization.objects.all()
        if options["slug"]:
            gyms = gyms.filter(slug=options["slug"])
            if not gyms.exists():
                from django.core.management.base import CommandError
                raise CommandError("Unknown gym")
        for gym in gyms:
            from gyms.databases import GymDatabaseUnavailable
            from django.core.management.base import CommandError
            try:
                provision_database(gym)
            except GymDatabaseUnavailable as error:
                raise CommandError(str(error)) from error
            self.stdout.write(f"Migrated {gym.slug}")
