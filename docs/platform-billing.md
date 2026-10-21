# Powerhouse platform subscriptions and billing

This is B2B billing: a gym operator pays Powerhouse for software services. All
plans, subscriptions, invoices, payment records, and billing events live only in
the central database. Member-to-gym COPAY orders and membership prices remain in
each gym's own database and never settle these invoices.

## Release setup

Apply central migrations once as the application release operator:

```sh
python manage.py migrate --noinput
```

Restart Django. No per-gym database scripts are needed for SaaS billing. The router
skips platform business tables when gym databases are migrated. Existing gyms stay
**Unmanaged** with their existing modules enabled until Powerhouse explicitly
assigns a subscription. This release does not silently enroll, charge, or suspend
legacy gyms.

Optionally set `PLATFORM_BILLING_INSTRUCTIONS` in `.env` to Powerhouse's payment
instructions and restart. Only authenticated gym owners see these instructions on
their software invoice pages. Do not enter another gym's merchant credentials here.

## Platform operator workflow

1. Sign in as a platform superuser on the platform host. Open `/billing/` and
   Manage SaaS plans at `/billing/plans/`.
2. Create a monthly or yearly PHP software plan with a price and the modules it
   includes: website, member management, attendance/front desk, gym payments, and
   optional POS/inventory.
   No sample plans/prices are inserted into real data. Archiving a plan prevents
   new assignment without cancelling existing contracts.
3. Open a gym's platform detail → Manage platform subscription. Assign a plan,
   choose 0–90 trial days and 0–30 grace days. The subscription starts today; its
   first invoice is issued immediately for the term after the trial. With a zero-day
   trial, paid coverage is required immediately. New-gym onboarding also includes
   optional plan/trial/grace fields in the same form.
4. The gym owner opens `/billing/` on their own domain, reviews the invoice, and
   follows the configured instructions to pay Powerhouse externally.
5. Verify receipt independently, open the invoice on the platform, and record the
   full payment with a method, globally unique reference, and verification note.
   The amount always comes from the invoice. The invoice becomes paid, coverage is
   evaluated immediately, and the next term's invoice is issued automatically.
6. If the quoted amount is zero, confirm it using the explicit No charge method
   and a unique reference/note. Free terms are still confirmed and audited; they
   are not fabricated incoming bank transfers.

Recorded SaaS receipts and invoices due now appear on the billing dashboard.
These totals use only central platform payment records, not tenant member/POS
revenue. Payments are acknowledgments of operator-verified receipts, not automatic
bank verification, card debits, or accounting revenue recognition.

Only one issued invoice exists per subscription. Repeated confirmation of the
same invoice/reference cannot duplicate a receipt or issue another next invoice.
A reference already used on another invoice is rejected case-insensitively.
Partial payments, refunds, credits, taxes, discounts, and payment-proof uploads are
not implemented. The printable service invoice/acknowledgment is not a BIR official
tax invoice. No automatic email delivery or external financial messages are sent.

## Coverage, renewal, and grace

Term starts are inclusive and ends exclusive. Trial coverage ends at the start of
`trial_ends_on`. A paid future invoice does not grant earlier uncovered days or
change the current term's included modules. Monthly/yearly renewals retain the
original day anchor, clamping in shorter months and recovering it when possible.
For example, a January 31 monthly anchor renews February 28, then March 31.

After a paid term ends, its configured grace days retain that term's modules. A
trial alone has no paid-term grace. When coverage/grace ends, operational access
is blocked by date on each request; no scheduler, per-gym script, or worker is
required. Existing sessions are subject to the same check.

Invoices do not initiate automatic debits. An unpaid invoice remains the next
term due; paying it issues the following term. A long-overdue payment can cover
an already ended period, so it may not restore today's access until the subsequent
outstanding periods are covered. The system does not silently waive arrears or
rebase dates. Agree on catch-up arrangements with Powerhouse before recording
payments; credit/rebase workflows are outside this release.

