# Central COPAY collections and gym withdrawals

Powerhouse can collect gym membership deposits through one central COPAY merchant,
while tracking an independent liability ledger per gym. This is not a separate
COPAY wallet for each gym, bank-account custody integration, or automated payout
API. COPAY must confirm this business model before production. The default is
sandbox; production checkout requires explicit operator approval configuration.
Do not use that switch as a substitute for COPAY confirmation.

Powerhouse software subscription invoices/revenue remain separate and unchanged.
The unfinished B2B checkout work was paused; it is not included in this release.
Direct per-gym merchant mode still exists and remains the default.

## Install and configure once on the VPS

Upload the new release while preserving `.env` and existing keys. Apply central
migrations and update each existing gym's payment schema using **Retry setup** on
its platform detail page. This preserves member records and website edits. Newly
created gyms receive the schema automatically. Central mode must not be enabled
until active gyms have been upgraded.

```bash
cd /home/megapaldo/apps/gym_platform
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput
sudo cp -R staticfiles/. /var/www/powerhouse/static/
install -d -m 700 secrets
(umask 077; cp -n deploy/central-copay.example.json central-copay.secrets.json)
nano central-copay.secrets.json
```

Fill the central sandbox merchant code, COPAY AES key and key-file paths. Save the
merchant RSA private key and COPAY public key in those paths. Formats and protocol
requirements are the same as [the existing COPAY adapter](copay.md): RSA >=2048,
private PKCS#8, public SubjectPublicKeyInfo. Existing registered 1024-bit RSA keys are supported only with `CENTRAL_COPAY_ALLOW_LEGACY_1024=true` in `.env`. This option applies to both central merchant and COPAY public keys; the default and direct gym merchant minimum remain 2048 bits. It does not disable signature verification, alter the production approval setting, or initiate any payment. Restart the web and worker services after changing it. Register your merchant public key and
server outbound IP (`162.0.225.31`, confirm actual egress) with COPAY. Never send
private keys through chat or commit them. Store backups encrypted and restrict
files to the application service account. PEMs, secrets directory and the central
JSON filename are ignored by Git. Confirm the chosen PHP method with COPAY.

If you do not already have a merchant RSA keypair, generate one on the VPS once
and register only the public key with COPAY (do not overwrite an existing key):

```bash
if [ ! -e secrets/central-merchant-private.pem ]; then
    (umask 077; openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out secrets/central-merchant-private.pem)
fi
openssl pkey -in secrets/central-merchant-private.pem -pubout -out secrets/central-merchant-public.pem
```

The COPAY public key is a different key supplied by COPAY; do not substitute your
merchant public key for it. Keep both private and AES credentials off the public site.

The central origin must be a HTTPS host in `PLATFORM_HOSTS`, e.g.
`https://megapaldo.com`. The callback is:

```
https://megapaldo.com/collections/copay/notify/
```

Set the environment while preserving other values:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from dotenv import set_key
values = {
    'CENTRAL_COLLECTIONS_ENABLED': 'true',
    'CENTRAL_COPAY_CONFIG_FILE': str(Path('central-copay.secrets.json').resolve()),
    'CENTRAL_COLLECTIONS_PRODUCTION_APPROVED': 'false',
    'COPAY_TRUST_LOOPBACK_PROXY': 'true',
}
for name, value in values.items():
    set_key('.env', name, value)
