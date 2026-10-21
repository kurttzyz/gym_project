# Powerhouse module architecture

Powerhouse sells infrastructure to gym operators. It does not operate the tenant
gyms. One codebase serves all gym domains; each tenant has a dedicated PostgreSQL
database/login. No casino platform code, data, or payment credentials are shared.

```text
platform_config/                 Django settings, platform URLs, ASGI/WSGI
platform_core/                   Central platform control plane
  tenants/                       Registry/provisioning/domain boundary
  subscription_plans/            Planned B2B SaaS plans and module entitlements
  billing/                       Planned gym-to-Powerhouse subscriptions/invoices
  platform_dashboard/            Platform administration boundary
  views.py                       Existing platform dashboard and onboarding
accounts/                        Central identities/authentication (existing)
gyms/                            Central registry, domain/access models (existing)
gym_core/                        Tenant operational application
  members/                       Profiles, emergency contacts, single-use account invitations
  memberships/                   Plans, applications, enrollment, dated terms and lifecycle audit
  attendance/                    Planned check-ins and QR attendance
  staff/                         Planned employee roles and permissions
  trainers/                      Planned trainers
  urls.py, views.py              Existing gym member portal and owner dashboard
tenant_data/                     Existing tenant branch model and migrations
pos/                             Tenant application
  products/                      Planned product catalog
  inventory/                     Planned stock ledger
  sales/                         Planned sales/receipts/refunds
payments/                        Tenant member-to-gym payment application
  gateway_adapters/              Provider-neutral typed contract
  recurring_billing/             Planned recurring collections
  invoices/                      Planned member/POS invoices
  webhooks/                      Planned verified/idempotent callbacks
websites/                        White-label presentation application
  templates/                     Shared public website, owner editor, and portal templates
  branding.py                    Existing request-scoped branding
  public_pages/                  Home, memberships, about, contact, and join pages
  models.py, views.py             Tenant website settings, public site, owner editor
tenants/                         Existing hostname-to-tenant middleware
config/                          Router/admin infrastructure and compatibility imports
```

Domain folders marked Planned are architectural boundaries, not implemented
business features. Billing, POS, recurring payment collection, and attendance
remain planned. Gym-priced plan publishing, membership requests, and private contact
inboxes, member profiles, invitation enrollment, and manually managed membership
terms are implemented. Activation requires an owner action and a reason; it does
not collect or verify payment. Subscription
prices (such as PHP 2,999 SaaS and PHP 1,000 gym membership) are proposed pricing,
not hardcoded application defaults. Concrete payment providers are not configured.
Celery/Redis and notification/billing workers are also planned, not running.

## Database ownership

Central `default` owns accounts, sessions, gym registration, custom domains,
access memberships, and future platform_core business models. Tenant access
memberships authorize users; they are not paid gym memberships.

The router sends `tenant_data`, `gym_core`, `pos`, `payments`, and `websites` to
an explicitly authorized gym database and refuses queries outside a tenant scope.
Their migrations are blocked on `default`. New business models must be defined
or imported through their Django app's models module so migration discovery sees
them. Central/tenant foreign keys are prohibited. Existing model labels/table names are deliberately retained. WebsiteSettings has
its own tenant-only migration and is initialized when each gym database is migrated.
`migrate_gym_databases` applies the full migration graph on each gym connection;
the router creates only tenant app tables there. Migration history rows may include
skipped central apps, but central business tables are never created in gym databases.

The existing accounts/gyms/tenant_data models retain their labels and migrations
so existing installations and gym databases can upgrade without data relocation.
Avoid copying them into new apps or regenerating initial migrations. The previous
`config.settings` and ASGI/WSGI entry points remain compatibility shims; canonical
settings/entry points are now in `platform_config`.

## Two distinct billing systems

**B2B SaaS:** gym operator pays Powerhouse. Plans, entitlements, platform invoices,
SaaS subscription status, and any future suspension decision belong to central
`platform_core`. Only platform operators configure platform prices and modules.

**B2C gym payments:** gym member pays their gym. Gym membership prices, member
invoices, POS payments, collections, refunds, and revenue belong to tenant-local
`gym_core`, `pos`, and `payments`. A gym admin cannot set SaaS platform prices or
read another gym's transactions. Platform and gym revenue reports are separate.

A gateway contract is defined in `payments/gateway_adapters/base.py`; it performs
no external requests. Later COPAY/PayMongo/Xendit adapters must receive scoped
credentials securely. Never reuse a gym merchant credential for SaaS billing or
another gym. Validate payment amounts server-side, verify webhook signatures,
resolve the tenant from trusted merchant metadata, and enforce idempotency before
writing business state. Transaction commission requires provider support and an
explicit commercial agreement; no split payments are assumed.

## Interfaces and roles

Current platform URLs belong to `platform_core` and require a platform superuser.
Current gym portal URLs belong to `gym_core` and require an active gym membership.
`websites` owns shared presentation and per-gym branding. Public websites, member portals, and owner website customization are implemented.
Staff POS and further gym-owner business management remain planned. These should share tenant-local records and services, not
separate code copies. Owner/manager/staff/member role names already exist, but
business module permissions must be explicitly implemented before enabling writes.

All new HTTP handlers, background jobs, exports, cache keys, and file storage paths
must be scoped by immutable gym UUID. Future Celery jobs must resolve a registry
tenant and enter a fresh database context; they must not inherit a request context.
Payment processing and background scheduling must not begin until the relevant
provider credentials, signed webhook flow, retry policies, and services are configured.


Owner account creation and website settings are documented in [owner websites](owner-websites.md).


See [membership website](membership-website.md) for public plans/applications and private owner workflows.
