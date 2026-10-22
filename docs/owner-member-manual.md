# Gym owner and member operating manual

## Payment model for this guide

Each gym uses its own COPAY merchant account. Member membership payments go to
that gym's merchant account; Powerhouse supplies the software. Powerhouse software
subscription invoices are a separate gym-to-platform billing flow.

The platform does not initiate merchant withdrawals in direct mode. Owners use
COPAY's merchant dashboard and agreed settlement/payout process. The optional
Powerhouse central wallet/withdrawal feature is not part of this direct flow.
Existing central transactions and ledger records must still be reconciled if any
were created before switching; disabling central mode does not move their funds.

All paths below belong to the gym's own HTTPS domain, unless explicitly labeled
Powerhouse. For example, `/manage/` means `https://YOUR-GYM-DOMAIN/manage/`.
Features depend on the gym's subscribed software modules and active service access.
No terminal commands are required for daily owner/member operations.

## Owner: first-time setup

1. Powerhouse creates the gym, provisions its database, assigns an owner account,
   connects the domain and enables the contracted modules.
2. Owner opens `/accounts/login/` on the gym's own domain and signs in. The owner
   dashboard is `/manage/`.
3. Configure website/branding, branches and membership plans.
4. Add staff and trainer profiles if needed, then publish classes.
5. Coordinate the gym's own merchant credentials with the platform operator using
   private server configuration. The current owner dashboard does not provide a
   merchant-secret editing form. Confirm COPAY onboarding and test the integration.
6. Configure gym email preferences after Powerhouse enables its email service.
7. Preview the public website and complete one test member journey before launch.

## Owner features and action manual

### 1. Dashboard — `/manage/`

Open the overview after login. Review registered/current members, today's check-ins,
open visits, membership applications, renewals, recent members and today's classes.
Switch the attendance chart between 7 and 30 days. Use the action links to open the
underlying records. Financial summaries use recorded production PHP COPAY member
payments; they are not a live COPAY balance, profit figure or bank reconciliation.
Open visits represent recorded visits that have not been checked out.

### 2. Website and branding — `/manage/website/`

1. Open **Website & branding**.
2. Choose the Strength or Studio preset.
3. Edit display name, color, HTTPS image/logo URLs, headline, introductory/about
   text, contact details, address and opening hours. Configure waiver text if used.
4. Save and open the public website to check the result.

Saving publishes immediately. This is a shared configurable template, not a
free-form drag-and-drop builder. Images are URL-based; there is no image-upload
workflow. Domain changes are handled by Powerhouse.

### 3. Membership plans — `/manage/plans/`

1. Add a plan with name, price, billing period, description and features.
2. Enable it to publish it; optionally feature it and set display order.
3. Save, then check the public `/memberships/` page.
4. To update a plan, open it, edit and save. Disable discontinued plans.

New applications and terms use the available plan. Existing membership/application
quotes retain their original prices and history. Publishing a plan does not charge
anyone or activate a membership.

### 4. Online membership applications — `/manage/applications/`

1. Visitor submits a plan request at `/join/`.
2. Owner opens **Applications**, reviews the requested plan and contact details.
3. Mark **Contacted**, **Approved**, or **Declined** as appropriate.
4. For an approved application, select **Enroll member**.
5. Open the resulting member profile and pending membership.

Approval/enrollment does not collect money or activate access. Repeating enrollment
opens the existing profile/term rather than adding a second one.

### 5. Walk-in members and profile maintenance — `/manage/members/`

1. Select **Add a member** for a walk-in.
2. Enter name, email, phone and optional birth/emergency-contact details; save.
3. Open the profile and assign a plan/start date under the membership section.
4. To correct details later, select **Edit profile** and save.
5. Disable member access when appropriate; this prevents portal access and entry.

The directory supports search and pagination. Creating a profile does not create a
login account or verify payment. Owners cannot use the profile form to rewrite
linked login identity or recorded waiver acceptance.

