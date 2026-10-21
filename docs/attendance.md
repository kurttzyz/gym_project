# QR check-in and attendance

Each gym has its own member cards, visits, and scan audit in its dedicated database.
Attendance uses the existing dated membership rules and records the staff operator
for entry and exit. It does not operate physical doors, charge members, perform
facial recognition, or infer occupancy from a subscription payment.

## Set up through the website

1. Pull the release, install updated `requirements.txt`, and restart Django. Existing
   gyms can use **Retry setup** on their platform detail page to add the QR version
   field and attendance tables without per-gym scripts. New gyms receive them during
   normal onboarding. Production must serve the application's collected static files.
2. On the gym domain, the owner opens **Manage branches** at `/manage/branches/`.
   Add a branch or edit an existing one. Disabling a branch blocks entry but leaves
   checkout available for members already inside.
3. Open **Manage staff access** at `/manage/staff/` to create a staff or manager
   account and share its credentials privately. These are ordinary central identities
   with an access role for this gym only, not Django staff/platform administrators.
   Both roles currently have the same attendance permissions across this gym's
   branches. Owners can disable/reenable this gym access without disabling the
   person's account or their access to another gym.
4. An enrolled member signs in on the gym domain and selects **My QR member card**.
   For walk-in members without a login, the owner opens the member's profile and
   selects **View / print member card**. The browser's Print card button prints
   a card without navigation or the raw-code copy field.
5. Staff sign in and land at `/desk/`. Select a branch and Check in or Check out.
   Capture the QR using a phone/computer camera, use a USB QR scanner, or paste
   the card's code. Review the selection and select **Check access and record**.
   Camera capture fills the form; it does not submit or admit anyone automatically.
6. Match the returned name/member number to the person before permitting entry.
   The result confirms entry, denied access, already checked in, or checkout.

Camera access requires HTTPS or a browser-trusted localhost origin, permission,
and working hardware. If unavailable, USB/paste input remains available. Camera
frames are decoded in the browser using a locally hosted library; no images are
uploaded and no third-party CDN is required. The camera stops after capture, form
submission, tab hiding, or leaving the page.

## Access and visit rules

Entry requires an enabled profile, an enabled branch, and an active term whose
start date is today or earlier and whose exclusive expiry date is later than today.
Pending, scheduled, expired, frozen, and cancelled terms do not permit entry.
Dates and filters use `APP_TIME_ZONE` (default `Asia/Manila`). Eligibility is checked
again for every entry; merely possessing a card never grants access or a login.

There can be only one open visit per member across all this gym's branches. Repeated
scans return Already checked in rather than incrementing visits. An open visit in
another branch must be checked out at that branch before a fresh entry. Reentry
after checkout is permitted while the membership is eligible; entry-count limits
per plan are not implemented.

Checkout does not require a still-active membership/profile/branch: members can
leave after membership expiry or suspension. A valid current card is required for
QR checkout. If the member has no card or forgot to scan on leaving, staff can open
attendance history and close the open visit with a required reason. This records
the actual time of that operation and operator; it cannot backdate or rewrite an
already completed checkout. Visits never auto-close at midnight. Current occupancy
is based on recorded open visits, not a guarantee that every exit was scanned.

Each scan carries a request UUID. Identical replays return the recorded result with
an explicit Previously processed label; they never open a second visit, including
after the original visit has been checked out. Reusing a request ID with different
input is rejected. Database transactions lock the branch/member and a partial unique
constraint enforces one open visit even with concurrent scanners. Denied and repeated
scans are audited; raw credentials and camera frames are never stored in that audit.

## Cards and privacy

Cards contain an opaque signed token with immutable gym/member IDs and a revocable
card version. They contain no contact/payment details and do not use a public URL
with credentials in query strings. A token from another gym, a plain member number,
a forged signature, or a replaced version cannot select another gym's records.

Cards do not expire automatically, so printed cards work until replaced. Under the
member profile, **Replace QR card** invalidates all previous copies immediately.
The member's next card view produces the new version. A card can be copied; front
desk verification remains necessary. These are supervised front-desk credentials,
not proof of physical presence or an unattended anti-sharing turnstile system.
Card pages and SVG responses require member/owner access and disable caching.
Changing the application's signing secret invalidates cards unless a planned
Django signing-key fallback is retained; do not rotate it casually.

## Routes and permissions

| Gym-domain URL | Who / purpose |
|---|---|
| `/desk/` | Owner, manager, staff: scanner and today's visit/open-visit counts |
| `/desk/attendance/` | Same roles: paginated visit history, date/branch/open filters, recent scan audit |
| `/desk/attendance/<uuid>/checkout/` | Same roles: POST checkout without a card, required reason |
| `/portal/card/` and `/portal/card.svg` | Enabled linked member: own card only |
| `/portal/attendance/` | Enabled linked member: own visit history only |
| `/manage/members/<uuid>/card/` | Owner: view/print a member's card |
| `/manage/members/<uuid>/card/replace/` | Owner: POST card replacement |
| `/manage/branches/` | Owner: create/edit/disable branches |
| `/manage/staff/` | Owner: create and enable/disable front-desk access |

Staff/managers cannot manage member profiles, plans, payments, branding, branches,
or staff accounts. Platform superuser status alone grants no tenant attendance
access. Revoked staff access is checked on every request, including existing
sessions. Mutating actions require same-origin CSRF-protected POST. Members see
only their own attendance; staff pages expose only operational member name/number,
not emergency contacts, waivers, or payment histories.

## QR library and validation

QR images are generated locally using Python `qrcode` 8.2. The camera decoder is
vendored **jsQR 1.4.0** from the npm `jsqr` package under Apache-2.0, with its license
in `websites/static/attendance/vendor/jsQR-LICENSE.txt`. No external runtime script
requests are required. Vendored `jsQR-1.4.0.js` SHA-256:
`bc40c8a15196236b2314db0856f72ca0b49980cd5413b8c852a7349f5fee0859`.

The real PostgreSQL suite covers entry/exit, membership boundaries, disabled profiles
and branches, card replacement, invalid/cross-gym tokens, repeated/replayed and
concurrent scans, manual checkout audit, owner/staff permissions, CSRF, staff
revocation, member-only views, and UI-driven schema upgrades. Browser checks use
unsaved fixtures and synthetic camera frames to validate QR encoding/decoding and
screen behavior; physical scanners/cameras still depend on the user's hardware.
