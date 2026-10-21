from django import forms
from .models import NotificationSettings
from .services import validated_origin
from gym_core.members.models import MemberProfile


class NotificationSettingsForm(forms.ModelForm):
    class Meta:
        model = NotificationSettings
        fields = ['email_enabled','public_origin','owner_email','bookings','class_reminders',
                  'membership_updates','membership_reminders','payment_confirmations','owner_alerts']
        labels = {'bookings':'Class confirmations, waitlists and cancellations',
                  'class_reminders':'Upcoming class reminders', 'owner_alerts':'New applications and payment issue alerts'}

    def __init__(self,*args,gym,**kwargs):
        self.gym = gym
        super().__init__(*args,**kwargs)

    def clean_public_origin(self):
        value = self.cleaned_data['public_origin']
        if value:
            return validated_origin(self.gym,value)
        return value

    def clean(self):
        data = super().clean()
        if data.get('email_enabled') and not data.get('public_origin'):
            self.add_error('public_origin','A verified gym website origin is required to enable email.')
        return data


class MemberEmailForm(forms.ModelForm):
    class Meta:
        model = MemberProfile
        fields = ['email_notifications']
        labels = {'email_notifications':'Receive gym notifications and reminders by email'}
