# White-label custom domains

Powerhouse is the hosting provider. A gym uses its own hostname such as
`ironfitgym.com`; no Powerhouse subdomain is required. A domain maps exactly to one
gym, which has its own PostgreSQL database/login and branding. `www` is a separate
hostname: register and verify it separately if you want to use it.

## Platform setup

`PLATFORM_HOSTS` is the comma-separated list of platform-only hostnames, without
schemes or ports. Local defaults are `localhost,127.0.0.1,[::1]`. Set it in `.env`:

```dotenv
PLATFORM_HOSTS=localhost,127.0.0.1,[::1]
```

Only platform superusers can sign in to the Powerhouse control panel. Existing
tenant accounts should sign in at their own domain instead. Central administration
is available only on platform hosts and requires an active staff superuser.
Do not put gym domains in PLATFORM_HOSTS: doing so reserves them for the platform.
The old DJANGO_ALLOWED_HOSTS setting is replaced by registry-based host validation.

## Onboard a gym

1. Sign in to the platform and select Add another gym. Supply the gym's name,
   internal slug, HTTPS logo URL (optional), and six-digit brand color. Creation
   provisions the separate database. Branding can be edited in platform admin.
2. Assign the gym's owner/user access membership in `/admin/`. Users are shared
   identities but must have an active membership for each gym portal they access.
3. Open the gym's platform detail page and register its hostname. Supply only a
   hostname, e.g. `ironfitgym.com`, without `https://`, a port, or a path. Hostnames
   are normalized to lowercase IDNA, unique, and cannot be rebound to another gym.
4. Give the gym owner the displayed DNS TXT name and random challenge value.
   They must publish `_powerhouse-verification.ironfitgym.com` with that value.
   Some DNS panels expect only `_powerhouse-verification` in the record-name box.
5. After DNS propagation, a trusted platform operator runs:

   ```sh
   python manage.py verify_gym_domain ironfitgym.com
   ```

   This queries public DNS and requires the exact TXT token. Failure does not mark
   a pending domain verified. Verification is operator-controlled, not tenant editable.
6. Point the domain's A/AAAA or provider-supported alias record to your deployment.
   If using a CNAME on a subdomain, point it to the ingress hostname your deployment
   provides. Apex CNAME support depends on the DNS provider.
7. Configure your reverse proxy/ingress to accept that verified hostname, preserve
   the original Host, and serve a valid HTTPS certificate for it. Prefer configuring
   routing/TLS before the verification step makes the hostname active in Django.

The app implements domain binding, verification, and routing. It does not buy
customer domains, change their DNS, issue certificates, or configure a public ingress.
Those steps require your actual domain/deployment access. The DNS TXT token should
remain in place; periodic reverification and ownership-change detection are future work.
Disable a domain in central admin when retiring it or when ownership is uncertain.

## Local Windows testing without buying a domain

1. Start Django at `127.0.0.1:8000` and sign in at `http://127.0.0.1:8000/` as your
   platform superuser. Create a gym (e.g. IronFit), assign its user membership in
   admin, and register `ironfit.test` on the gym detail page.
2. With Notepad running as administrator, edit:

   ```text
   C:\Windows\System32\drivers\etc\hosts
   ```

   Add:

   ```text
   127.0.0.1 ironfit.test
   ```

3. From the activated Python environment, run:

   ```powershell
   python manage.py verify_gym_domain ironfit.test --local
   ```

   The bypass works only with DJANGO_DEBUG=true and `.test`/`.localhost` hostnames.
   It cannot activate public custom domains or run when debug is disabled.
4. Visit `http://ironfit.test:8000/`. Its login and dashboard show IronFit branding;
   the user must have an active IronFit membership. Platform accounts with no gym
   membership cannot use the gym portal. Signing into Powerhouse does not sign you
   into independent domains: each host has its own session cookie.
5. Repeat with `titans.test` for a second gym and a second hosts-file entry.

## Boundaries and production configuration

Unknown, unverified, and disabled hosts return 404 with no default tenant fallback.
Malformed hosts are rejected. Tenant hosts have no `/admin/`, platform provisioning,
platform gym-list API, or slug-based gym-switching routes. Membership authorization
is checked before selecting a tenant database, including for superusers on a tenant
host. Shared platform users and memberships remain central by design.

Django ALLOWED_HOSTS uses `*` because domains are dynamic; TenantHostMiddleware is
the first middleware and performs the actual exact host allowlisting. Do not remove
or reorder it behind redirects or host-dependent middleware. X-Forwarded-Host is
not trusted. Session and CSRF cookies are host-only; production DEBUG=false makes
both secure. Keep CSRF origin checks enabled. No wildcard CSRF origins are added.
For a TLS-terminating proxy, configure Django's secure-proxy header only if the
trusted proxy strips incoming spoofed headers and sets that header itself.

Tenant-owned operational features should live under `config.tenant_urls`, check
request.tenant membership, and use an explicit authorized gym_database context.
Platform control-plane views belong under config.urls and require a superuser.
