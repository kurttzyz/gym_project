# Gym retail POS and branch inventory

This milestone adds physical retail products, branch stock, cash/externally verified
bank sales, receipts, and full returns. All POS business tables belong to each gym's
own PostgreSQL database. Retail receipts, gym-member COPAY payments, and Powerhouse
SaaS invoices are three separate ledgers; none settles or overwrites another.

## Release setup and modules

Pull the release, apply central migrations once with `python manage.py migrate`,
and restart Django. Existing gyms can select **Retry setup** on their platform
detail page to install the POS tables without per-gym scripts. New gyms receive
them during ordinary onboarding. Collect/serve updated static files in production.

Unmanaged gyms have POS enabled alongside their existing modules. Managed SaaS plans
have a new **POS and inventory** checkbox, default off. Existing trial and paid
invoice snapshots are preserved: missing `pos` means not included. Powerhouse can
include POS in a plan and explicitly change the gym's next unpaid term; there is
no retroactive paid-term upgrade or proration. POS access follows the currently
covered term's modules, not a future invoice's entitlements.

Owner branch/staff setup is shared by attendance and POS, so it remains available
when either module is included. With POS-only access, staff sign-in lands on `/pos/`.

## Owner setup through the website

1. On the gym's domain, open `/manage/branches/` and add an active branch if needed.
2. Open `/manage/products/`, create a product, and enter its unique SKU, name, PHP
   selling price, and low-stock threshold. SKUs are normalized to uppercase and
   unique case-insensitively within this gym. No example inventory or prices are
   seeded in actual gyms. Hide discontinued products rather than deleting history.
3. Open `/manage/inventory/adjust/`. Choose the branch and product, enter a positive
   receiving quantity, and provide the supplier/reference/reason. Negative deltas
   record removals/corrections. Zero deltas and negative resulting balances are
   rejected. Stock at another branch never covers a sale here.
4. Review `/manage/inventory/` by branch, optionally selecting low stock only.
   Products without a balance have zero units. Recent movements show the delta,
   resulting balance, timestamp, reason, and operator. Disabled branches/products
   remain available for owner stock reconciliation.
5. Use `/manage/staff/` to create staff/manager accounts. Both roles can make POS
   sales and see their own receipts, without pricing, stock-adjustment, report,
   or refund permissions. Owners can see all retail receipts in their gym.

## Cashier workflow

1. Open `/pos/`, choose an active branch, and select products/whole-unit quantities.
   There are five rows initially; Add another product row supports up to twenty.
   Blank rows are ignored. Duplicate product selections are merged, with at most
   999 units per product. This release supports physical retail items only.
2. Select Review cart and payment. The server quotes current prices and checks
   available stock. The review shows each item and the PHP total; it does not
   reserve stock or record a sale.
3. For cash, enter the actual cash received. The server calculates change and
   rejects insufficient tender. A zero-total giveaway can be recorded with cash
   received zero, with the stock deduction still audited.
4. For bank transfer, verify the incoming transfer independently, enter its unique
   reference and verification note, and confirm receipt. The form records that
   verification; it does not move money, initiate a bank transfer, charge a card,
   or open a COPAY checkout. A bank reference cannot settle two retail sales.
5. Record the sale. Product availability, prices, and branch stock are checked again
   under database locks. The receipt appears only after the sale, lines, stock
   deductions, and ledger movements commit together. If price/name/SKU changes or
   stock runs out, no partial sale is recorded; review a fresh cart before payment.
6. Print the receipt from the browser. Cashiers see their own sales at `/pos/sales/`;
   owners see all gym retail sales there.

Quotes are signed, gym/cashier-bound, and valid for ten minutes for a new sale. An
exact retry of a recorded checkout returns its original receipt even after quote
expiry; changed payment details are rejected. Confirmation uses POST/redirect/GET.
Reviewing a new cart creates a new checkout intent, so check sales history before
starting over after an uncertain response. A receipt is not a public bearer link:
viewing/reprinting requires an active owner or the original cashier's gym access.

Receipts snapshot product SKU/name/price, quantities, gym brand, branch name/address,
payment details, operator, and date. Later catalog/branding edits do not rewrite
these fields. These are payment acknowledgments, not BIR official tax invoices.
There is no tax calculation, discounting, split tender, customer debt, stock cost
valuation, profit calculation, barcode lookup, or electronic receipt delivery yet.

## Full refunds and stock returns

Only an owner can open a receipt and select Record full refund. Verify that the
money was returned externally, supply a unique refund/voucher reference and reason,
and confirm the full amount. The amount is copied from the original sale; a posted
amount cannot reduce or increase it. The original payment method remains on record.

Choose Return all items to stock only if all items were received in resellable
condition. If unchecked, the full financial refund is recorded without adding units
(e.g. damaged goods). Full returns can restock inactive products or disabled branches.
A sale has at most one full refund. Repeating the same request cannot add stock twice;
changed refund details cannot rewrite a completed refund. Refund and stock return
updates commit atomically, and the original receipt remains with its refund annotation.
Partial returns, exchanges, refund cancellation, and provider-issued refunds are
outside this milestone.

## Reports and service restrictions

Owners use `/manage/sales/report/` to filter by local date and optional branch.
Gross retail receipts are counted on sale dates; refunds on their recording dates.
Net recorded retail receipts can therefore be negative when an older sale is refunded
today. This report is not tax/profit accounting or bank reconciliation. Dates use
`APP_TIME_ZONE`. Member subscription charges and platform SaaS receipts are excluded.

A missing POS entitlement or expired/suspended SaaS agreement blocks new POS and
inventory operations, including direct POSTs. Existing receipt reads remain available
for reconciliation, with owner/original-cashier authorization still required. Refund
recording and stock adjustments require an active POS module. Revoked employees
lose access on their next request; members/public visitors never gain POS access.
A platform superuser alone is not a gym owner/cashier.

## Routes

| Gym-domain URL | Access |
|---|---|
| `/pos/` | Owner/staff/manager: review a cart |
| `/pos/confirm/` | Same roles: CSRF-protected POST to record a paid sale |
| `/pos/sales/` | Owner: all sales; staff/manager: own sales only |
| `/pos/sales/<uuid>/` | Owner/original cashier: view/print receipt |
| `/manage/products/` | Owner: catalog and price management |
| `/manage/inventory/` | Owner: branch stock and recent movements |
| `/manage/inventory/adjust/` | Owner: audited receiving/removal adjustments |
| `/manage/sales/<uuid>/refund/` | Owner: verified full refund/optional restock |
| `/manage/sales/report/` | Owner: daily retail receipts/refunds |

## Transaction boundaries and validation

All inventory writers lock the branch before products (in a stable UUID order).
Checkout and refunds share that order, and stock balances cannot go negative.
Each stock adjustment has a request UUID/fingerprint; each sale has one signed
checkout UUID/fingerprint. Unique constraints protect bank references, refund
references, branch balances, sale lines, and sale/refund stock movements. No central
identity foreign keys are placed in gym databases; operator IDs are scalar references.

The PostgreSQL integration suite covers multi-item rollback, concurrent last-unit
sales, concurrent retries, stale/expired/foreign quotes, exact Decimal money/change,
branch stock isolation, immutable receipt snapshots, cashier permissions, bank
reference reuse, refunds/restock idempotency, report dates, module gating, physical
database isolation, and UI-driven upgrades. Browser checks use unsaved fixtures for
phone/desktop layouts, cart-row controls, and payment-field behavior. No real sales,
stock, bank transfers, or refunds are seeded or executed by those checks.
