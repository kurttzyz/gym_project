from django.conf import settings
from django.core.checks import Error, register
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


@register()
def check_email_transport(app_configs,**kwargs):
    if not settings.NOTIFICATION_DELIVERY_ENABLED:
        return []
    errors = []
    if not settings.EMAIL_HOST:
        errors.append(Error('EMAIL_HOST is required when email delivery is enabled.',id='notifications.E001'))
    try:
        validate_email(settings.DEFAULT_FROM_EMAIL)
        if settings.DEFAULT_FROM_EMAIL.endswith('.invalid'):
            raise ValidationError('placeholder')
    except ValidationError:
        errors.append(Error('DEFAULT_FROM_EMAIL must be a verified sender email address, not a placeholder.',id='notifications.E002'))
    if settings.EMAIL_USE_TLS and settings.EMAIL_USE_SSL:
        errors.append(Error('Enable only one of EMAIL_USE_TLS and EMAIL_USE_SSL.',id='notifications.E003'))
    return errors
