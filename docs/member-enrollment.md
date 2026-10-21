# Member enrollment and membership lifecycle

This is the first operational milestone after the public membership website.
Every profile, invitation, membership term, and lifecycle event belongs to the
request's gym database. Central accounts and access roles remain central; their
IDs are stored as scalar references, never cross-database foreign keys.

## Owner workflow

1. On the gym's own domain, sign in as its owner and open `/manage/members/`.
2. Create a walk-in member, or approve an application under
   `/manage/applications/` and select Enroll member. Repeated enrollment opens the
   same profile/term. Application enrollment preserves its original quoted price.
3. Open the member and assign a published plan if needed. Assignment creates a
   pending term and snapshots the plan name, price, and period.
4. Select Activate (manual/offline), enter a reason or offline reference, and save.
   This grants dated access; it does not verify payment, create a receipt, or
   charge a card. Alternatively create a [COPAY payment](copay.md) from a pending
   term; verified production settlement can activate it automatically. Lifecycle history records the note and time, with an operator ID for manual
   actions or System for verified COPAY activation.
5. Create an invitation link and share it privately with the member. Only the
   digest is stored. Links expire after 48 hours, are single-use, and work only
   on the issuing gym's domain. Reissuing, changing email, or disabling the
   profile invalidates outstanding invitations. Links are displayed once;
   email delivery is not implemented.
6. The member opens the link, chooses a username/password or proves ownership
   of an existing platform account, and accepts the gym's configured waiver.
   They are signed into `/portal/` and can later use `/accounts/login/` on this
   gym's domain. Existing passwords and other gyms' access are preserved.

The website editor at `/manage/website/` can configure waiver text. When present,
acceptance is required at invitation claim, and the exact text and acceptance time
are retained on the profile. A changed waiver must be reviewed again before
claiming. This does not implement electronic signatures, guardian consent,
periodic reacceptance, or an assessment of legal enforceability.

## Dates and state

Term start dates are inclusive; expiry dates are exclusive. A term expiring on
November 21 grants access through November 20. Calendar months/quarters/years
clamp to the last valid day when necessary; day passes last one calendar day.
Dates use `APP_TIME_ZONE`, default `Asia/Manila` for this deployment.

Pending terms confer no access. Active terms may be scheduled in the future;
access ends automatically by date without a background job. Owner actions can
freeze a currently active term, resume it with expiry extended by paused days,
or cancel it permanently. A renewal is a new term, preserving the original
history. Overlapping active terms and activation while a frozen term exists are
rejected; adjacent renewal terms are permitted. Resume is rejected if its
extension would overlap an already scheduled active term.

Disabling a profile denies its portal login and access regardless of term state.
Expired membership alone still permits portal login to review membership history.
Entry eligibility is calculated here; QR scanning and attendance recording belong
to the next milestone and are not implemented by this release.

## Permissions and recovery

Only active gym owners manage these records. The profile form cannot change a
linked identity or rewrite waiver evidence. A member sees only their own profile
and terms; plan/profile IDs or invitation tokens from another gym cannot select
that gym's records. Public activation requires the secret invitation and CSRF;
invitation responses disable caching and referrer forwarding.

Claiming spans the central and tenant databases, so it is not a distributed
transaction. The central grant commits first. Portal access also requires an
enabled linked tenant profile, so a rare tenant commit failure leaves that grant
unusable. The user can retry the same unused invitation using the existing-account
option. Inactive central access is never reenabled by invitation claim.

New gyms receive this schema during normal onboarding. For an older gym, open
its platform detail page and choose Retry setup to install the missing tables
and waiver field while keeping branding and branches. No per-gym scripts are
required. Release operators can still use the documented migration commands.

The PostgreSQL integration suite covers original quotes, duplicate enrollment,
calendar boundaries, activation, overlap rejection, freeze/resume/cancellation,
renewal and expiry, invitation expiry/replay/replacement, existing identities,
waiver snapshots, permission/CSRF checks, and physical tenant table isolation.
