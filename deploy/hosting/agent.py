#!/usr/bin/env python3
"""Root-owned Unix socket agent. No Django import, shell commands, or caller paths."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import struct
import subprocess
import tempfile

import dns.exception
import dns.resolver

CONFIG = Path('/etc/powerhouse-hosting.json')
MANAGED = Path('/etc/nginx/powerhouse-managed')
WEBROOT = Path('/var/lib/powerhouse-acme')


class SetupError(Exception):
    pass


def run(args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise SetupError('Hosting command failed. Check the root service and certificate logs, then retry.')
    return result.stdout


def hostname(value):
    if not isinstance(value, str) or len(value) > 253 or value != value.lower():
        raise SetupError('Invalid hostname.')
    labels = value.split('.')
    if len(labels) < 2 or labels[-1].isdigit() or labels[-1] in {'test', 'localhost', 'local', 'internal'} or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in labels
    ):
        raise SetupError('Invalid public hostname.')
    return value


def check_dns(host, token, config):
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5
    try:
        txt = resolver.resolve('_powerhouse-verification.' + host + '.', 'TXT')
        if not any(secrets.compare_digest(b''.join(row.strings).decode('ascii'), token) for row in txt):
            return 'Waiting for the matching ownership TXT record.'
        addresses = {str(row) for row in resolver.resolve(host + '.', 'A')}
        expected = str(ipaddress.IPv4Address(config['ipv4']))
        if addresses != {expected}:
            return 'Waiting for the A record to point only to the platform IPv4 address. Disable DNS proxying during setup.'
        try:
            if list(resolver.resolve(host + '.', 'AAAA')):
                return 'Remove the AAAA record during setup; this ingress is configured for IPv4.'
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            pass
    except (dns.exception.DNSException, UnicodeError):
        return 'Waiting for public DNS A and ownership TXT records to become readable.'
    return None


def refuse_existing_site(host, target):
    # nginx -T identifies every included file, including configs outside sites-enabled.
    output = run(['/usr/sbin/nginx', '-T'])
    sections = re.split(r"^# configuration file (.+):$", output, flags=re.MULTILINE)
    for index in range(1, len(sections), 2):
        current = Path(sections[index])
        if current == target:
            continue
        body = re.sub(r"#[^\n]*", "", sections[index + 1])
        for directive in re.findall(r"\bserver_name\s+([^;]+);", body):
            names = [name.strip('"').lower() for name in directive.split()]
            if any(name == host or
                   name.startswith('.') and (host == name[1:] or host.endswith(name)) or
                   name.startswith('*.') and host.endswith(name[1:]) or
                   name.endswith('.*') and host.startswith(name[:-1]) or
                   name.startswith('~') for name in names):
                raise SetupError('Hostname conflicts with an existing Nginx site. Ask the operator to review it; no existing site was changed.')


def config_text(host, tls=False, certbot_config_dir='/var/lib/powerhouse-certbot/config'):
    challenge = f'location ^~ /.well-known/acme-challenge/ {{ root {WEBROOT}; }}'
    proxy = '''location /static/ { alias /var/www/powerhouse/static/; }
    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 130s;
    }'''
    if not tls:
        # Before proof/certificate completion, serve only ACME challenges.
        return f'server {{ listen 80; server_name {host}; {challenge} location / {{ return 503; }} }}\n'
    return f'''server {{ listen 80; server_name {host}; {challenge}
        location / {{ return 301 https://{host}$request_uri; }} }}
server {{ listen 443 ssl; server_name {host};
    ssl_certificate {certbot_config_dir}/live/{host}/fullchain.pem;
    ssl_certificate_key {certbot_config_dir}/live/{host}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    {proxy}
}}
'''


def write_config(target, text):
    if target.is_symlink():
        raise SetupError('Managed configuration must not be a symlink.')
    previous = target.read_bytes() if target.exists() else None
    def replace(data):
        fd, name = tempfile.mkstemp(dir=MANAGED, prefix='.pending-')
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
            os.chmod(name, 0o644)
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    replace(text.encode())
    try:
        run(['/usr/sbin/nginx', '-t'])
        run(['/usr/bin/systemctl', 'reload', 'nginx'])
    except (SetupError, subprocess.TimeoutExpired):
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            replace(previous)
        # Restore the last valid on-disk configuration without restarting Nginx.
        run(['/usr/sbin/nginx', '-t'])
        run(['/usr/bin/systemctl', 'reload', 'nginx'])
        raise


def provision(payload, config):
    host = hostname(payload.get('hostname'))
    if host in config['reserved_hosts']:
        raise SetupError('This hostname is reserved for the platform.')
    token = payload.get('token')
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,64}', token):
        raise SetupError('Invalid ownership token.')
    waiting = check_dns(host, token, config)
    if waiting:
        return {'status': 'waiting', 'message': waiting}
    target = MANAGED / (hashlib.sha256(host.encode()).hexdigest() + '.conf')
    refuse_existing_site(host, target)
    # Do not downgrade an existing managed HTTPS site on a retry.
    cert = Path(config['certbot_config_dir']) / 'live' / host / 'fullchain.pem'
    if not cert.exists():
        write_config(target, config_text(host))
    run([config['certbot'], 'certonly',
         '--config-dir', config['certbot_config_dir'],
         '--work-dir', config['certbot_work_dir'],
         '--logs-dir', config['certbot_logs_dir'],
         '--webroot', '-w', str(WEBROOT),
         '--cert-name', host, '-d', host, '--non-interactive', '--agree-tos',
         '--email', config['email'], '--keep-until-expiring'], timeout=180)
    write_config(target, config_text(host, tls=True, certbot_config_dir=config['certbot_config_dir']))
    return {'status': 'live', 'message': 'DNS ownership confirmed. Nginx and HTTPS are configured.'}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != self.server.config['allowed_uid']:
            return
        try:
            raw = self.rfile.readline(2049)
            if len(raw) > 2048:
                raise SetupError('Request too large.')
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise SetupError('Invalid request.')
            result = provision(payload, self.server.config)
        except SetupError as error:
            result = {'status': 'failed', 'message': str(error)}
        except Exception:
            result = {'status': 'failed', 'message': 'Hosting setup failed. Ask the operator to check the agent and certificate logs.'}
        self.wfile.write((json.dumps(result) + '\n').encode())


def main():
    config = json.loads(CONFIG.read_text())
    ipaddress.IPv4Address(config['ipv4'])
    address = '/run/powerhouse-hosting/agent.sock'
    Path(address).unlink(missing_ok=True)
    with socketserver.UnixStreamServer(address, Handler) as server:
        server.config = config
        os.chown(address, 0, config['allowed_gid'])
        os.chmod(address, 0o660)
        server.serve_forever()


if __name__ == '__main__':
    main()
