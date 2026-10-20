"""Create the central database using the separately configured provisioner."""
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.conf import settings
from gyms.databases import create_database

if __name__ == "__main__":
    database = settings.DATABASES["default"]
    create_database(database["NAME"], database["USER"], database["PASSWORD"])
    print("Central PostgreSQL database is provisioned")
