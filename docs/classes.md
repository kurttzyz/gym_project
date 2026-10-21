# Classes, trainers, and member booking

All trainer profiles, sessions, bookings, and booking events live in each gym's own PostgreSQL database. No tenant IDs supplied by a browser select a database; the verified request hostname and central active role select it. Classes are included in the existing **memberships** SaaS module, including trial and paid-term snapshots. Subscription suspension and module gating apply before tenant views run.

## Owner workflow

1. Open the gym's `/manage/trainers/` and add a trainer profile. Trainer profiles are not login accounts; employee access remains under `/manage/staff/`.
2. Create an active branch under `/manage/branches/` if needed.
3. Publish a session at `/manage/classes/new/`: title, description, trainer, branch, start, end, and capacity (1–500). Dates use `APP_TIME_ZONE`, default Asia/Manila. Capacity 1 supports a personal training appointment.
4. Review `/desk/classes/` and each session's roster. Owners, managers, and staff can see rosters; only owners maintain trainers, publish, or cancel sessions.
5. Cancel a future session from its roster with a reason. Reservations and waitlist entries are cancelled atomically and retained in history. Members see the reason in their portal. Published session details cannot be edited: cancel and replace to change details. A cancelled session frees the trainer's schedule.

A trainer cannot have overlapping non-cancelled sessions, including across branches. Different trainers may run simultaneous sessions in a branch; room scheduling is not included. Disabling a branch or trainer stops new reservations and promotions but does not silently cancel existing reservations. Owners should explicitly cancel affected sessions.

## Member workflow

Members sign in on their own gym domain and open `/portal/classes/`, also linked from their portal. Only an enabled member profile linked to that login and an active central member role can book or cancel their own reservations. Staff cannot impersonate a member using a submitted member ID.

- Booking requires an active membership covering the entire session, using inclusive start/exclusive membership expiry. Frozen, pending, cancelled, disabled, or expired memberships cannot book.
- A class with a free seat confirms the reservation. A full class adds it to the waitlist.
- A member cannot hold overlapping confirmed bookings across branches or trainers. A new waitlist request is also refused if the member already has a confirmed conflict.
- Cancellation is available until the session starts. No cutoff fee or refund is involved.
- Cancellation of a reservation offers its seat to waitlisted members in original queue order. Ineligible members and those with confirmed conflicts are skipped and retain their waitlist status. Promotion rechecks membership and branch/trainer availability. Cancelling and rebooking joins the back of the queue.
- Promotion runs when a reservation is cancelled or a new booking is attempted. It is not a scheduled worker: restoring membership eligibility or cancelling a conflicting class elsewhere does not alone trigger promotion.
- Members must check the portal for promotion/cancellation status. Optional branded email confirmations, cancellations, promotions, and upcoming-class reminders are available after [notification setup](notifications.md). SMS is a later milestone.
- The portal shows the latest 100 personal bookings, with paginated upcoming classes. Staff can browse session history and rosters. A booking is not gym entry attendance; QR entry still validates current membership separately.

Bookings do not collect class fees, consume training credits, or automatically renew memberships. Eligibility changes after confirmation do not erase booking history or automatically release a seat. Gym staff remain responsible for verifying eligibility at class time. Recurring timetable generators, room allocation, class attendance/no-shows, trainer payroll, and paid personal-training packages are future extensions.

## Reliability and upgrades

A PostgreSQL advisory transaction lock serializes short class scheduling/booking mutations **within a tenant database**. This protects capacity, FIFO allocation, retries, and cross-session conflicts under concurrent requests. PostgreSQL releases the lock on commit/rollback; another gym's database is unaffected. One unique booking row per session/member and immutable booking events preserve retry safety and cancellation history.

New gyms receive the migration automatically. After pulling the release and restarting Django, open each existing gym's central platform detail and click **Retry setup**. The platform detects missing class tables and applies tenant migrations without deleting members, sales, branding, or existing data. No tenant management scripts are needed.

Integration tests exercise booking/waitlist transitions, concurrent final-seat allocation, trainer/member conflicts, active membership coverage, duplicate retries, cancellation history, staff/member permissions, CSRF, cross-domain isolation, and SaaS module enforcement.
