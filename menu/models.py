"""
The product catalogue.

Mirrors the `menus` table in yellow_ribbon_pizza_schema.txt.
"""

from django.core.validators import MinValueValidator
from django.db import models


class Menu(models.Model):
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    photo = models.ImageField(
        upload_to="menu_photos/",
        max_length=255,
        blank=True,
        null=True,
    )
    is_available = models.BooleanField(
        default=True,
        help_text="Drives the Available / Unavailable badge in all three apps.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "menus"
        ordering = ["name"]
        indexes = [
            # Supports the admin search bar.
            models.Index(fields=["name"], name="idx_menu_name"),
        ]

    def __str__(self):
        return self.name
