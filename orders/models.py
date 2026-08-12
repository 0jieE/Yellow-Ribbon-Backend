"""
Orders, line items and the status audit trail.

Mirrors the `orders`, `order_items` and `order_status_logs` tables in
yellow_ribbon_pizza_schema.txt.
"""

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import connection, models

from accounts.models import Customer
from menu.models import Menu

from .constants import (
    CUSTOMER_CANCELLABLE_STATUSES,
    OrderStatus,
    PaymentStatus,
    PaymentType,
)

ORDER_NUMBER_SEQUENCE = "orders_order_number_seq"


def next_order_number():
    """
    `YR-0001`, zero-padded and sequential (spec section 4).

    Backed by a Postgres sequence rather than `MAX(order_number) + 1` so
    two customers checking out at the same moment cannot collide — nextval
    is atomic and never hands the same value to two transactions.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT nextval('{ORDER_NUMBER_SEQUENCE}')")
        value = cursor.fetchone()[0]
    return f"YR-{value:04d}"


class Order(models.Model):
    order_number = models.CharField(max_length=20, unique=True, editable=False)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="orders",
    )

    status = models.CharField(
        max_length=20,
        choices=OrderStatus.choices,
        default=OrderStatus.PENDING,
    )
    payment_type = models.CharField(max_length=10, choices=PaymentType.choices)
    payment_status = models.CharField(
        max_length=10,
        choices=PaymentStatus.choices,
        default=PaymentStatus.UNPAID,
    )

    # All three computed server-side from the line items. Never client-supplied.
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2)
    total = models.DecimalField(max_digits=10, decimal_places=2)

    delivery_address = models.CharField(max_length=255)
    delivery_latitude = models.DecimalField(max_digits=9, decimal_places=6)
    delivery_longitude = models.DecimalField(max_digits=9, decimal_places=6)
    delivery_notes = models.TextField(blank=True)

    eta_minutes = models.IntegerField(null=True, blank=True)
    eta_at = models.DateTimeField(null=True, blank=True)

    # Encoded polyline for the leg the ETA was measured over, straight from
    # Google Routes. Refreshed on the same triggers as the ETA. Not in the
    # DBML — added so the apps can draw the route without a second Maps call.
    route_polyline = models.TextField(blank=True, default="")

    placed_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "orders"
        ordering = ["-placed_at"]
        indexes = [
            models.Index(
                fields=["customer", "placed_at"], name="idx_order_customer_recent"
            ),
            models.Index(fields=["status"], name="idx_order_status"),
        ]

    def __str__(self):
        return self.order_number

    @property
    def is_cancellable_by_customer(self):
        """
        Cancellable until the store accepts the order.

        Includes `pending_payment` so an abandoned GCash checkout has a
        customer-facing exit instead of sitting there until an admin notices.
        """
        return self.status in CUSTOMER_CANCELLABLE_STATUSES


class OrderItem(models.Model):
    """
    `unit_price` is a snapshot taken at checkout. It is never re-read from
    the menu, so editing a price later cannot rewrite an old receipt.
    """

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    menu = models.ForeignKey(
        Menu,
        on_delete=models.PROTECT,
        related_name="order_items",
    )
    quantity = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1)]
    )
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_subtotal = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = "order_items"
        constraints = [
            models.UniqueConstraint(
                fields=["order", "menu"], name="idx_orderitem_unique_line"
            )
        ]

    def __str__(self):
        return f"{self.quantity}x {self.menu_id} on {self.order_id}"


class OrderStatusLog(models.Model):
    """Defensible timeline per order: placed, confirmed, picked up, delivered."""

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="status_logs"
    )
    status = models.CharField(max_length=20, choices=OrderStatus.choices)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_status_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "order_status_logs"
        ordering = ["changed_at", "id"]

    def __str__(self):
        return f"{self.order_id} -> {self.status}"
