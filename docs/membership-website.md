# Gym membership website

Each gym domain now has a reusable, responsive public website backed by that gym's
PostgreSQL database. Its name, logo, colors, imagery, introductory copy, about text,
contact information, and hours come from the owner's existing website settings.
The Strength and Studio presets apply across all public pages.

## Public pages

| Route | Purpose |
|---|---|
| `/` | Branded homepage with current published membership plans |
| `/memberships/` | Published plan prices/benefits and membership FAQ |
| `/about/` | Editable gym story and introduction |
| `/contact/` | Contact information and message form |
| `/join/` | Select a plan and submit a membership application |
| `/join/thanks/` | Generic receipt without exposing applicant details |
| `/contact/thanks/` | Generic message confirmation |
| `/accounts/login/` | Existing gym owner/member login |

Prices are PHP and are configured by the gym owner, not copied from Powerhouse's
SaaS pricing. No sample prices or fabricated testimonials are seeded into real
gyms. If no plans are published, visitors are directed to contact the gym rather
than shown invented offers. The public Join form requires an available published
plan, contact details, and permission for the gym to contact the applicant.

## Owner workflow (no per-gym scripts)

1. Sign in on the gym's domain and open `/manage/`.
2. Select Manage memberships, then Create a plan.
3. Enter name, price, term, description, and one benefit per line. Publish it and
   optionally feature it. Save; it appears on the public membership page immediately.
4. A visitor requests that membership. Review the request under Review applications.
   Mark it Contacted, Approved, or Declined after following up with the applicant.
5. Read contact submissions in the Gym inbox and mark them as read when handled.

Plans can be hidden instead of deleted. Existing applications retain a snapshot of
the plan's name, price, and term, even when the plan changes. The server selects
that quote from the published plan; posted prices or statuses are ignored. Duplicate
pending requests for the same email/plan are prevented case-insensitively. Closed
applications cannot be reopened through the review endpoint.

**An approved application is not an active paid membership.** This release does not
collect payments, create member login accounts from applications, schedule recurring
charges, or send emails. Owners follow up using the submitted contact details.
Payment activation and account enrollment are separate workflows to implement with
a configured provider. Public forms currently store requests in the gym's private
database/inbox rather than sending them to an external messaging service.

## Owner routes

| Route | Purpose |
|---|---|
| `/manage/` | Website, published-plan, application, and message overview |
| `/manage/plans/` | Plan catalog management |
| `/manage/plans/new/` | Create a plan |
| `/manage/plans/<uuid>/` | Edit/publish/hide a plan |
| `/manage/applications/` | Paginated private application list |
| `/manage/inbox/` | Paginated private contact inbox |
| `/manage/website/` | Existing branding/content editor |

Only active gym owners can use these routes. Staff, managers, ordinary members,
and platform superusers without that gym-owner membership cannot access them.
Public pages expose only published plan details and the configured public content;
applications, contact details from forms, and review actions are private.

## Isolation and upgrades

MembershipPlan and MembershipApplication belong to the tenant `gym_core` app.
ContactMessage belongs to tenant `websites`. These tables are never created in the
central identity database. All queries/forms are evaluated inside the hostname's
explicit gym database scope, including lazy plan selections during form rendering.
A plan/application identifier from another database cannot select another gym.
Owner actions use CSRF-protected POST; public forms enforce CSRF, consent, and a
hidden spam field. Normal Django escaping applies to all stored text.

New gyms receive the schema during ordinary UI onboarding. Existing gyms can open
their Powerhouse detail page and click Retry setup when the new module's tables
are missing. This migrates the gym database and preserves existing branding and
branches. Operator migration commands remain available for release maintenance,
but are not part of the owner's or platform administrator's per-gym workflow.

## Validation

The real PostgreSQL suite covers published/hidden plans, precise decimal quotes,
owner-only writes, duplicate applications, stale or cross-gym plan IDs, review
transitions, contact inbox isolation, CSRF, and escaped stored content. Upgrade
coverage exercises installing the module from Retry setup while preserving old
branding. The design was also rendered in Chromium at desktop and mobile sizes;
visual fixtures and prices used for that inspection are not saved as gym records.
