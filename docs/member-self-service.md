# Member self-service and renewal review

A member signs in on their own gym domain and uses their linked, enabled profile. Self-service never accepts a member ID from the browser to select someone else's records. Central active member access and the tenant-local profile are both required. Owner/manager/staff roles do not gain member self-service simply by being employees or platform superusers.

## Member pages

| Path | Behavior |
| --- | --- |
| `/portal/profile/` | Edit own full name, phone, birth date and paired emergency contacts |
| `/portal/renewals/` | Request an available plan and view/cancel own pending requests |
| `/portal/payments/` | Paginated own membership payments and existing payment/receipt links |
| `/portal/password/` | Change the shared login password after proving the old password |
| `/accounts/password-reset/` | Request a reset link using login username and central account email |

The existing class booking, QR card, email preferences, membership history and attendance pages remain linked from the member portal. Profile updates only save the explicit editable fields and record old/new values and the actor in a tenant-local audit. They cannot change email, account linkage, QR version, member number, waiver evidence, activation, prices or payment status. Contact the owner to correct a gym contact email; changing it does not change the central login recovery address. Birth dates cannot be in the future and emergency contact name/phone must be provided together. Repeated identical edits produce no extra audit rows. Owners can see the latest ten profile update summaries on the member detail page.

## Renewal workflow

1. A member chooses an active plan and a start date. The suggested date is today or the end of the latest active/scheduled term, whichever is later.
2. The server snapshots the current plan name, price, billing period, and exclusive end date. No submitted price/status is trusted, and no money is collected.
3. One pending request is allowed per member. Repeating the same plan/date request returns it. Cancel first to request a different term. Start dates must be today through one year ahead, without an overlapping active term. Frozen memberships and unexpired pending terms require owner attention first.
4. Owners review `/manage/renewals/` and approve or decline with a note. Approval rechecks member eligibility, available plan, dates, overlaps and pending terms, then creates exactly one **pending** membership term at the original snapshot price. Catalog price/name edits do not reprice an existing request. A discontinued plan cannot be approved. If the requested start date has passed, decline and discuss a fresh request; dates are not silently shifted.
5. The owner uses the existing member detail/payment setup workflow to issue a COPAY payment order or verify an offline payment. Approval itself never activates membership or verifies payment. Normal membership activation and gateway validation still apply.
6. The member can see the decision and pending term/payment link. Already reviewed requests cannot be withdrawn; contact the owner to cancel the resulting term. Duplicate approval/cancellation submissions don't create another term or rewrite history.

Short tenant transactions lock the member before the request, using the same member lock as membership assignment/activation. Concurrent requests or approvals cannot create duplicate pending renewals. Renewal requests, profile edits and business audit remain in the gym database. Owners receive optional new-request alerts; members receive optional review decision messages under **Membership updates** in notification settings. Existing email opt-out and SaaS rules apply.

Renewal operations use the memberships SaaS entitlement. Profile, payment history, password change, reset requests and email preferences remain available to authorized users during SaaS suspension for account recovery and existing business records. No self-service email change, automatic charges, recurring debit, refunds, or instant renewal activation is added.

## Password security and shared identities

Login identities live centrally. A password change or reset applies to **every gym using that same login account** and invalidates other session hashes. The normal authenticated password change preserves the current session; reset requires signing in again. Both forms use Django password validation. The old password is rechecked under a central user lock for authenticated changes.

Password recovery is separate from the tenant notification outbox:

- The request requires the exact login username and its **central identity email**. A gym-editable member email is never used as recovery authority or delivery destination. Duplicate email addresses across login accounts remain disambiguated by username.
- Only active, linked member accounts with usable passwords are eligible on that gym. Owners, staff/manager roles, disabled profiles, platform staff/superusers and foreign gym accounts cannot recover through this member endpoint.
- Unknown, ineligible, throttled or unavailable accounts receive the same confirmation redirect and text. A hashed gym/IP bucket permits at most ten requests per ten-minute window; matching accounts have a global five-minute cooldown. Raw requester IPs are not stored. The code uses `REMOTE_ADDR`, not untrusted forwarded headers. Configure a trusted proxy/client-IP boundary appropriately for production; a shared proxy address shares its bucket.
- Central `PasswordRecovery` records contain recipient, gym, verified request origin, password fingerprint, expiry and bounded delivery outcome. They contain **no reset token**. Production recovery links always use HTTPS on the verified request host, even behind a TLS-terminating proxy. The sender generates a gym-bound Django password reset token only in worker memory and emails it to the central address. No usable reset link or token is stored in gym-owner delivery history, tenant databases, or broker messages.
- The link expires 30 minutes after the request, is bound to that request's gym, and is invalidated by password/address changes, account disablement, revoked gym access, domain disablement, superseding request, or successful reset. Django's token also invalidates when its login-state hash changes. A successful reset locks the central user then request and consumes it once.
- On opening the email link, the raw token is moved to the central session and the browser is redirected to a token-free `set-password` URL before rendering the form. Reset responses are private/no-cache, no-referrer, and noindex. Avoid retaining raw reset URLs in production access logs.
- Explicitly requested security recovery emails ignore optional gym/member notification switches and SaaS suspension, but still require the platform's configured/enabled SMTP transport and an active verified gym domain. They have a separate central worker queue and never appear in gym-owner notification history. Ordinary owner notification test emails do not verify account recovery eligibility.
- The existing Celery scheduler dispatches recovery request UUIDs. SMTP runs outside transactions, uses a five-minute claim lease, and retries transient failures up to three times. As with all SMTP, acceptance followed by a crash can lead to another copy. All tokens are subject to the same single-use/expiry checks. No real email is sent in tests.
- Expired requests are marked expired, expired throttle buckets are removed, and security delivery records older than 30 days are deleted by the scheduler. Platform superusers can inspect read-only delivery diagnostics at `/platform-admin/accounts/passwordrecovery/`; gym owners cannot access that interface. Platform administrators control central backups and secrets. Tenant database access alone is insufficient to reset a shared account.

## Release setup

No new Python dependency is required. Run the standard central deployment migration once:

```shell
python manage.py migrate
```

Restart Django, the Celery worker and the single beat scheduler. Open each existing gym's central platform detail and click **Retry setup** to apply tenant migration 0007. New gyms install it automatically. Configure the SMTP transport using [notifications.md](notifications.md); no per-member or per-gym scripts are required. Account recovery cannot deliver until the SMTP transport is enabled. Domain/TLS and email provider setup remain operator responsibilities.
