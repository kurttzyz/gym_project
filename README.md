# Gym Platform — Multi-Tenant SaaS

Django 5.2 LTS with PostgreSQL 17. The platform hosts independent gym businesses.
Each gym has a dedicated PostgreSQL database and login role. User identities,
sessions, gym registration, and access memberships remain in a central database.

## Local development

Requires Python 3.12+, Docker Engine, and Docker Compose.

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/init_dev_env.py
docker compose up -d --wait
.venv/bin/python scripts/bootstrap_postgres.py
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py migrate_gym_databases
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

The setup helper generates credentials in ignored `.env` with restricted file
permissions and preserves existing files. Django and Compose load `.env`; exported
variables take precedence. No application users or passwords are seeded.
PostgreSQL is exposed only on loopback, with password authentication. Docker's
`postgres_data` volume retains local data across container restarts. `docker compose
stop` stops the server without removing data; never use `down -v` on data to retain.

Sign in at `/accounts/login/` on a platform host. Platform superusers can **Add another gym** from `/`.
Creation asks for the owner account and automatically provisions the gym database
and website template. Existing gyms have a Create gym owner action on their detail
page; additional memberships can be managed in `/admin/`. Gym users sign in on their gym's verified custom domain and see only that gym. Gym records cannot be created/deleted through central admin, and
branch administration is not exposed through the central admin.

## Structure

- `platform_config/`: canonical settings, platform URLs, ASGI and WSGI.
- `platform_core/`: platform onboarding/dashboard; SaaS plans and billing boundaries.
- `gym_core/`: tenant portal; members, paid memberships, attendance, staff, trainers boundaries.
- `pos/`: product, inventory, and sales boundaries.
- `payments/`: tenant payment boundaries and provider-neutral adapter contract.
- `websites/`: shared brand-aware templates, branding, public-page boundary.
- `tenants/`: verified custom-domain resolution; no copied code per gym.
- `accounts/`, `gyms/`, `tenant_data/`: existing migration-stable identity, registry,
  access, and branch models.
- `config/`: router/admin infrastructure and compatibility entry points.

See [module architecture and billing boundaries](docs/modules.md) for implemented
versus planned capabilities. One platform serves all gyms; gym-to-platform SaaS
billing and member-to-gym billing are separate domains and data stores.

A gym (`Organization`) is a tenant; a `Location` is a branch in its database.
Membership roles are owner, manager, staff, and member. These access memberships
are separate from planned paid gym subscriptions.

## Endpoints

- `/`: Powerhouse gym registry on platform hosts; public gym website on tenant hosts.
- `/gyms/new/`: platform-superuser-only gym provisioning.
- `/gyms/<slug>/`: platform-only gym details, branding, domains, and branches.
- `/accounts/login/` and `/accounts/logout/`: session login and POST logout.
- `/api/organizations/`: platform-superuser-only organization API.
- `/health/`: platform database connectivity check; not an all-gym readiness check.
- `/admin/`: platform maintenance and access assignment.

Owner website editing is available at `/manage/website/` and the owner dashboard
at `/manage/` on each gym domain. Member portals are at `/portal/`. Self-service
signup, branch editing, payment collection, attendance, and POS transactions
are not implemented yet. Gym membership plans, applications, and contact inboxes
are implemented; approval is not paid membership activation. See [architecture](docs/architecture.md).

## Configuration and security

`.env.example` lists configuration names. The central `PGUSER` owns only its
platform database and has no role/database creation privileges. `PGADMIN_*` selects
a separate privileged provisioning connection; normal tenant reads do not use it.
Provisioning requires permission to create roles/databases and set their ownership;
local development uses the container administrator. Production should isolate this
privilege in a trusted control-plane worker. The application process currently has
access to provisioner configuration for superuser-triggered gym creation.

Each gym uses a UUID-based database/role name. Credentials are derived using
`GYM_DATABASE_SECRET`; preserve this secret securely. Do not casually regenerate it
or change existing role passwords: rotation requires updating PostgreSQL credentials
and application configuration together. Credentials are never stored in source.
Use secure external secret management in production.

