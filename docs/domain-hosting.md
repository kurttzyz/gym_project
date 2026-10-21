# Automatic gym domains on an Ubuntu VPS

This opt-in integration supports the current Nginx/Gunicorn layout: application on
`127.0.0.1:8002`, public static files at `/var/www/powerhouse/static`, PostgreSQL
separate from other applications, Redis and Celery. No root password, sudo access,
DNS-provider credential or certificate private key is given to Django. It does not
purchase domains or modify external DNS. Owners must add A and TXT records once.
IPv6/proxied DNS ingress is deliberately unsupported by this first integration.

## How it works

Register a domain through **Add gym** or the gym detail page. With automation
configured, the background scheduler finds pending active public domains every
minute. It checks up to five per sweep. Missing DNS is retried every five minutes.
The detail page displays A/TXT instructions, status, last check and a retry button.
Refresh the page to see updates; there is no claim of instant DNS propagation.

The root-owned agent independently validates the hostname, exact public IPv4 A
record, absence of AAAA records, and the gym's ownership TXT token. It refuses
platform hosts, local names, shell/path injection, and conflicts with existing
Nginx server names (including wildcard/regex configurations). It never adopts,
replaces, disables or deletes existing manually managed sites. Before issuing a
certificate it serves only the ACME challenge, not tenant content. Certbot uses
HTTP-01 webroot, not the plugin that edits other Nginx sites. A successful setup
installs HTTPS, preserves Host, reloads Nginx and marks ownership verified/live.

Each generated site lives under `/etc/nginx/powerhouse-managed/`. Configuration
writes are atomic, checked with `nginx -t`, and rolled back on validation/reload
failure. Individual failed issuance attempts require explicit retry to avoid
hammering ACME rate limits. Inspect Certbot logs before retrying. A crashed worker's
lease expires after ten minutes; live domains aren't issued again on every sweep.
The existing Certbot renewal timer must be enabled; its deploy hook reloads Nginx.
Live is a recorded setup result, not continuous external DNS/TLS health monitoring.

## One-time VPS installation

Upload this release to `/home/megapaldo/apps/gym_platform` without replacing `.env`.
Keep the deployed production secrets stable. From that directory:

```bash
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py collectstatic --noinput
sudo cp -R staticfiles/. /var/www/powerhouse/static/
sudo find /var/www/powerhouse/static -type d -exec chmod 755 {} +
sudo find /var/www/powerhouse/static -type f -exec chmod 644 {} +
```

Install the agent with your real certificate contact email (replace the example):

```bash
sudo bash deploy/hosting/install.sh megapaldo 162.0.225.31 YOUR_EMAIL megapaldo.com
```

The installer creates a root-owned Python environment and copies the agent under
`/opt/powerhouse-hosting`. It uses the already-installed Nginx and Certbot. Inspect
these source files before installing; the running root service must never execute
code or an interpreter from an application-user-writable directory. Its socket
accepts only the configured application UID via kernel peer credentials. The
request accepts only hostname/proof; paths, commands, certificate flags and IP
addresses come from root-owned configuration. No arbitrary shell is invoked.
The root process still needs Nginx/Certbot privileges, so keep it separate and its
configuration/executable owned by root. OS root remains trusted across instances.

Enable the app-side configuration, preserving other `.env` values:

```bash
.venv/bin/python - <<'PY'
from dotenv import set_key
for name, value in {
    'DOMAIN_HOSTING_ENABLED': 'true',
    'DOMAIN_HOSTING_IPV4': '162.0.225.31',
    'DOMAIN_HOSTING_SOCKET': '/run/powerhouse-hosting/agent.sock',
    'CELERY_BROKER_URL': 'redis://127.0.0.1:6379/5',
}.items():
    set_key('.env', name, value)
print('Domain automation configured.')
PY
```

The worker and beat templates below match this VPS's user/path. Review them if
using another machine. Run only one Powerhouse beat scheduler for this broker;
do not start duplicates if Powerhouse notification workers already exist. Other
applications must not consume this broker's database/queue. Redis DB 5 must be
reserved for Powerhouse; DB numbers are logical separation, not a security boundary.

```bash
sudo cp deploy/hosting/powerhouse-worker.service /etc/systemd/system/
sudo cp deploy/hosting/powerhouse-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now powerhouse-worker powerhouse-scheduler
sudo systemctl restart powerhouse-gym
sudo systemctl status powerhouse-hosting powerhouse-worker powerhouse-scheduler --no-pager
sudo systemctl list-timers --all | grep -i certbot
```

If no renewal timer exists, configure a single daily `certbot renew` timer for your
Certbot installation. Do not duplicate a pre-existing snap/system renewal job.
Verify `sudo certbot renew --dry-run`. Ports 80/443 must be reachable publicly.
No firewall changes, database passwords or existing site changes are needed.

## Test with a new gym domain

1. In Powerhouse, add a real domain that is not already configured in Nginx.
2. Publish its A record to `162.0.225.31`; remove conflicting A/AAAA and turn off
   Cloudflare proxying during onboarding. Publish the exact TXT shown on the gym
   detail page at `_powerhouse-verification.<hostname>`. Keep that token private
   to domain administration until publishing; DNS itself is public.
3. Leave the matching TXT record published, including after manual verification.
4. Within the polling/propagation window, status changes from Pending/Waiting for
   DNS to Setting up HTTPS and Live. Website/owner links appear after Live.
5. Open the gym HTTPS site, sign in as its owner, and check the member registration
   flow. Use a second gym/domain to verify isolation. DNS/API provider integration
   is not included; domains controlled by independent gyms still require DNS edits.

Existing manually configured domains remain routed under their existing ownership
rules. Automation refuses their Nginx site conflict and shows Needs attention; it
will not take over their certificates. Keep those sites manual, or plan an operator
migration to the managed include separately. Enabling automation hides portal links
for domains whose automatic setup hasn't reached Live; it does not erase bindings,
existing certificates, or tenant databases. Disabling a domain in the registry blocks
Django access immediately; generated Nginx/certificate files remain for operator
review, and domain deletion/cleanup is intentionally not automated.

For troubleshooting:

```bash
sudo journalctl -u powerhouse-hosting -u powerhouse-worker -u powerhouse-scheduler -n 80 --no-pager
sudo nginx -t
sudo tail -n 60 /var/log/letsencrypt/letsencrypt.log
```

Fix the cause, then use **Retry hosting checks**. Ownership verification alone is
not a statement that TLS is ready. No live certificates, DNS changes or root-service
installation are performed by the automated test suite.
