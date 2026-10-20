import secrets
import dns.exception
import dns.resolver
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from gyms.models import GymDomain, normalize_hostname


class Command(BaseCommand):
    help = "Verify domain ownership using a DNS TXT challenge."

    def add_arguments(self, parser):
        parser.add_argument("hostname")
        parser.add_argument("--local", action="store_true", help="DEBUG-only .test/.localhost development domains")

    def handle(self, *args, **options):
        try:
            domain = GymDomain.objects.get(hostname=normalize_hostname(options["hostname"]))
        except (GymDomain.DoesNotExist, ValidationError) as error:
            raise CommandError("Register a valid gym domain first") from error
        if not domain.is_active:
            raise CommandError("Domain is disabled")
        if options["local"]:
            if not settings.DEBUG or not domain.hostname.endswith((".test", ".localhost")):
                raise CommandError("Local verification requires DEBUG and a .test/.localhost hostname")
        else:
            try:
                answers = dns.resolver.resolve(
                    f"_powerhouse-verification.{domain.hostname}.", "TXT", lifetime=5
                )
                tokens = [b"".join(record.strings).decode("ascii") for record in answers]
            except (dns.exception.DNSException, UnicodeError) as error:
                raise CommandError("Could not read the DNS TXT challenge; verify DNS and retry") from error
            if not any(secrets.compare_digest(token, domain.verification_token) for token in tokens):
                raise CommandError("DNS TXT challenge does not match; domain remains unverified")
        domain.verified_at = timezone.now()
        domain.save(update_fields=["verified_at"])
        self.stdout.write(f"Verified {domain.hostname}")
