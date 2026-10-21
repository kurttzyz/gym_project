from django import forms
from .models import ContactMessage, WebsiteSettings


class WebsiteSettingsForm(forms.ModelForm):
    class Meta:
        model = WebsiteSettings
        fields = (
            "display_name", "template", "primary_color", "logo_url", "hero_image_url",
            "tagline", "hero_title", "hero_text", "about_title", "about_text",
            "contact_email", "contact_phone", "address", "opening_hours", "waiver_text",
        )
        widgets = {
            "primary_color": forms.TextInput(attrs={"type": "color"}),
            "hero_text": forms.Textarea(attrs={"rows": 3}),
            "about_text": forms.Textarea(attrs={"rows": 5}),
            "address": forms.Textarea(attrs={"rows": 3}),
            "opening_hours": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "logo_url": "Optional HTTPS URL to your logo.",
            "hero_image_url": "Optional HTTPS URL to your main website photo.",
            "template": "Layouts share the same content; switching preserves your edits.",
        }


class ContactForm(forms.ModelForm):
    consent = forms.BooleanField(label="I agree that this gym may contact me about this message.")
    website_url = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = ContactMessage
        fields = ("full_name", "email", "subject", "message")
        widgets = {"message": forms.Textarea(attrs={"rows": 5})}

    def clean_website_url(self):
        if self.cleaned_data.get("website_url"):
            raise forms.ValidationError("Please leave this field blank.")
        return ""
