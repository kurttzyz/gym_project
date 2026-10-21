# Branded email notifications

Each gym has its own notification preferences, durable email outbox, and delivery attempt history in its own PostgreSQL database. SMTP credentials belong to the platform environment, never to a browser form or broker message. The shared transport sends using a platform-verified sender address with the gym's display name; the gym website contact email is Reply-To. Emails use that gym's name, color, and verified website origin, without Powerhouse branding.

## What sends

| Event | Recipient |
| --- | --- |
| Class confirmation, waitlist entry, waitlist promotion | Member |
| Member cancellation or gym session cancellation, including reason | Member |
| Confirmed class starting within 24 hours | Member |
| Active membership term expiring in 7 days, 1 day, or today | Member |
| Verified production COPAY payment received | Member |
| Verified production payment failed or needs owner review | Configured owner inbox |
| New membership application | Configured owner inbox |

Class messages and membership reminders use the memberships SaaS module. Member payment confirmations and payment issue alerts use the payments module. These are gym-to-member operational notifications, not marketing campaigns or platform subscription receipts. There is no automatic payment collection or membership renewal in this feature. Sandbox payments never send payment notifications.

## One-time platform setup

1. Install the updated `requirements.txt`.
2. Set the following in the ignored `.env` using your email provider's SMTP credentials:

```dotenv
EMAIL_HOST=smtp.your-provider.example
EMAIL_PORT=587
EMAIL_HOST_USER=your-smtp-user
EMAIL_HOST_PASSWORD=your-smtp-password
EMAIL_USE_TLS=true
EMAIL_USE_SSL=false
DEFAULT_FROM_EMAIL=notifications@your-verified-sender-domain.example
NOTIFICATION_DELIVERY_ENABLED=true
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
```

Use a sender address your provider has verified. Configure its SPF/DKIM/DMARC records as instructed by that provider. For implicit SSL on port 465, enable `EMAIL_USE_SSL` and disable `EMAIL_USE_TLS`. Do not enable both. Anonymous internal SMTP relays may leave username/password blank. SMTP timeout is 20 seconds. `manage.py check` rejects enabled transport with no host, a placeholder/invalid sender, or conflicting TLS settings.

3. Restart Django after changing environment configuration.
4. Open each existing gym in the central platform and click **Retry setup** to apply the new tenant migration. New gyms get it automatically. No tenant SQL or management scripts are necessary.
5. Keep Redis, one Celery worker service, and exactly one Celery beat scheduler running. The scheduler queues a tick every 60 seconds and dispatches separate work for registered gyms. Normal application requests never need Redis or SMTP to succeed; they write to the tenant outbox.

For the existing Docker PostgreSQL setup, run the services as persistent containers:

```shell
docker compose --profile notifications up -d --build
```

The worker image uses a non-root user, excludes `.env`, private keys, uploads, and local databases from its build context, and receives configuration through `env_file` at runtime. Beat state has its own persistent volume. Containers restart automatically. The bundled web instructions still use your existing Django server; the worker image is not a production web deployment.

If your PC uses native PostgreSQL 18 on port 5433, set these additional worker values before starting **only** Redis and the notification services (to avoid starting another PostgreSQL server on the same port):

```dotenv
WORKER_PGHOST=host.docker.internal
WORKER_PGPORT=5433
```

```shell
docker compose --profile notifications up -d --build redis notification-worker notification-scheduler
```

Native PostgreSQL must allow the Docker worker connection with normal password authentication, and the existing database credentials must match `.env`. If Docker cannot reach native PostgreSQL, run the worker natively instead:

```shell
celery -A platform_config.celery:app worker --pool=solo --loglevel=info
celery -A platform_config.celery:app beat --loglevel=info
```

