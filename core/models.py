"""
Store-wide settings the admin can change without a redeploy.

The delivery fee and the service area used to be environment variables, so
changing either meant editing Render and redeploying. They live here now.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

TWO_PLACES = Decimal("0.01")


def _percent_field(default, help_text):
    return models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal(default),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        help_text=help_text,
    )


def _peso_field(default, help_text, maximum="10000"):
    return models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal(default),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal(maximum))],
        help_text=help_text,
    )


class StoreSettings(models.Model):
    """
    The one row of store settings (pk is always 1).

    On first read the row is seeded from the old environment variables, so an
    existing deployment keeps behaving exactly as it did. After that the
    environment values are ignored: the database is the source of truth.
    """

    SINGLETON_PK = 1

    # -- delivery fee --------------------------------------------------

    delivery_fee = _peso_field(
        "30.00",
        "Flat delivery fee in pesos. With a per-km rate below it is the base "
        "charge. Orders keep the fee they were placed with.",
    )
    delivery_fee_per_km = _peso_field(
        "0.00",
        "Extra pesos per km from the store, added to the base fee. 0 means a "
        "flat fee, which is how the app behaves today.",
        maximum="1000",
    )

    # -- service area --------------------------------------------------

    service_area_city = models.CharField(
        max_length=100,
        default="Kidapawan City",
        help_text="Shown to customers who order from outside the area.",
    )
    service_area_center_lat = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        default=Decimal("7.0083"),
        validators=[MinValueValidator(Decimal("-90")), MaxValueValidator(Decimal("90"))],
        help_text="Latitude of the store. Also where delivery distances are measured from.",
    )
    service_area_center_lng = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        default=Decimal("125.0894"),
        validators=[MinValueValidator(Decimal("-180")), MaxValueValidator(Decimal("180"))],
        help_text="Longitude of the store.",
    )
    service_area_radius_km = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("8.00"),
        validators=[MinValueValidator(Decimal("0.1")), MaxValueValidator(Decimal("100"))],
        help_text="How far from the store we deliver, in km (0.1 to 100).",
    )

    # -- payment gateway fees ------------------------------------------

    paymongo_fee_percent = _percent_field(
        "2.50", "PayMongo's percentage fee on a GCash payment."
    )
    paymongo_fee_fixed = _peso_field("0.00", "PayMongo's fixed fee per payment, in pesos.")
    maya_fee_percent = _percent_field(
        "1.50", "Maya's percentage fee on a wallet payment."
    )
    maya_fee_fixed = _peso_field("0.00", "Maya's fixed fee per payment, in pesos.")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "store_settings"
        verbose_name = "store settings"
        verbose_name_plural = "store settings"

    def __str__(self):
        return "Store settings"

    # -- singleton -----------------------------------------------------

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Everything reads this row; deleting it would only make the next
        # read re-seed from the environment and silently undo the admin's edits.
        raise models.ProtectedError("Store settings cannot be deleted.", {self})

    @classmethod
    def load(cls):
        """The settings row, created from the environment values on first use."""
        obj, _ = cls.objects.get_or_create(
            pk=cls.SINGLETON_PK,
            defaults={
                "delivery_fee": Decimal(str(settings.DELIVERY_FEE)),
                "service_area_center_lat": Decimal(str(settings.SERVICE_AREA_CENTER_LAT)),
                "service_area_center_lng": Decimal(str(settings.SERVICE_AREA_CENTER_LNG)),
                "service_area_radius_km": Decimal(str(settings.SERVICE_AREA_RADIUS_KM)),
            },
        )
        return obj

    # -- derived values ------------------------------------------------

    @property
    def center(self):
        return float(self.service_area_center_lat), float(self.service_area_center_lng)

    @property
    def radius_km(self):
        return float(self.service_area_radius_km)

    def delivery_fee_for(self, distance_km=None):
        """
        The fee for an order `distance_km` from the store.

        Flat unless a per-km rate is set. Rounded to centavos.
        """
        fee = self.delivery_fee
        if self.delivery_fee_per_km and distance_km is not None:
            fee = fee + self.delivery_fee_per_km * Decimal(str(distance_km))
        return fee.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
