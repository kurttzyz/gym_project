from django.core.validators import RegexValidator, URLValidator
from django.db import models


class WebsiteSettings(models.Model):
    """One editable website per gym database; template code is shared."""

    class Template(models.TextChoices):
        STRENGTH = "strength", "Strength — bold and dark"
        STUDIO = "studio", "Studio — bright and minimal"

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    display_name = models.CharField(max_length=200)
    template = models.CharField(max_length=16, choices=Template.choices, default=Template.STRENGTH)
    primary_color = models.CharField(max_length=7, default="#245ac5", validators=[
        RegexValidator(r"^#[0-9a-fA-F]{6}$", "Use a six-digit hex color, e.g. #245ac5.")
    ])
    logo_url = models.URLField(blank=True, validators=[URLValidator(schemes=["https"])])
    hero_image_url = models.URLField(blank=True, validators=[URLValidator(schemes=["https"])])
    tagline = models.CharField(max_length=120, default="Your space to grow stronger")
    hero_title = models.CharField(max_length=160, default="Train stronger. Live better.")
    hero_text = models.TextField(max_length=1200, default="Find your rhythm, build your confidence, and make progress with us.")
    about_title = models.CharField(max_length=120, default="More than a workout")
    about_text = models.TextField(max_length=4000, default="Welcome to our community. Get in touch to learn about our gym, training spaces, and memberships.")
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=50, blank=True)
    address = models.TextField(max_length=600, blank=True)
    opening_hours = models.TextField(max_length=600, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="one_website_per_gym")]

    def __str__(self):
        return self.display_name
