# Database-per-gym architecture

```text
Platform PostgreSQL database: gym_platform
├── Platform user identities, sessions, and permissions
├── Gym registry: name, slug, immutable database UUID, readiness
└── Memberships: user access and roles per gym

Gym A PostgreSQL database + login role: gym_<uuid-a>
└── Branches (tenant_data.Location)

Gym B PostgreSQL database + login role: gym_<uuid-b>
└── Branches (tenant_data.Location)
```

A gym business is a tenant (`gyms.Organization`); a branch belongs to that gym's
PostgreSQL database. Users can belong to multiple gyms through central access
memberships. This identity and access control plane is intentionally shared;
gym operational data is stored separately. There are no cross-database foreign
keys. All databases use PostgreSQL; local development uses one PostgreSQL 17 server.

## Request boundary

1. Authenticate against the platform database.
2. Resolve the gym through active memberships (or explicit platform superuser access).
3. Resolve its database from its server-generated UUID, never a user-supplied connection string.
4. Enter a context-local database scope and fully evaluate tenant queries there.
5. Reset the scope on success and exceptions.

Unauthorized gyms return 404 before their database is selected. An unavailable
authorized gym returns 503 without falling back to the platform or another gym.
Tenant queries outside a selected scope raise an error. ContextVar scopes prevent
concurrent requests from sharing a mutable global selection. Do not defer tenant
query evaluation until after leaving the scope, including template rendering.

The router only creates `tenant_data` tables in gym databases and central tables
in `default`. Central and tenant rows cannot be joined. Explicit `.using()` calls
can bypass routing and are reserved for trusted provisioning/migration code.
Each gym connects through its own login role and owns only its database. Database
CONNECT privileges are revoked from PUBLIC on the platform and all gym databases.
Integration checks demonstrate that gym credentials cannot connect to other gyms
or the central database. The central application role does not have CREATE ROLE
or CREATE DATABASE privileges. A separate privileged control-plane connection
provisions databases and roles. Database creation uses quoted SQL identifiers and
an advisory lock to serialize creation of a given database.

A compromised platform process can still access provisioning configuration and
the credential derivation secret. Independent databases/roles do not remove that
control-plane trust. Isolating provisioning into a separate worker and restricting
server/network access are future production deployment improvements.

## Provisioning and migrations

Only platform superusers can add gyms through `/gyms/new/`. Creation registers the
gym, migrates its database, then marks it ready. Failures retain a nonready registry
entry so operators can retry with `migrate_gym_databases --slug <slug>`. Existing ready
databases that disappear must be restored, not silently recreated. Slug edits do
not move a database because database names use immutable UUIDs. Central admin cannot
create or delete gyms, bypass provisioning, or expose tenant branch tables.

Run platform migrations first, then `migrate_gym_databases` after every schema
update. Tenant migration records live in each gym database. Database changes across
the platform and gyms are not one atomic transaction; provisioning is retryable.
The initial split migration copies legacy branches, verifies field equality, and
only then removes the shared table. Back up first; reversal is intentionally blocked.
If a migration fails, preserve existing databases and diagnose before retrying; partial
migration attempts may leave orphan databases/roles requiring operator review.

## Future features

Put operational models (plans, paid subscriptions, attendance, classes, payments)
in `tenant_data`. Reference other tenant-local models through ordinary foreign keys.
Keep shared identity/access metadata in central apps. Any link to a platform user
must be a scalar identifier validated through authorized platform services, not a
cross-database foreign key. Tenant role names do not yet grant write capabilities.

Background jobs must resolve a gym and enter a fresh context; do not rely on inherited
request context. Scope future cache keys, file paths, exports, and logs by immutable
gym UUID. These services are not implemented yet. Gym deletion, archival, account
signup, billing, and a branch editing UI remain separate features.


## Credential lifecycle

Gym names and login roles use immutable UUIDs. Their passwords are HMAC-derived
from a stable secret held outside source control. Reusing a database verifies its
owner; it never takes over an arbitrary pre-existing database. Provisioning does
not reset existing login passwords. Preserve the derivation secret and coordinate
any rotation with database role password updates. Provisioning access belongs to
the hosting platform only; normal tenant handlers connect with tenant credentials.

Local Docker credentials are generated once into ignored `.env`; production must
supply securely managed secrets and trusted TLS settings. Backups must include all
databases, roles, and the derivation secret. Docker volume persistence is local
runtime persistence and does not establish fresh cloud-task restoration.
