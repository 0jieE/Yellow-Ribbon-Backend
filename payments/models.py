"""
Invoices.

Mirrors the `invoices` table in yellow_ribbon_pizza_schema.txt. Written by
the PayMongo webhook for GCash, or on rider completion for COD.
"""

from django.db import connection, models

from orders.models import Order

INVOICE_NUMBER_SEQUENCE = "payments_invoice_number_seq"


def next_invoice_number():
    """`INV-0001`. Same Postgres sequence trick as Order.order_number."""
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT nextval('{INVOICE_NUMBER_SEQUENCE}')")
        value = cursor.fetchone()[0]
    return f"INV-{value:04d}"


class Invoice(models.Model):
    # OneToOne is the backbone of webhook idempotency: PayMongo retries, and
    # the database itself refuses a second invoice for the same order.
    order = models.OneToOneField(
        Order,
        on_delete=models.PROTECT,
        related_name="invoice",
    )
    invoice_number = models.CharField(max_length=30, unique=True, editable=False)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    paymongo_payment_id = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text=(
            "PayMongo reference. Holds the source id (src_...) from checkout "
            "until the payment settles, then the payment id (pay_...). "
            "Always blank for COD."
        ),
    )
    paymongo_checkout_url = models.CharField(max_length=500, blank=True)

    # Null means issued but not yet settled — a GCash checkout the customer
    # has not completed.
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "invoices"
        ordering = ["-created_at"]

    def __str__(self):
        return self.invoice_number

    @property
    def is_paid(self):
        return self.paid_at is not None
