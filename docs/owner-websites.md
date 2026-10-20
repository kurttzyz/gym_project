# Gym owner onboarding and reusable websites

Powerhouse administrators create gym owner accounts. Gym owners administer only
their gym's website; they do not become Django platform staff or superusers.

## Create a gym with its owner

1. Sign in to the Powerhouse platform and open `/gyms/new/`.
2. Enter gym details and initial branding, then an owner username, email, and
   initial password. The normal Django password validators apply.
3. Creation commits the central gym, user, and owner membership together. It then
   provisions the gym database, applies tenant migrations, and initializes the
   website from the shared defaults. No password is displayed in the result.
4. Register/verify the gym's custom domain on its detail page. Configure DNS and
   HTTPS for public domains; use the local hosts-file procedure for `.test` domains.
5. Share the owner's initial credentials through a private channel. Automatic
   invitation emails, account recovery, and first-login password changes are not
   implemented yet. Do not put passwords in domains, URLs, source control, or tickets.
6. The owner signs in at `https://their-gym-domain/accounts/login/` and lands at
   `/manage/`. Their dashboard links to the website editor and public website.

For an existing gym, select **Create gym owner** on its platform detail page.
This creates a new nonstaff account scoped to that gym. To grant access to an
existing platform identity, use central Membership administration instead.
Usernames are globally unique; creating an owner never resets another user's password.
If provisioning fails, the central gym/owner records remain and provisioning can
be retried with `migrate_gym_databases --slug <slug>`. It does not create another owner.

## One reusable website, individual settings

Every gym uses the same versioned Django website template. The `Strength` and
`Studio` design choices are presets within that template, not copied applications.
The owner can change:

- Display name, accent color, and optional HTTPS logo/photo links.
- Template preset, tagline, hero heading, and introduction.
- About heading/text, contact email/phone, address, and opening hours.

The website editor is `/manage/website/` on the gym's domain. Saving publishes the
changes immediately; there is no draft workflow. The view-public-website action
opens `/` in a new tab. Plain text is escaped, not interpreted as HTML or executable
CSS. Images use URLs; file uploads and a visual drag-and-drop builder are not included.

WebsiteSettings is a singleton in each gym's PostgreSQL database, not a central
shared content table. New sites inherit the gym's initial registry name/color/logo.
Later owner edits are authoritative for public pages, tenant login, and owner/member
portal branding. Registry branding fields are initial defaults, not a way to replace
owner edits on a live website. Domain configuration and provisioning remain platform-only.

`migrate_gym_databases` initializes missing website settings and preserves existing
customizations. Updating shared template code upgrades the common layout without
overwriting content. There is no Python folder or template copy for each customer.

## Tenant-domain routes

| Route | Access and purpose |
|---|---|
| `/` | Public branded gym website, no login required |
| `/accounts/login/` | Active gym-member login; owners land in `/manage/` |
| `/manage/` | Active owner only; gym dashboard |
| `/manage/website/` | Active owner only; website customization |
| `/portal/` | Active gym membership required; existing member portal |

Anonymous owner-route requests redirect to login. Authenticated users without an
active owner membership receive 404. Staff, managers, ordinary members, and platform
superusers without explicit gym-owner membership cannot edit a gym website.
The request hostname chooses the gym; submitted IDs/database keys cannot select
another gym. Forms retain CSRF protection and cookies remain host-only.

## Upgrade an existing local installation (PowerShell)

Stop runserver with Ctrl+C, then run:

```powershell
git pull origin main
python -m pip install -r requirements.txt
python manage.py migrate --noinput
python manage.py migrate_gym_databases
python manage.py runserver
```

The second migration command is required: it creates the website settings table
and initializes the template in each gym database. Back up existing databases
before schema upgrades. Your PostgreSQL service must be running.
