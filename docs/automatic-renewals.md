# Automatic membership renewal invoices

COPAY supports one-time payments. Powerhouse automates the invoice and reminder
schedule; the member still approves every payment. This feature never stores a
card, submits checkout from a background job, or grants unpaid entry grace.
Payments go to the **gym's own COPAY merchant account**. Platform SaaS billing is
separate. Optional legacy central collections are not used by this feature.

## Owner flow

1. Configure the gym's direct merchant integration using [copay.md](copay.md).
   The platform operator must use `CENTRAL_COLLECTIONS_ENABLED=false` and provide
   separate credentials for each gym. Existing central payment records remain
   intact; reconcile them before changing merchant modes.
2. Open **Membership → Renewal invoicing** at
   `https://YOUR-GYM-DOMAIN/manage/automatic-renewals/`.
3. Enable invoicing, choose 1–30 days before expiry (default 7), and choose a late
   payment policy. **Restart from confirmed payment date** preserves a full term
   for a late payer. **Keep original billing cycle** retains the original dates,
   so elapsed days are not restored. The policy is displayed before consent and
   again on the payment page.
4. Ask members to enable their own renewal preference. Owners cannot opt in on a
   member's behalf. An initial active membership must already exist.
5. Review invoices, member consent status, setup messages and the last worker
   check on the same page. Open a payment to inspect or recheck COPAY status.
   Cancellation requires a reason and is allowed only before a checkout attempt.

Changing plan name, price, period, availability or late policy pauses **future**
invoices until the member reconfirms. Already-issued invoices keep their original
price and agreed policy. Disabling the feature stops generation/reminders and
prevents new checkouts for these invoices. An already submitted payment can still
be reconciled and activated if eligible; disabling invoicing does not reverse it.

## Member flow

1. Sign in on the gym's own domain and open **My gym → Renewal invoices** at
   `/portal/automatic-renewals/`.
2. Select a plan and quoted price, review the gym's late policy, and tick the
   consent checkbox. Consent authorizes invoices, **not automatic charges**.
3. Before expiry, the worker prepares one pending next term, renewal invoice and
   payment order. No provider request happens at this stage. The invoice appears
   in the portal even when email is disabled. Existing frozen memberships,
   pending terms or manual renewal requests pause generation for owner review.
4. Open the invoice's payment link and approve the one-time COPAY checkout.
   If the outcome is unknown, use **Recheck payment status**, not a new payment.
5. A verified production payment for the full quoted net amount activates the
   next eligible term. Early payment schedules it immediately after the existing
   term; late payment follows the agreed policy. Conflicting terms, partial net
   amounts or changed eligibility require owner review. Sandbox payments never
   grant access. Repeated confirmations never activate twice.
6. Use **Stop future invoices** to opt out. Unsubmitted pending invoices are
   cancelled. Submitted payments remain available for reconciliation. Paid
   memberships and payment history remain intact. A cancelled invoice is not
   regenerated for the same cycle; request a manual renewal or contact the gym.

The existing **Renew membership** request and owner approval flow remains
available. An unpaid auto invoice can block another pending renewal: cancel it
safely first. Attendance continues to require an eligible paid/active term; this
feature adds no overdue grace period.

## Scheduling and reminders

The existing Celery worker and single beat scheduler process each registered gym
in its own database. Memberships and payments SaaS modules must both be allowed.
Concurrent/repeated workers serialize by member and produce one invoice per
preceding term. Missing merchant configuration rolls back the entire invoice
attempt and displays a generic owner setup message.

With gym email enabled, an invoice notice is queued when created, then reminders
at 7, 1 and 0 days before expiry (one message when creation coincides with a
reminder day). These reuse **Membership reminders** and members' optional email
preferences. Consent-change notices use **Membership updates**. Delivery rechecks
current consent, invoice/payment state, gym policy, role, domain and SaaS access.
Opt-out or settlement suppresses obsolete queued reminders. SMTP remains at least
once; a crash after SMTP acceptance can cause a repeat. See
[notifications.md](notifications.md) for transport and worker setup.

## Upgrade an existing installation

During a planned upgrade window, back up platform and tenant databases, pull the
release and run the standard central `manage.py migrate`. Restart
`powerhouse-gym` to load the new code. In the platform administration page, open
each gym and click **Retry setup** to apply tenant migration `0008`; newly created
gyms install it automatically. Tenant payment pages may return unavailable until
this upgrade finishes: invoice status hooks need the new tables. After upgrading
each gym, restart the existing `powerhouse-worker` and `powerhouse-scheduler`
(use your actual configured worker names), then verify the gym pages before
resuming payment traffic. Do not start duplicate schedulers. No per-gym or
per-member scripts are needed for ongoing invoices.

This release does not resolve gateway HTTP 403 responses, prove production
merchant authorization, add automatic bank debits, or enable platform SaaS
subscription collection. Verify each gym's credentials and provider connectivity
before collecting live payments.