### 6. Member account invitation — member profile

1. Open the member profile and select **Create invitation link**.
2. Share that private link directly with the member.
3. Member opens it on the issuing gym domain and sets up or links their account.
4. Check that the profile shows a linked account.

Invitations expire after 48 hours, work once and are not automatically emailed.
Creating another invitation replaces earlier unused invitations. Do not publish
invitation links. Account setup and membership activation remain separate.

### 7. Assign, activate, freeze, resume and cancel membership — member profile

- **Assign/renew:** choose a published plan and start date; save a pending term.
- **Activate manually:** first verify the offline arrangement/payment externally,
  select Activate, provide a reason/reference and save. This grants dated access
  but does not generate a verified COPAY payment or payment receipt.
- **Freeze:** select Freeze on an eligible active term, record the reason and save.
- **Resume:** select Resume and record the reason. Paused days extend expiry when
  the resulting dates do not conflict with another active term.
- **Cancel:** select Cancel with a reason. This cancels that term; it does not issue
  a financial refund. A later membership requires a new term.

Start dates are inclusive and expiry dates exclusive. A membership expiring on
November 21 provides access through November 20. Pending, future, expired, frozen
and cancelled terms do not grant current entry. Overlapping active terms are blocked.

### 8. Membership COPAY payments — member profile and `/manage/payments/`

1. Open a member's eligible pending, unexpired membership.
2. Select **Create COPAY payment**. The server takes the quoted membership amount.
3. Owner or invited member opens **Pay securely with COPAY**.
4. Complete the provider checkout.
5. Return to the payment page. Wait for verified confirmation or use **Recheck
   payment status** on that same payment.
6. Review the paid acknowledgment and membership status. Print the acknowledgment
   if needed.

In direct mode the gym's merchant account receives the payment. The gym domain
receives the signed notification at `/payments/copay/notify/`. The browser return
alone never proves payment. Eligible production payments with the expected net
amount can activate the term; short/net-different payments need owner review.
Sandbox payments never activate membership. An acknowledgment is not a BIR official
tax invoice. Zero-priced memberships use the manual activation workflow.

After a timeout or unknown outcome, do not create another payment to retry. Recheck
the existing order and contact Powerhouse if needed. Direct merchant withdrawal,
settlement and balance checks happen through COPAY, not the optional platform wallet.

### 9. Review renewal requests — `/manage/renewals/`

1. Review the member's requested plan, quoted price, dates and note.
2. Approve or decline with a review note.
3. Approval creates a pending term at the request's preserved quote.
4. Open the member profile and complete COPAY payment or the verified offline
   activation process.

Approval does not itself charge or activate. If the requested start date has
passed, decline and arrange a new request; the system does not silently shift dates.

### 10. Member QR cards — member profile

Open **View / print member card** and print if needed. If a card is lost or copied,
select **Replace QR card**. Previous copies stop working immediately; the member
can open their updated card in the portal. Possessing a card does not bypass
membership or branch eligibility checks.

### 11. Branches and staff — `/manage/branches/`, `/manage/staff/`

1. Add a branch with name/address and enable it.
2. Add staff/manager login credentials and assign the gym role.
3. Share credentials privately; employees sign in on the gym's domain.
4. Disable a staff access grant when employment/access ends.

Trainer profiles are separate from employee logins. Staff and managers currently
share operational attendance, class-roster and permitted POS access; they are not
owners or Powerhouse admins. They cannot manage prices, stock corrections, website,
owner financial settings or other gyms. Disabling a branch blocks new entry while
still allowing existing visits to be checked out.

### 12. Front-desk check-in/check-out — `/desk/`

1. Choose the branch and Check in or Check out.
2. Scan by camera/USB reader, or paste the member QR code.
3. Select **Check access and record**. Camera capture alone does not admit a member.
4. Match the returned name/member number to the person and review the result.

