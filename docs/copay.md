# COPAY+ gym membership payments

Implemented against the supplied **COPAY+ Merchant Integration Guide V1.0.16**:
SubmitDeposit, GetDepositDetail, encrypted/signed deposit notifications, cashier
return, and payment acknowledgment. This handles member-to-gym payments only.
Powerhouse SaaS billing, payouts, refunds, split settlements, stored cards, and
automatic recurring debits are not part of this integration. The supplied guide
does not document a recurring debit/tokenization API.

## Configure each gym once

1. Update the application and install `requirements.txt`. Restart Django. On an
   existing gym's platform detail page, select **Retry setup** to install the
   payment tables. New gyms receive them automatically during onboarding.
2. Copy `deploy/copay.example.json` to `copay.secrets.json` outside public/static
   directories. It is ignored by Git. Replace the outer placeholder with the
   **Payment integration identifier** shown on the platform's gym detail page.
3. Fill that gym's merchant code and COPAY-issued AES key. Save the merchant's RSA
   private key and COPAY's RSA public key as the referenced files. File paths are
   relative to the JSON file, or absolute. PEM and base64 DER are accepted;
   RSA keys must be at least 2048 bits. Merchant private keys use unencrypted
   PKCS#8 PEM/DER; public keys use SubjectPublicKeyInfo PEM/DER. Keep all files
   readable only by the application operator/service account.
4. Set `public_origin` to this gym's independently owned, active, verified HTTPS
   domain, without a path or port. It must already be registered to that gym.
   Set its merchant-enabled PHP deposit method: `GCP`, `GCDR0`, `GCDR1`, `GCQR0`,
   `GCQR1`, `GCWU0`, `GCWU1`, or `MYP`. Confirm availability with COPAY.
5. In `.env`, set `COPAY_CONFIG_FILE` to the absolute JSON file path. For Windows,
   forward slashes work, for example `C:/gym_platform/copay.secrets.json`. Restart
   Django after changing `.env`. The JSON/key files are read when used, so filling
   them does not require per-gym scripts or a database edit.
6. For another gym, add another outer UUID entry with its own merchant account,
   AES key, and RSA key files. There is no global fallback merchant. Sharing the
   same merchant code/environment across two gym entries is rejected.

Blank/missing/invalid configuration leaves COPAY disabled for that gym. Ordinary
website and member-profile workflows still work. Owner `/manage/payments/` shows
whether the gateway is configured, without showing secrets.

### COPAY onboarding requirements

The guide requires COPAY to whitelist your server's outgoing public IP and register
your merchant RSA public key. Give COPAY only the public key; keep the private key
on your server. Obtain their public key and AES key through their merchant setup.
Your approved merchant account must support collecting payments for your gym.

The server selects these endpoints; they cannot be overridden by checkout input:

| Environment | API origin |
|---|---|
| `sandbox` | `https://mapi.marvelpay.net` |
| `production` | `https://mapi.copay.plus` |

Configure/allow the generated per-gym callback URL with COPAY if their onboarding
requires it. Localhost is not externally reachable. Use a deployed HTTPS gym domain
for sandbox callbacks; adding keys alone cannot make your PC reachable to COPAY.
The guide lists callback source IPs `52.68.7.167` (test) and `18.182.65.124`
(production); confirm current values before adding network ingress rules. The app
verifies cryptographic signatures and does not trust spoofable forwarded-IP headers.
Behind a proxy, arrange trusted client IP handling at the server layer so REMOTE_ADDR
is meaningful; the application never accepts a posted client IP.

Cashier URLs must be HTTPS on the selected environment's documented domain
(`marvelpay.net` or `copay.plus`, including subdomains). If COPAY explicitly assigns
a different cashier hostname, put that exact hostname in `cashier_hosts`. No wildcard,
arbitrary redirect, or cross-environment default is allowed.

## Using it without scripts

1. The owner enrolls a member and assigns a pending, unexpired membership term.
2. On the member detail page, select **Create COPAY payment**. The amount comes
   from the term's immutable quote, never a browser-supplied amount. Zero-priced
   terms use the existing manual activation workflow rather than a deposit.
3. The owner can open checkout, or the invited member can sign in at `/portal/`
   and select View payment → Pay securely with COPAY. Payment URLs require that
   member's login or the gym's owner; they are not public bearer payment links.
4. COPAY returns the member to the payment page. Browser return/query parameters
   never mark a payment paid. A verified server notification triggers requery,
   or the member/owner can select **Recheck payment status**.
5. A confirmed **production** deposit with status `6` records the received net amount and
   marks the order paid. If the net amount equals the quote, it
   activates its pending membership if still eligible. A differing net amount (including
   deductions or overpayment) is retained for owner review without automatic access. Expired, cancelled,
   overlapping, disabled, or already active terms keep a paid record with an
   explicit owner-review note. Payment success never overrides access rules.
6. A confirmed **sandbox** deposit is marked as test-paid, has a visibly marked
   acknowledgment, and does **not** activate membership access.

The owner sees all tenant payments at `/manage/payments/`. Members see only their
own orders and paid acknowledgments. Acknowledgments are printable browser pages,
not BIR official receipts/tax invoices. There is no automated email delivery.

