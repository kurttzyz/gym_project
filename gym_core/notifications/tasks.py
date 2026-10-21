import logging
from celery import shared_task
from django.db import connections
from django.utils import timezone
from gyms.models import Organization
from gyms.databases import ready_database
from platform_core.billing.services import access_for_gym
from .models import NotificationSettings
from .reminders import queue_reminders
from .delivery import deliver_batch

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def sweep_notifications():
    # Only registered gym UUIDs travel through the broker, never DB aliases,
    # addresses, message bodies, or connection credentials.
    for key in Organization.objects.filter(database_ready=True).values_list('database_key',flat=True).iterator(chunk_size=100):
        process_gym_notifications.delay(str(key))


@shared_task(ignore_result=True)
def process_gym_notifications(database_key):
    gym = Organization.objects.filter(database_key=database_key,database_ready=True).first()
    if gym is None:
        return
    alias = None
    try:
        access = access_for_gym(gym)
        alias = ready_database(gym)
        if access.allowed and access.modules.get('memberships',False):
            queue_reminders(alias)
        deliver_batch(gym,alias,limit=25)
        NotificationSettings.objects.using(alias).filter(pk=1).update(last_worker_check=timezone.now())
    except Exception:
        # Isolate missing migrations and DB outages. Exception text can expose
        # connection credentials or private provider responses, so don't log it.
        logger.warning('Notification sweep unavailable for gym %s',gym.database_key)
    finally:
        if alias:
            connections[alias].close()
