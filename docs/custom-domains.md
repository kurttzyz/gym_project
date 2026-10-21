# Gym portals without per-gym terminal commands

Powerhouse provisions gym databases, owner accounts, and starter websites through
the Add gym form. Domain registration, activation, verification, and setup retries
are also available in the platform dashboard. Operator management commands remain
optional maintenance tools, not the normal gym-onboarding workflow.

## Local Windows workflow

1. Sign in to Powerhouse at `http://127.0.0.1:8000/` as a platform superuser.
2. Select Add another gym. Enter the gym, owner credentials, and initial branding.
   A real custom domain is optional; leave it blank for local testing.
3. Create gym & owner. In DEBUG mode, on a loopback platform host, Powerhouse
   creates and activates a local address such as `ironfit.localhost` automatically.
4. On the gym detail page, click **Open website**, **Owner login**, or **Owner dashboard**.
   The links preserve the port used by your local platform (e.g. 8000).
5. For an already-created gym, open its detail page and click **Enable local portal**.
   Its existing owner, password, database, and website edits are preserved.

Modern Chrome and Edge resolve `.localhost` to the local machine automatically.
No hosts-file entry, public DNS record, or verification command is needed. If a
browser/proxy does not honor the localhost convention, use a supported local
browser and bypass any proxy for localhost addresses. Local portals are not meant
to be shared with another computer: localhost always refers to the viewer's PC.

Human-readable hostnames use the gym slug; a UUID-based fallback prevents a
collision from taking over another gym's hostname. Actual links are displayed in
the dashboard. Existing disabled domains are not silently reactivated. `.test`
domains are still supported for manual development, but unlike `.localhost` they
need an operating-system hosts entry. Use the automatic local portal instead.

Local automation requires `DJANGO_DEBUG=true` and a platform request on localhost,
127.0.0.1, or ::1. Local activation is denied on remote platform hosts and when
DEBUG is false. Local tenant domains are rejected outside development, even if
an old local domain registry record is marked verified.

## Real custom domains

A gym uses its own independent hostname such as `ironfitgym.com`; no Powerhouse
subdomain is required. Both apex and www are supported as separate domain bindings.

1. Enter the hostname in the Add gym form, or add it to an existing gym's detail page.
   Supply only the hostname, without scheme, port, or path.
2. Powerhouse displays a DNS TXT challenge. In the domain's DNS provider, publish
   `_powerhouse-verification.ironfitgym.com` with the exact displayed value.
   Some DNS panels expect just `_powerhouse-verification` in the name field.
3. Click **Verify domain** in Powerhouse. The server checks public DNS with a
   bounded timeout. Missing or incorrect proof leaves the domain pending and
   provides a retry message. There is no command to run.
4. Route the domain to your deployment using A/AAAA, or a provider-supported alias.
   For subdomains, a CNAME can target your hosting ingress where supported.
5. Configure your ingress/reverse proxy for that exact hostname and a valid HTTPS
   certificate. Preserve the original Host header. Prefer completing routing and
   TLS before activating the hostname. The dashboard's public-domain links use HTTPS.

The app checks ownership; it does not control your DNS provider or hosting service.
It cannot automatically buy domains, edit external DNS records, or issue/configure
HTTPS without those provider integrations. Verification does not claim that DNS
routing or TLS has been configured. No provider credentials are currently required
or embedded. Keep the TXT record in place; periodic reverification remains future work.

## Setup recovery

A provisioning failure retains the central gym, owner account, and membership.
The platform detail page displays **Retry setup**, which reapplies migrations and
initializes missing website settings while preserving owner passwords/content.
Older gyms whose website schema has not been initialized can use the same button.
If PostgreSQL is down, start its service and retry. A previously ready database
that disappears must be restored; the button never silently replaces it empty.
No new owner is created by retrying setup.

## Security and configuration

`PLATFORM_HOSTS` lists platform-only hostnames without schemes or ports. Local
defaults are localhost,127.0.0.1,[::1]. Do not add gym hostnames there. Domain bindings
are immutable and unique. Disabled, unknown, and unverified domains fail closed.

All dashboard actions require a platform superuser and CSRF-protected POST.
Verification resolves the domain within the selected gym; changing a URL/domain ID
cannot verify another gym's binding. A posted `local=true` cannot bypass public DNS.
The CLI and web dashboard share the same verification implementation.

Django ALLOWED_HOSTS is dynamic (`*`); first-position TenantHostMiddleware performs
exact registry/PLATFORM_HOSTS validation before redirects and authentication.
X-Forwarded-Host is not trusted. Cookies are host-only; production makes them secure.
Tenant URLconfs do not expose platform onboarding or administration routes.

See [owner websites](owner-websites.md) for owner permissions and customization.

## Optional VPS automation

The manual ingress steps above can now be handled by the opt-in
[domain hosting agent](domain-hosting.md). Once installed, it independently checks
A/TXT DNS, creates a managed Nginx site and certificate, and updates hosting status
on the gym detail page. External DNS edits remain the domain owner's step. Without
this opt-in installation, the existing manual and local workflows are unchanged.
