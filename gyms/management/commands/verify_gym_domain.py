from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from gyms.domains import DomainVerificationError, verify_domain
from gyms.models import GymDomain, normalize_hostname


class Command(BaseCommand):
    help = "Verify domain ownership using a DNS TXT challenge (also available in Powerhouse)."

    def add_arguments(self, parser):
        parser.add_argument("hostname")
        parser.add_argument("--local", action="store_true")

    def handle(self, *args, **options):
        try:
            domain = GymDomain.objects.get(hostname=normalize_hostname(options["hostname"]))
            verify_domain(domain, local=options["local"])
        except (GymDomain.DoesNotExist, ValidationError) as error:
            raise CommandError("Register a valid gym domain first") from error
        except DomainVerificationError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"Verified {domain.hostname}")