Entry requires an enabled member, branch and currently active membership. One open
visit per member is allowed across that gym's branches. Repeated entry is not a
second visit. Camera scanning requires HTTPS and device permission.

For a missing checkout: open `/desk/attendance/`, filter the branch/open visits,
and close the visit with a reason. This records the current action time, not a
backdated exit. Visits do not auto-close at midnight.

### 13. Trainers and classes — `/manage/trainers/`, `/desk/classes/`

1. Add/enable a trainer profile.
2. Select **Publish a class** and enter title, trainer, branch, start/end and capacity.
3. Save and open its roster to review confirmed/waitlisted members.
4. To cancel a future session, enter a reason from its roster.
5. To change a published session's details, cancel it and publish a replacement.

A trainer cannot have overlapping sessions. Capacity one can represent an
appointment. The system does not currently generate recurring schedules, track
class no-shows, manage trainer payroll or collect separate class fees.

### 14. Products and inventory — `/manage/products/`, `/manage/inventory/`

1. Add a product with unique SKU, name, PHP price and low-stock threshold.
2. Open stock adjustment, choose branch/product, enter receiving quantity and a
   supplier/reference/reason.
3. For a removal/correction, use a negative quantity with a reason.
4. Review stock by branch and low-stock filter. Disable discontinued products.

Stock cannot become negative. Stock at one branch does not cover a sale at another.
Only owners manage prices and stock adjustments.

### 15. POS, receipts and retail refunds — `/pos/`, `/pos/sales/`

1. Choose branch, products and quantities; review cart/payment.
2. For cash, enter the tendered amount; review calculated change.
3. For bank transfer, verify receipt externally and enter its unique reference/note.
4. Confirm the sale. The system checks stock/prices again, deducts stock and creates
   the receipt together.
5. Open sales history to review/reprint receipts.
6. For a full refund, owner opens the receipt, returns money externally first,
   selects **Record full refund**, supplies a unique refund reference/reason, and
   chooses whether all returned items should be restocked.

POS currently records cash and externally verified bank sales; it does not open a
COPAY retail checkout. Staff see their own receipts; owners see all gym retail
sales. Refunds are full-sale records, not partial refunds/exchanges or provider API
refunds. Retail stock/payment history is separate from membership payments.

### 16. Daily retail report — `/manage/sales/report/`

Choose the local date and optional branch. Review recorded gross receipts, refunds
and net retail receipts. This excludes member membership payments and Powerhouse
software billing, and is not a profit, tax or bank reconciliation report.

### 17. Website inquiries — `/manage/inbox/`

Open messages submitted at `/contact/`, follow up with the sender, and mark read.
There is no built-in live chat or automatic reply workflow.

### 18. Notifications — `/manage/notifications/`

1. Enter the verified gym HTTPS origin and owner alert email.
2. Enable gym email and desired categories; save.
3. Queue a test to the owner inbox.
4. Review message/attempt history; retry an eligible failed delivery after fixing
   its cause. A retry may duplicate an email whose prior transport result was uncertain.

SMTP, worker and scheduler must already be configured by Powerhouse. Categories
include class bookings/reminders, membership updates/expiry reminders and payment
messages. Member opt-out applies to optional service notifications. Invitation
links are still manually shared. No SMS integration is included.

### 19. Powerhouse software subscription — `/billing/`

Review the gym's software plan, service state and invoices. Open an invoice, follow
the platform's payment instructions and provide its invoice number. Powerhouse
verifies and records the software payment. This manual describes direct gym member
payments; it does not add COPAY checkout for software invoices. Explicit suspension
is not automatically removed just because a payment is recorded.

## Member features and action manual

### 1. Apply — `/join/`

Visit the gym website, view `/memberships/`, choose a plan, enter contact details
and submit consent/request. The gym reviews it. Submission does not create login
credentials, collect payment or activate membership. Follow up with the gym about
approval and invitation.

