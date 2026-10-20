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
signup, branch editing, billing, plans, attendance, and business transaction APIs
are not implemented yet. See [architecture](docs/architecture.md).

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

The Django runner executes a subprocess integration suite containing 32 checks
and two direct database-routing boundary tests.
It creates uniquely named test databases/roles, tests legacy data migration,
provisioning/retries, missing database handling, authentication, application isolation,
and PostgreSQL connection privileges, then removes only its own databases/roles.
Run against a development PostgreSQL server with provisioning access.

## White-label gym domains

Powerhouse is the platform provider; each gym uses its own independent domain,
name, logo, and color. Register a gym domain from its platform detail page, verify
its DNS TXT challenge, and configure DNS/HTTPS at your ingress. Unknown or
unverified domains never resolve to a gym. Gym hostnames have no platform routes.
Platform hosts are configured with `PLATFORM_HOSTS`, replacing DJANGO_ALLOWED_HOSTS.

For local Windows testing use `ironfit.test` in your hosts file and the DEBUG-only
`verify_gym_domain ironfit.test --local` command. See the complete
[custom-domain onboarding and Windows guide](docs/custom-domains.md).

## Reusable websites and gym owner accounts

Powerhouse creates the gym and owner account in one onboarding form. Owners get a
dedicated dashboard on their own domain and can customize the shared website
through a tenant-local settings form. Two design presets are included. New gyms
start from common defaults; each gym's edits stay in its own database. See the
[owner onboarding and website guide](docs/owner-websites.md).

When upgrading, run both `manage.py migrate` and `manage.py migrate_gym_databases`
before starting Django so existing gyms receive their website settings.
