from django import forms
from .models import Organization


class GymForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ("name", "slug")
        labels = {"name": "Gym name", "slug": "Unique gym identifier"}