print('Central sandbox collections configured.')
PY
chmod 600 central-copay.secrets.json
find secrets -type f -exec chmod 600 {} +
sudo systemctl restart powerhouse-gym
sudo systemctl restart powerhouse-worker powerhouse-scheduler
```

The proxy IP switch above applies only to this VPS layout: Gunicorn binds `127.0.0.1:8002`, and Nginx overwrites `X-Real-IP` with `$remote_addr`. The app accepts that single IP only from loopback peers when explicitly enabled; it never trusts a browser-supplied X-Forwarded-For chain. Keep the switch false on other deployments unless equivalent proxy restrictions are established.

Use the dedicated Powerhouse worker/scheduler from [VPS domain hosting setup](domain-hosting.md)
if not already installed. Do not start duplicate beat processes. The central tenant
sync recovery runs every minute; it performs no payment submissions or payouts.
The public callback and manual Recheck can also recover sync without Celery.

Do not silently replace an existing direct merchant with the central account for
outstanding orders. Orders snapshot their collection mode, merchant and environment.
Central accounts may not reuse a direct gym's merchant/environment configuration.
Before switching modes, reconcile direct orders and use new dedicated sandbox
member/term records. Existing central orders can reconcile with central mode turned
off if their original central config is retained; direct orders require direct
mode restored. Configuration changes do not migrate paid direct history into the
ledger. Direct and central configurations are not interchangeable.

## Owner flow

1. Owner enrolls a member and assigns a pending membership as before.
2. Owner creates COPAY payment; member/owner opens authenticated checkout.
3. SubmitDeposit uses the central merchant and platform callback. The return page
   remains the gym's independent domain. Browser parameters never settle payments.
4. Signature-verified callback plus authenticated GetDepositDetail records a net
   credit in that gym/environment's **pending** balance. Provider reference,
   merchant reference, quote, method and currency are checked. Net over quote is
   rejected for review; positive lower net is credited as received, but membership
   activation still requires the existing full-net quote rule. Fees may therefore
   require owner review rather than automatic membership access.
5. Owners see production balances on `/manage/`, and full test/production ledger,
   verified payout destinations and withdrawal history at `/manage/wallet/`.
6. Owner submits a destination. Platform verifies account ownership outside the
   app and records evidence; the owner cannot self-verify or edit an already-used
   destination. A changed destination is a new record requiring verification.
7. Once funds are available, owner requests withdrawal. Funds move atomically to
   **reserved**. Duplicate submissions reuse a request key; concurrent requests
   cannot overdraw. Requested withdrawals can be cancelled, returning the reserve.

## Platform flow

At `https://megapaldo.com/collections/`, a platform superuser:

- Reviews each gym's balances, confirmed collections and ledger history, separate
  from software subscription revenue.
- Verifies payout destinations and records ownership evidence.
- Confirms actual merchant settlement/available liquidity externally, then releases
  a collection's net amount from pending to available with a settlement reference.
  A successful deposit alone does not automatically make funds withdrawable.
- Approves a withdrawal, then marks it processing before doing the external payout.
- Performs the supported payout manually using the central merchant dashboard or
  settlement bank account, as agreed with COPAY. This app makes no transfer API call.
- After verifying success, records the unique external payout reference. Reserved
  funds are debited once. Replays cannot pay twice. Uncertain transfers must stay
  processing; they cannot be cancelled/rejected into a second available balance.
- May reject a requested/approved withdrawal and release its reservation.
- Records verified fees/refunds/corrections as signed pending/available adjustments
  with a unique reference and evidence. This only updates the ledger: it does not
  refund through COPAY or change member access. Adjustments cannot spend reserved
  funds or make balances negative. Refund/chargeback deficits after prior payout
  require external recovery and operator reconciliation; this version does not
  implement debt balances, provider refunds, disputes, tax reporting or splits.

Sandbox and production balances never mix. Sandbox release/payout records simulate
this flow and must not be accompanied by real transfers. Production withdrawal
processing is blocked until `CENTRAL_COLLECTIONS_PRODUCTION_APPROVED=true` is set.
No percentage commission is imposed automatically. Gross/net are displayed for
collections; extra fees/commission require agreed terms and explicit adjustments.

## Isolation and failure recovery

Financial control records live in the central PostgreSQL database. Member profiles
and membership payment history remain in each gym's database. Central records store
an immutable gym/order mapping and only the settlement fields necessary for
reconciliation, not member names, cards or payment credentials. An untrusted callback
cannot post a gym selector or select another tenant database.

A central credit commits independently of the tenant update. If the gym DB is down,
the callback returns a retryable failure while the durable credit/snapshot remains.
A repeat callback, Recheck, or scheduled tenant-sync recovery applies the same
verified result to the correct tenant. Unique ledger keys prevent double credits.
The reconciliation snapshot is not an alternative to signature/API verification:
only the authenticated result handler writes it. Order locks preserve existing
membership eligibility checks and explicit SaaS suspensions.

Balances are accounting records of what Powerhouse owes a gym, not proof of funds
actually held at COPAY. Release references are recorded assertions by the reviewing
operator; no settlement/bank reconciliation API exists in the supplied guide.
Monitor the central merchant against all gym liabilities and outstanding payouts.
Back up the central database along with tenant databases and the stable tenant
secret. Do not modify ledger rows or cached balances directly.

Tests use isolated PostgreSQL databases, generated keys and mocked COPAY calls.
They exercise concurrent withdrawals, reservations, idempotency, isolation,
callback signatures, tenant outages, sandbox access, short payments and audit flows.
No real deposit, bank payout or refund is made by tests. Before production, complete
the full sandbox round trip with issued COPAY credentials and confirm their approval.
