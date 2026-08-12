"""
Users and role profiles.

Mirrors the `users`, `customers` and `riders` tables in
yellow_ribbon_pizza_schema.txt. One user table, three role flags (RBAC).
"""

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower

from .managers import UserManager


class User(AbstractUser):
    """AUTH_USER_MODEL. Role flags drive the DRF permission classes."""

    # Schema: email is unique and not null (Django's default is neither).
    email = models.EmailField("email address", max_length=254, unique=True)
    first_name = models.CharField("first name", max_length=100, blank=True)
    last_name = models.CharField("last name", max_length=100, blank=True)

    phone = models.CharField(
        max_length=20,
        blank=True,
        help_text="Fallback contact during delivery.",
    )

    is_admin = models.BooleanField(default=False)
    is_rider = models.BooleanField(default=False)
    is_customer = models.BooleanField(default=True)

    objects = UserManager()

    REQUIRED_FIELDS = ["email"]

    class Meta:
        db_table = "users"
        verbose_name = "user"
        verbose_name_plural = "users"
        constraints = [
            # `unique=True` on the field gives a case-*sensitive* index, so
            # Bob@x.com and bob@x.com could both exist. Login accepts an email
            # case-insensitively, and two accounts answering to the same
            # address would make that lookup ambiguous. The serializers
            # already check with iexact; this closes the paths that skip
            # them — createsuperuser, a data import, or two registrations
            # racing each other.
            models.UniqueConstraint(
                Lower("email"),
                name="uniq_user_email_ci",
            ),
        ]

    def __str__(self):
        return self.username

    @property
    def role(self):
        """Single-word role, handy for logging and the admin list view."""
        if self.is_admin:
            return "admin"
        if self.is_rider:
            return "rider"
        return "customer"


class DevicePlatform(models.TextChoices):
    ANDROID = "android", "Android"
    IOS = "ios", "iOS"


class DeviceToken(models.Model):
    """
    An FCM registration token for one installed app.

    One user may have several devices; a push targets every token they own.
    Tokens Firebase reports as unregistered are deleted rather than retried.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="device_tokens",
    )
    fcm_token = models.CharField(max_length=255, unique=True)
    platform = models.CharField(
        max_length=10,
        choices=DevicePlatform.choices,
        blank=True,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "device_tokens"
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.user_id}:{self.fcm_token[:12]}..."


class Customer(models.Model):
    """
    The saved address is a convenience default only. The authoritative
    delivery point lives on each order.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="customer",
    )
    default_address = models.CharField(max_length=255, blank=True)
    default_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    default_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "customers"

    def __str__(self):
        return f"Customer<{self.user.username}>"


class Rider(models.Model):
    """
    Live coordinates are overwritten on every ping, not appended — the
    location endpoint writes straight to these columns.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="rider",
    )
    vehicle_type = models.CharField(max_length=50, blank=True)
    plate_number = models.CharField(max_length=20, blank=True)
    is_available = models.BooleanField(
        default=False,
        help_text="Rider toggles on-duty. Drives GET /api/admin/riders/available/.",
    )
    current_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    current_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    last_location_update = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Stale timestamp means the rider app is not reporting.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "riders"

    def __str__(self):
        return f"Rider<{self.user.username}>"