For deployment, disable `DJANGO_DEBUG`, configure a secure `DJANGO_SECRET_KEY`,
allowed hosts, trusted PostgreSQL TLS (`PGSSLMODE=verify-full` with the appropriate
CA), HTTPS, static assets, backups, and a production ASGI/WSGI server. Local Compose
is a development service, not a production deployment configuration.

## Migrations and maintenance

```sh
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py migrate_gym_databases
```

Use `migrate_gym_databases --slug <gym-slug>` to migrate one gym or retry failed
initial provisioning. A registered ready gym with a missing database requires backup
restoration; migration refuses to silently replace it. Databases and login roles
are not deleted automatically. Slug edits do not change database identities.

Back up the central database, every gym database, database roles, and the credential
secret together. The legacy shared-branch split migration copies and verifies data
before deleting the shared table; it is intentionally irreversible. PostgreSQL
sequences are reset after transferring existing IDs. This migration handles an
older shared schema on the configured server; it does not import SQLite files.
The previous local SQLite files are preserved but no longer used. Importing a
populated SQLite installation requires a separate data-transfer procedure.

Trusted development maintenance can add a branch with:

```python
from gyms.models import Organization
from gyms.databases import ready_database
from config.database_router import gym_database
from tenant_data.models import Location

gym = Organization.objects.get(slug="your-gym")
with gym_database(ready_database(gym)):
    Location.objects.create(name="Main branch")
```

Web handlers must authorize membership before selecting a gym database. Future
tenant-owned models belong in `tenant_data`; avoid cross-database foreign keys.

## Validation

```sh
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py test
```

The Django runner executes a subprocess integration suite containing 60 checks
and two direct database-routing boundary tests.
It creates uniquely named test databases/roles, tests legacy data migration,
provisioning/retries, missing database handling, authentication, application isolation,
and PostgreSQL connection privileges, then removes only its own databases/roles.
Run against a development PostgreSQL server with provisioning access.

## White-label gym domains

Each gym uses its own independent domain, name, logo, and color. During local
onboarding on a loopback platform host, a ready-to-use `.localhost` portal is
created automatically. Open the website or owner-login links from the gym's detail
page. Existing gyms can select Enable local portal. No per-gym terminal commands
or hosts-file edits are needed in modern Chrome/Edge.

Real domains can be registered during onboarding or on the gym detail page. Publish
the displayed DNS TXT challenge and click Verify domain. Actual DNS routing and
HTTPS require your hosting provider; the app does not configure them automatically.
Unknown/unverified domains never resolve to a gym, and tenant hosts have no platform
routes. Platform-only hosts are configured through PLATFORM_HOSTS.

See the [dashboard domain workflow](docs/custom-domains.md).

## Reusable websites and gym owner accounts

Powerhouse creates the gym and owner account in one onboarding form. Owners get a
dedicated dashboard on their own domain and can customize the shared website
through a tenant-local settings form. Two design presets are included. New gyms
start from common defaults; each gym's edits stay in its own database. See the
[owner onboarding and website guide](docs/owner-websites.md).

When upgrading, run both `manage.py migrate` and `manage.py migrate_gym_databases`
before starting Django so existing gyms receive their website settings.

## Try two gyms through the UI

Use Add another gym to create IronFit and Titans with their respective owners.
Both automatically receive separate databases, websites, and local portals. Follow
the [two-gym walkthrough](docs/demo.md). No demo scripts are needed. If provisioning
fails or an older gym lacks its website schema, use Retry setup on its detail page.

## Membership website

Gym domains include Home, Memberships, Our gym, Contact, and Join pages. Owners
create and publish their own PHP-priced plans at `/manage/plans/`, review requests
at `/manage/applications/`, and read messages at `/manage/inbox/`. Plan requests
are inquiries, not charges or automatic account activation. See the complete
[membership website workflow](docs/membership-website.md).
