#!/bin/bash
# Run once as root: bash install.sh APP_USER IPV4 ACME_EMAIL PLATFORM_HOST
set -euo pipefail
if [ "$EUID" -ne 0 ] || [ "$#" -ne 4 ]; then
    echo 'Usage: sudo bash deploy/hosting/install.sh APP_USER IPV4 ACME_EMAIL PLATFORM_HOST'
    exit 1
fi
APP_USER=$1
IPV4=$2
ACME_EMAIL=$3
PLATFORM_HOST=$4
SOURCE=$(cd -- "$(dirname -- "$0")" && pwd)
APP_UID=$(id -u "$APP_USER")
APP_GID=$(id -g "$APP_USER")
CERTBOT=$(command -v certbot)
/usr/sbin/nginx -t
# Root must own every parent of the executable and its interpreter.
install -d -o root -g root -m 755 /opt/powerhouse-hosting
if [ ! -x /opt/powerhouse-hosting/venv/bin/python ]; then
    python3 -m venv /opt/powerhouse-hosting/venv
fi
/opt/powerhouse-hosting/venv/bin/python -m pip install 'dnspython==2.9.0'
install -o root -g root -m 644 "$SOURCE/agent.py" /opt/powerhouse-hosting/agent.py
install -d -m 755 /etc/nginx/powerhouse-managed /var/lib/powerhouse-acme
mkdir -p /etc/letsencrypt /var/lib/letsencrypt /var/log/letsencrypt
python3 - "$APP_UID" "$APP_GID" "$IPV4" "$ACME_EMAIL" "$PLATFORM_HOST" "$CERTBOT" <<'PY'
import ipaddress, json, os, sys
from pathlib import Path
uid, gid, ipv4, email, host, certbot = sys.argv[1:]
ipaddress.IPv4Address(ipv4)
if not Path(certbot).is_absolute() or '@' not in email:
    raise SystemExit('Check certbot path and ACME email.')
for item in [Path(certbot).resolve(), *Path(certbot).resolve().parents]:
    info = item.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit('Certbot and its parent directories must be owned by root and not writable by other users.')
path = '/etc/powerhouse-hosting.json'
with open(path, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as out:
    json.dump(dict(allowed_uid=int(uid), allowed_gid=int(gid), ipv4=ipv4,
                   email=email, reserved_hosts=[host.lower()], certbot=certbot), out)
os.chmod(path, 0o600)
PY
# A separate include leaves existing sites untouched.
INCLUDE=/etc/nginx/conf.d/powerhouse-managed.conf
EXPECTED='include /etc/nginx/powerhouse-managed/*.conf;'
if [ -e "$INCLUDE" ] && [ "$(cat "$INCLUDE")" != "$EXPECTED" ]; then
    echo 'Existing include differs; review manually.'
    exit 1
fi
printf '%s\n' "$EXPECTED" > "$INCLUDE"
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
install -o root -g root -m 644 "$SOURCE/powerhouse-hosting.service" /etc/systemd/system/powerhouse-hosting.service
install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\n/usr/sbin/nginx -t && /usr/bin/systemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/powerhouse-reload
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/powerhouse-reload
systemctl daemon-reload
systemctl enable powerhouse-hosting
systemctl restart powerhouse-hosting
echo 'Agent installed. Configure the application and start its dedicated worker/scheduler; see docs/domain-hosting.md.'
