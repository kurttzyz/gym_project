from django import forms
from tenant_data.models import Location
from .models import Trainer, ClassSession


class TrainerForm(forms.ModelForm):
    class Meta:
        model = Trainer
        fields = ['name', 'specialty', 'bio', 'is_active']


class SessionForm(forms.ModelForm):
    class Meta:
        model = ClassSession
        fields = ['title', 'description', 'trainer', 'location', 'starts_at', 'ends_at', 'capacity']
        widgets = {name: forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'})
                   for name in ['starts_at', 'ends_at']}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['trainer'].queryset = Trainer.objects.filter(is_active=True)
        self.fields['location'].queryset = Location.objects.filter(is_active=True)


class CancelSessionForm(forms.Form):
    reason = forms.CharField(max_length=500, widget=forms.Textarea)
