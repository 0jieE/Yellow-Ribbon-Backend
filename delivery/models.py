"""
Deliveries.

Mirrors the `deliveries` table in yellow_ribbon_pizza_schema.txt. Created by
POST /api/admin/orders/{id}/assign/. The rider-facing endpoints that drive
`status`, the location ping and the proof upload land in build step 6.
"""

from django.db import models
from django.db.models import Q

from accounts.models import Rider
from orders.constants import DeliveryStatus
from orders.models import Order


class Delivery(models.Model):
    order = models.OneToOneField(
        Order,
        on_delete=models.CASCADE,
        related_name="delivery",
    )
    rider = models.ForeignKey(
        Rider,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    status = models.CharField(
        max_length=20,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.ASSIGNED,
    )
    rider_earnings = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text=(
            "Snapshot taken at assignment. Historical pay must not shift when "
            "the rate changes."
        ),
    )
    proof_of_delivery = models.ImageField(
        upload_to="delivery_proofs/",
        max_length=255,
        blank=True,
        null=True,
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "deliveries"
        ordering = ["-assigned_at"]
        indexes = [
            models.Index(fields=["rider", "status"], name="idx_delivery_rider_active"),
        ]
        constraints = [
            # A rider may hold any number of deliveries at `assigned` and
            # `picked_up` — they batch them at the store — but only one leg
            # may be under way at a time. Enforced in the database as well as
            # the serializer, because two delivery cards can be tapped at
            # once and a Python-side check alone would race.
            models.UniqueConstraint(
                fields=["rider"],
                condition=Q(status=DeliveryStatus.IN_TRANSIT),
                name="uniq_rider_one_in_transit",
            ),
        ]
        verbose_name_plural = "deliveries"

    def __str__(self):
        return f"{self.order_id} -> rider {self.rider_id}"