## Plan changes and operator controls

Catalog edits do not reprice existing subscriptions or invoices. To change an
existing agreement, select Change next unpaid term and enter a reason. The old
issued invoice is retained as void and a replacement is issued using the selected
plan's current price, interval, and modules. Paid invoices and original trial
modules remain immutable. There is no proration; the change applies to the next
unpaid term, which may already be due. Never pay an old void invoice.

Suspend blocks operations immediately without deleting gym data or paid history.
Remove suspension restores operations only if trial/paid/grace coverage is valid;
it never waives unpaid invoices. Recording payment does not remove an explicit
suspension. Cancel ends this agreement immediately, preserves paid history and gym
data, and voids the outstanding invoice. Cancellation is final for this agreement;
restarting a cancelled agreement is not exposed by this release.

## Tenant access while restricted

The central middleware runs before operational views and checks module entitlements
on direct GET/POST requests, not just menu visibility. A disabled module cannot be
reached by posting its URL directly. The public Join flow also requires the member
management module. Staff/member authorization still applies after billing checks.

During expiry/suspension/cancellation:

- Public website GET/HEAD pages remain available if the website module was included;
  new membership applications and contact submissions are blocked.
- Owner sign-in/logout and `/billing/` remain available. Owner billing is read-only,
  scoped to the request's gym domain, and does not require the tenant database.
- Existing member payment detail/receipt/return/status-recheck routes and signed
  COPAY notifications remain available so in-flight B2C payments can reconcile.
  New checkouts and payment creation remain blocked. A confirmed member payment
  never restores SaaS access or pays a Powerhouse invoice.
- Other operational screens and mutations are blocked with a generic 503 response.
  Owners receive a link to their software billing page. Members/staff do not see
  platform invoices, payment instructions, operator notes, or other gyms' billing.

A disabled website module also blocks the public website. Unknown domain checks,
verified domain binding, tenant authentication, separate databases, CSRF, and owner
permissions remain enforced. A central billing outage fails closed for operations;
recovery endpoints still keep their normal authorization checks.

## URLs

| Host | URL | Access |
|---|---|---|
| Platform | `/billing/` | Superuser: invoices and recorded receipts |
| Platform | `/billing/plans/` | Superuser: catalog management |
| Platform | `/gyms/<slug>/subscription/` | Superuser: assign/manage gym agreement |
| Platform | `/billing/invoices/<uuid>/` | Superuser: invoice and payment recording |
| Gym domain | `/billing/` | That gym's active owner: own subscription/invoices |
| Gym domain | `/billing/invoices/<uuid>/` | That gym's active owner: own invoice only |

SaaS plan assignment, state changes, plan changes, and receipt recording require
CSRF-protected POST. Every agreement/payment action records the platform operator.
Gym owners cannot change SaaS prices, modules, invoice status, or payment records.

## Validation and remaining integrations

The PostgreSQL integration suite exercises onboarding, quote snapshots, trial and
exclusive expiry/grace dates, invoice renewal, future entitlements, module gating,
CSRF/role checks, duplicate and concurrent payments, tenant billing isolation,
recovery during restricted service, and central-only physical tables. Calendar
checks cover month ends and leap years. Browser checks use unsaved fixtures.

This release provides the SaaS billing ledger and access controls. Automated B2B
payment collection needs a separately configured Powerhouse merchant account and
provider integration. Existing tenant COPAY credentials are intentionally not reused.
The provided COPAY guide has no recurring debit/tokenization API, so stored-card
recurring billing is not claimed. Scheduling/email workers and official tax invoice
integration also remain future work.

POS is a separate module, default off on managed plans. Existing invoice/trial
snapshots without a `pos` key stay unchanged. Branch/staff setup can be used when
attendance or POS is included. Existing POS receipt reads remain available during
restriction for authorized owners/original cashiers, like member payment receipts.