### 2. Set up an account — private invitation

Open the gym's HTTPS invitation directly in the browser. Create a username/password,
or select the existing-account option and authenticate. Accept the displayed gym
waiver if configured. Submit, then open the portal. Never share the private link.
If expired/replaced, request a fresh invitation from the gym. The invitation is
not a payment or an active membership.

### 3. Sign in and dashboard — `/accounts/login/`, `/portal/`

Use the gym's domain and your credentials. Review your current/scheduled membership,
remaining days, recent attendance, upcoming bookings and branch information.
An expired membership can still allow portal login while the profile/account
remains enabled. Staff manage entry eligibility separately.

### 4. Update profile — `/portal/profile/`

Edit your own name, phone, birth date and paired emergency contact fields, then
save. Contact the gym for member email corrections. The form cannot change your
membership price/status, account linkage, member number or waiver evidence.
Changing the gym's contact email record does not change your central login email.

### 5. Pay and view acknowledgments — `/portal/payments/`

Open your payment, check the plan/amount, select **Pay securely with COPAY**, complete
checkout and return. Use **Recheck payment status** if confirmation is delayed.
Open the acknowledgment only after payment is confirmed. You see only your own
orders. Do not make a second payment while the first outcome is uncertain.
Membership activation and financial confirmation are shown separately if review is
needed. No stored-card or automatic recurring debit is implemented.

### 6. Membership QR card and attendance — `/portal/card/`, `/portal/attendance/`

Open your digital card or use a printed copy at the front desk. Staff scan it for
entry and exit. Review only your own visit history in Attendance. If a card is lost
or exposed, ask the gym to replace it. A QR card alone does not grant entry when
membership is pending, frozen, expired, disabled or not yet started.

### 7. Book/cancel a class — `/portal/classes/`

Browse sessions and select Book. A free seat confirms; a full session creates a
waitlist entry. Review the displayed status. Cancel your reservation before the
session starts if you cannot attend. Membership must cover the whole session;
confirmed timetable conflicts are blocked. Waitlist promotion rechecks eligibility
when a seat is processed; keep checking the portal for status. A class reservation
is not a front-desk check-in.

### 8. Request renewal — `/portal/renewals/`

Choose an available plan and start date, add a note and submit. One pending request
is allowed; cancel it before choosing a different request. Owner approval creates
a pending term. Complete the payment/activation step afterwards. Reviewed requests
cannot be withdrawn; contact the gym about their resulting term. No automatic
charge or instant activation occurs on submission/approval.

### 9. Email preferences — `/portal/notifications/`

Enable/disable optional gym notification emails and save. Actual delivery depends
on the gym/platform setup. Requested security password-recovery messages are a
separate flow, not controlled by this optional notification switch.

### 10. Change/reset password and sign out

- Signed in: open `/portal/password/`, prove the current password and set a new one.
- Forgot password: open `/accounts/password-reset/`, enter the exact login username
  and central account email, then follow the emailed link if eligible. Links expire
  after 30 minutes; recovery needs the platform email service.
- Use the navigation **Sign out** action when finished on a shared device.

If you use the same login identity at more than one gym, changing/resetting that
password affects the shared identity at those gyms. Password recovery is private;
gym owners cannot view a member's reset token in their notification history.

## Complete operating flows

### New online member

Website plan -> membership request -> owner review -> approve -> enroll -> pending
membership -> private invitation -> member account -> COPAY checkout -> verified
payment -> active term when eligible -> QR entry -> class booking/attendance.

Invitation and payment order can be prepared in either order by the owner. Member
self-checkout requires the linked login. The owner can open checkout for a walk-in.

### Walk-in member

Owner creates profile -> assigns pending plan -> verifies offline payment and
activates manually OR issues COPAY checkout -> creates/prints card -> records entry.
Invite the member when they want online portal access.

### Renewal

Member requests plan/dates -> owner reviews -> pending term -> payment/activation ->
new term applies on its start date. Past terms remain in history.

