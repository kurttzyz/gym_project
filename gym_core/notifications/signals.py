from django.db.models.signals import post_save
from django.dispatch import receiver
from config.database_router import gym_database
from gym_core.classes.models import BookingEvent
from gym_core.memberships.models import MembershipApplication
from payments.models import PaymentOrder
from .services import booking_event, payment_event, queue_email


@receiver(post_save, sender=BookingEvent, dispatch_uid='notifications.booking_event')
def on_booking_event(sender, instance, created, using, raw=False, **kwargs):
    if created and not raw and using.startswith('gym_'):
        with gym_database(using):
            booking_event(using, instance)


@receiver(post_save, sender=PaymentOrder, dispatch_uid='notifications.payment_event')
def on_payment_event(sender, instance, using, raw=False, **kwargs):
    if not raw and using.startswith('gym_'):
        with gym_database(using):
            payment_event(using, instance)
            from gym_core.renewal_billing.services import sync_invoice_status
            sync_invoice_status(using,instance)


@receiver(post_save, sender=MembershipApplication, dispatch_uid='notifications.application_event')
def on_application(sender, instance, created, using, raw=False, **kwargs):
    if created and not raw and using.startswith('gym_'):
        queue_email(using, key=f'application:{instance.pk}', category='owner_alerts', title='New membership application',
            message=f'{instance.full_name} applied for {instance.quoted_plan_name}. Review the application in your gym dashboard.',
            path='/manage/applications/')
