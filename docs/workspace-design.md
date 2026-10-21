# Client-facing workspace design

The platform, gym-owner, staff, and member interfaces share a local design system in `websites/static/workspace/`. There are no hosted fonts, icon libraries, external chart services, or new build dependencies. Tenant names, logos, and accent colors come from the existing gym website settings; button text switches between black and white using the accent's relative luminance for contrast. Platform branding remains separate from tenant branding.

## Workspaces

- **Platform overview (`/` on the platform host):** gym portfolio, search by name or identifier, setup-state filter, pagination, owner counts, verified domains, and shortcuts to onboarding and software billing. “Provisioned” describes the persisted database setup state; it does not claim real-time uptime or that DNS/TLS is configured.
- **Gym-owner overview (`/manage/` on the gym's host):** registered and currently active members, today's check-ins, open visits, current-month production COPAY amounts received, new applications, renewal queue, inbox, 7/30-day attendance chart with an accessible daily-data table, recently added members, today's remaining classes, and website preview/settings.
- **Member overview (`/portal/`):** own current/scheduled membership, remaining days, monthly recorded visits, upcoming confirmed/waitlisted bookings, digital card link and member ID, recent visits, own membership history, profile/payment/security links, and gym branch information. A missing term or empty history gets a useful next step rather than invented demo activity.
- **Staff workspace:** navigation exposes attendance, class rosters and/or retail workflows according to existing permissions and included modules. Staff do not receive member-management, owner settings, platform or other members' portal links.

All new projections run inside the existing authorized tenant database scope. The platform overview reads only central registry/domain/subscription records. No tenant selector can be supplied through a dashboard query parameter. Navigation is a convenience, not an authorization boundary: existing decorators and middleware continue enforcing access.

## Navigation and components

The sidebar groups workflows into collapsible native sections. The active workflow opens automatically; owner overview also opens the membership and gym operations sections. Other sections remain discoverable without requiring a long unstructured menu. Quick navigation searches the authorized sidebar links; open it with **Find a page** or **Ctrl/Cmd K**. It searches page titles, not member/customer data. Escape closes it and the mobile drawer; keyboard focus remains within the drawer and returns to its trigger on close. A no-JavaScript fallback exposes navigation in the page on small screens.

The shared system covers typography, spacing, forms, validation, notices, status badges, tables, empty states, booking cards, scanner display, printable cards and receipts. Tables scroll inside their own container on narrow screens. Mobile inputs use a 16px font to avoid focus zoom in common mobile browsers. Reduced-motion settings and visible focus indicators are respected. Print styles remove navigation, dialogs, toolbars and other workspace chrome.

Public website presets retain their content and Strength/Studio identity, with refined spacing, typography, buttons, hero treatment, forms and mobile navigation. Existing website editing remains the way to supply real gym photos and copy. No stock photo, fake testimonial, member count or performance claim is inserted into a real gym website.

## Metric definitions

- Active members are distinct enabled profiles with an active term covering the local date (inclusive start, exclusive expiry).
- Attendance is recorded visits by local check-in date across the gym's branches. Open visits are records with no checkout; they are not a physical occupancy sensor.
- COPAY received sums non-null `net_amount` on paid **production PHP** orders with settlement dates in the current local month. Sandbox orders, retail sales, platform receipts and unrecorded/offline payments are excluded. It is not a profit/tax/settlement reconciliation report.
- Today's classes are non-cancelled sessions starting on the local date whose end is still in the future; reserved counts include confirmed bookings only.
- Member upcoming bookings include confirmed and waitlisted entries in non-cancelled sessions that have not ended.

Local dates use the configured `APP_TIME_ZONE` (Asia/Manila by default). Dashboard figures are queried when a page loads; there is no claim of streaming updates. Changing the attendance range reloads the server-rendered page and recomputes only the supported 7/30-day windows.

## Release and verification

There are no schema changes or new Python packages for this redesign. Pull the update and restart Django. Production deployments must run `python manage.py collectstatic --noinput` and serve the updated static files. Restarting Celery or migrating databases is not required solely for these presentation changes. If upgrading from an older functional release as well, follow that release's migration/setup instructions first.

Verification covers role/tenant boundaries, production-only financial summaries, attendance ranges, portfolio filtering, server-rendered template compilation, desktop and mobile layouts, quick navigation, mobile drawer focus, POS row additions and payment-method toggles, native public navigation, and receipt/card print layouts. Visual previews use unsaved fixture objects outside the repository; no demo members, revenue, bookings or sales are inserted into production data.
