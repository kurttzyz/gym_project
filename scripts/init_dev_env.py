"""Create local PostgreSQL credentials once; never overwrite an existing .env."""
from pathlib import Path
import os
import secrets

root = Path(__file__).resolve().parent.parent
path = root / ".env"
if not path.exists():
    values = {
        "PGHOST": "127.0.0.1", "PGPORT": "5432", "PGDATABASE": "gym_platform",
        "PGUSER": "gym_platform", "PGPASSWORD": secrets.token_urlsafe(32),
        "PGADMIN_USER": "postgres", "PGADMIN_PASSWORD": secrets.token_urlsafe(32),
        "GYM_DATABASE_SECRET": secrets.token_urlsafe(48),
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(48), "DJANGO_DEBUG": "true",
    }
    with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as file:
        file.write("".join(f"{key}={value}\n" for key, value in values.items()))
    print("Created ignored .env with local development credentials")
else:
    print("Preserving existing .env")