Use separate terminals (with `.venv` activated); Windows uses `--pool=solo`. In production use your process manager or container orchestrator instead of interactive terminals. Redis should be on a private network with authentication/TLS as appropriate; the local Compose port binds only to loopback. Redis holds task UUIDs, not email bodies or credentials. A broker outage delays dispatch but doesn't lose the PostgreSQL outbox. No Redis persistence is required to retain the authoritative notification records.

## Gym owner setup (no scripts)

1. Sign in on the gym's own domain and open `/manage/notifications/` from the owner dashboard.
2. Enter the gym's active verified website origin, e.g. `https://ironfit.example.com`. No path/query/userinfo is allowed. It must belong to that gym. HTTPS is required outside debug mode; local debug origins may include an HTTP scheme and runserver port.
3. Choose an owner inbox if you want owner alerts, choose categories, and enable email for the gym. Both the gym switch and platform transport switch must be enabled for actual delivery.
4. Use **Queue test to owner inbox** with owner alerts enabled. It queues a message; the next worker tick sends it using SMTP. The status page shows the last worker check, each message's outcome, and its attempt history.

Setup doesn't retroactively email old bookings, payments, or applications. Scheduled reminders evaluate existing eligible memberships and bookings. A test message requires no member account and is addressed only to the configured owner inbox.

Members manage email preferences at `/portal/notifications/`, linked from their own portal and notification emails. Opting out suppresses queued and future member emails; owner alerts are separate. Preferences and owner history remain accessible during SaaS suspension so notifications can be disabled without reactivating a subscription. Only active linked members can change their own preference; only active gym owners can view configuration or delivery history. Superuser status alone does not grant tenant access.

## Delivery, retries, and suppression

- Business events and outbox rows are written in the same tenant transaction. A rollback removes both. Stable unique event keys prevent duplicate queue entries. Booking event history gives rebookings separate notifications. Public application creation already runs in a transaction.
- Reminders have stable term/expiry-date or booking/queue-version keys. Repeated scheduler ticks don't duplicate them. Membership expiry uses exclusive expiry dates: on the expiry date the term has already expired at local midnight. Catch-up outside the 7/1/0-day windows is not sent. A paid active adjacent renewal suppresses the reminder; a pending/unpaid renewal does not.
- Before SMTP, the worker rechecks gym/category switches, member activation/opt-out/current address, current owner inbox, verified domain/origin, current SaaS allowance/module, and expiry. It skips obsolete booking status/version, changed membership expiry, and renewed memberships. Class reminders also recheck current membership, branch, and trainer eligibility.
- Disabling a switch causes pending messages to be skipped on the next processing pass, rather than sending later if you re-enable it. With platform transport disabled, messages stay queued without attempts and may expire before re-enabling.
- Workers claim rows using short PostgreSQL locks and five-minute leases; network calls happen outside transactions. Concurrent workers cannot normally send the same claimed message. A recovered abandoned lease is retained in attempt history.
- Transient failures retry with exponential delays (2, 4, 8, 16 minutes), up to five attempts. Recipient refusal is terminal. Expired or suppressed messages are not retried. Owners can explicitly requeue an unexpired failed message with five more attempts, retaining earlier history, up to 100 total attempts. Sent/skipped/sending messages cannot be manually requeued.
- SMTP is **at least once**, not exactly once: a crash or dropped connection after server acceptance can cause another copy on retry. The stable Message-ID helps mail systems recognize repeats but is not a guarantee. “Sent to mail server” means accepted by SMTP, not delivered/read. Bounce/complaint webhooks and suppression lists are not implemented yet.
- Provider exception text is not stored or logged because it may contain credentials, email addresses, or private responses. History uses bounded outcome codes. Outbox text/HTML are tenant-private snapshots, with HTML escaping, and one recipient per email.

No real SMTP messages are sent in integration tests. They use Django's in-memory mail backend and isolated tenant databases. SMS, marketing, bounce ingestion, per-gym SMTP credentials, retention/purge UI, and platform B2B billing emails are future extensions. Protect outbox data with the same backups and access controls as member records.
