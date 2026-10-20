from django.db import models


class Location(models.Model):
    """A branch in the currently selected gym database."""

    name = models.CharField(max_length=200, unique=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
