"""Renew only the root-configured Powerhouse certificate profile."""
import json
from pathlib import Path
import subprocess


def main():
    config = json.loads(Path('/etc/powerhouse-hosting.json').read_text())
    subprocess.run([config['certbot'], 'renew', '--non-interactive',
        '--config-dir',config['certbot_config_dir'], '--work-dir',config['certbot_work_dir'],
        '--logs-dir',config['certbot_logs_dir']], check=True, timeout=1800)
    subprocess.run(['/usr/sbin/nginx','-t'],check=True,timeout=30)
    subprocess.run(['/usr/bin/systemctl','reload','nginx'],check=True,timeout=30)


if __name__ == '__main__':
    main()
