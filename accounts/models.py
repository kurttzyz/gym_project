from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Platform identity; organization access is granted through memberships."""