| Gym-domain URL | Purpose |
|---|---|
| `/manage/payments/` | Owner list and configuration status |
| `/payments/<uuid>/` | Owner/member payment detail |
| `/payments/<uuid>/checkout/` | CSRF-protected POST to open checkout |
| `/payments/<uuid>/recheck/` | CSRF-protected POST to reconcile |
| `/payments/<uuid>/return/` | Browser return; never settles a payment |
| `/payments/<uuid>/receipt/` | Paid acknowledgment with access controls |
| `/payments/copay/notify/` | Signed JSON callback; no login or CSRF token |

## Reconciliation and safety

Each term has one payment order and a server-generated merchant reference. The
attempt is persisted before sending SubmitDeposit. Repeated clicks and timeouts
never resubmit it. An unknown outcome stays Checking with COPAY until a notification
or manual recheck resolves it. Do not switch the merchant configuration while
that outcome is unresolved. If COPAY confirms no transaction was created, an
operator must investigate before arranging a replacement term/payment; this release
does not guess or automatically retry submission. A failed payment is retained;
contact the gym before starting a replacement. No scheduled polling worker runs. Use dedicated sandbox member/term records; a
sandbox-paid term retains its test payment and cannot be reused for a live deposit.

The guide's status IDs are deposit `6` success, `7` fail, and other integers processing.
Payment status updates lock the member, term, and order. Settlement is idempotent;
replayed notifications cannot activate twice or downgrade a paid order. Late success
can reconcile a previously failed order. Amount, merchant reference, payment method,
merchant configuration, and provider reference must match. The requery API omits
currency in its documented result, so currency is bound to the authenticated request
and immutable order; a returned currency, when present, must match too.

AES uses the guide's PBKDF2-HMAC-SHA1 (1000 rounds, 32-byte key, AES-key text as salt),
AES-256-CBC with random IV prepended, and PKCS#7 padding. RSA uses PKCS#1 v1.5/SHA1
as required by this protocol. Signatures cover the exact plaintext JSON bytes;
decimal amounts are sent as JSON numbers. Notifications verify COPAY's signature
before business processing. Outer TransactionId and AdditionalInformationData are
not treated as trusted settlement evidence; authenticated GetDepositDetail binds
the provider reference and current status. Only a reconciled notification receives
the literal `success` acknowledgment. A final callback whose requery still shows
processing is left unacknowledged so COPAY can retry; a success callback cannot
be acknowledged while requery reports failure.

Merchant codes/environments are snapshotted per order. Changing them with outstanding
orders fails closed and requires restoring that configuration to reconcile; coordinate
key rotation and merchant migrations. Secret files are not rendered or logged.
Production transport uses certificate-verified HTTPS, a fixed API origin, a timeout,
response size limit, and no automatic HTTP redirects/retries.

## Validation and going live

Automated tests use generated test keys and mocked provider HTTP calls; none create
real COPAY payments. They cover an independent OpenSSL AES vector, RSA byte fidelity,
malformed messages, checkout allowlists, settlement idempotency, timeouts, receipt
permissions, amount/method mismatches, tenant isolation, and UI-driven schema upgrades.

Before production, complete a COPAY sandbox checkout/notification/requery round trip
with the issued credentials and registered domain, verify the merchant's payment
methods/limits and cashier response, then switch that gym to separately issued
production credentials. No live or sandbox provider certification has been performed
without those credentials.

## Optional central collection model

The direct merchant workflow above remains the default. The platform now also
supports [central member collections with per-gym ledgers and manual withdrawals](central-collections.md).
This mode uses a separate operator configuration and platform callback, keeps gym
liabilities separate from SaaS revenue, and starts in sandbox pending provider approval.

### Transport diagnostics

Failed HTTP calls now report the upstream HTTP status separately from DNS, TLS,
timeout and connection failures. The application warning log includes only a fixed
operation name, environment, failure category and numeric HTTP status. Provider
response bodies, headers, exception text, credentials and payment payloads are not
logged. Inspect `journalctl -u powerhouse-gym` after **Recheck payment status**.
A reachable unauthenticated GET does not prove that a signed POST is accepted;
HTTP 401/403 may require checking merchant credentials, IP allowlisting or provider
edge rules with COPAY. Do not automatically retry SubmitDeposit for an unknown
outcome. Recheck uses GetDepositDetail and does not submit another payment.


## Owner wallet and direct withdrawals

With `CENTRAL_COLLECTIONS_ENABLED=false`, `/manage/wallet/` shows the gym's own
COPAY wallet access instead of the optional Powerhouse custody ledger. Add
`"merchant_portal_url": "https://YOUR-COPAY-MERCHANT-DASHBOARD"` to that gym's
UUID entry in `COPAY_CONFIG_FILE`, using the actual HTTPS dashboard URL supplied
by COPAY. The **Withdraw in COPAY** button opens that page in a separate tab;
owners sign in with their own merchant account. Leave the field blank until the
real URL is confirmed. Invalid or missing links show setup guidance.

Live balances, payout destinations, transfers and withdrawal status are managed
inside COPAY. This link does not submit a payout API request or share a merchant
login. Direct-mode POSTs cannot create new Powerhouse withdrawal requests or
payout destinations. Historical central ledger/withdrawal records are preserved
for platform reconciliation. The previous manual central workflow remains only
when central collections mode is explicitly enabled.