### Direct merchant funds

Member -> gym's COPAY account -> COPAY's merchant settlement/payout process -> gym.
Powerhouse records member payment status, but does not pay the gym from its central
wallet in this model. Software invoice payments remain gym -> Powerhouse separately.

### Daily opening/closing

Opening: review applications/renewals, upcoming classes, stock and working scanner.
During day: verify payments, supervise entries/exits, record sales and inquiries.
Closing: review open visits and retail report, reconcile external payments, inspect
failed emails, and verify COPAY settlement using the gym's merchant dashboard.

## Role boundaries and current limitations

Owner: manages their gym. Staff/manager: allowed attendance, class-roster and POS
operations. Member: own profile, terms, payments, card, visits and bookings.
Powerhouse: tenant setup, domains, platform subscriptions and merchant configuration.
Authentication is central; member/business records stay in separate gym databases.
A known URL or another gym's record ID does not grant access to that gym.

Not included: unattended gate integration, native mobile app, automatic monthly
COPAY debit, automated direct merchant payouts, membership refund API, partial POS
refunds, payroll, training-credit packages or official tax invoicing.

The latest reported live COPAY status requery returned HTTP 403. Code configuration
and implemented features do not prove a successful provider checkout. Resolve that
upstream refusal and complete a verified payment round trip before treating the
payment flow as operational for clients. Request code must not blindly resubmit
unknown payment attempts.

## Operator appendix: switch existing deployment to per-gym merchants

This is deployment setup, not a daily owner operation. Never change an unresolved
payment's stored merchant/environment/mode. Reconcile existing central orders with
their original account first; retain central config, mappings and ledger history.
No switch migrates old central funds to a gym's merchant account.

1. For each gym, obtain its own merchant code/AES key and registered merchant RSA
   private key, plus COPAY's public key, using a private channel.
2. Copy `deploy/copay.example.json` to the ignored `copay.secrets.json`. Add one
   entry for each gym using the immutable **Payment integration identifier** on its
   Powerhouse detail page. Every gym needs a different merchant/environment pair.
3. Set each entry's `public_origin` to that gym's verified HTTPS domain and select
   a merchant-supported PHP method. Point its key fields at private files. No
   merchant key material belongs in browser forms or the client manual.
4. Set `COPAY_CONFIG_FILE` to the absolute per-gym JSON path and
   `CENTRAL_COLLECTIONS_ENABLED=false`. Restart `powerhouse-gym` and workers as
   appropriate. Preserve `.env`, account keys and historical records.
5. Register each gym callback `https://GYM-DOMAIN/payments/copay/notify/` with COPAY
   if required and confirm server egress allowlisting, key registration and account
   permissions. Test each gym with dedicated test member/term/order records.

Current direct-merchant configuration requires RSA keys of at least 2048 bits.
`CENTRAL_COPAY_ALLOW_LEGACY_1024` is scoped to central collections and does not enable
1024-bit direct merchant keys. If a gym's registered keys are 1024-bit, direct
per-merchant compatibility needs a separate explicit implementation or a
COPAY-approved key upgrade; do not silently replace registered keys.

See [COPAY configuration](copay.md), [membership lifecycle](member-enrollment.md),
[member self-service](member-self-service.md), [attendance](attendance.md),
[classes](classes.md), [POS](pos.md), and [notifications](notifications.md).

### Automatic renewal invoicing

Owners can now enable renewal invoices at `/manage/automatic-renewals/` and
choose the advance notice and late-payment policy. Members explicitly agree to a
quoted plan at `/portal/automatic-renewals/` and can stop future invoices there.
The worker generates invoices and optional reminders; the member approves every
one-time COPAY checkout. A verified production payment schedules or activates
the next eligible membership. Each gym uses its own merchant account.
See the [complete automatic renewal flow and upgrade guide](automatic-renewals.md).
