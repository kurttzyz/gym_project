import os
from celery import Celery
os.environ.setdefault('DJANGO_SETTINGS_MODULE','platform_config.settings')
app = Celery('powerhouse')
app.config_from_object('django.conf:settings',namespace='CELERY')
app.conf.imports = ('gym_core.notifications.tasks', 'gyms.hosting', 'platform_core.collections.gateway')
