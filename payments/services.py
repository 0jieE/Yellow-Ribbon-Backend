"""
Invoice writing and the GCash payment gate.

Everything here is idempotent. PayMongo retries webhooks, and a duplicate
event must never produce a second invoice, a second status log, or a
double-counted payment.
"""

import logging

from django.db import transaction
from django.utils import timezone

from core.notifications import notify_new_order
from orders.admin_api import apply_status_change
from orders.constants import OrderStatus, PaymentStatus

from .models import Invoice, next_invoice_number

logger = logging.getLogger(__name__)


@transaction.atomic
def issue_invoice(order, *, source_id="", checkout_url=""):
    """
    Create the unpaid invoice that a GCash checkout hangs off.

    Returns (invoice, created). Called again for the same order, it returns
    the existing invoice so repeated checkout taps reuse one PayMongo source
    instead of spawning a new one each time.
    """
    invoice, created = Invoice.objects.get_or_create(
        order=order,
        defaults={
            "invoice_number": next_invoice_number(),
            "amount_paid": 0,
            "paymongo_payment_id": source_id,
            "paymongo_checkout_url": checkout_url,
        },
    )
    return invoice, created


@transaction.atomic
def settle_payment(order, *, amount=None, payment_id="", changed_by=None):
    """
    Mark an order paid and open the kitchen gate. Idempotent.

    Returns (invoice, changed). `changed` is False when this order was
    already settled, which is the normal answer to a PayMongo retry.
    """
    # Serialise concurrent webhook deliveries for the same order.
    order = type(order).objects.select_for_update().get(pk=order.pk)

    invoice, _ = Invoice.objects.select_for_update().get_or_create(
        order=order,
        defaults={
            "invoice_number": next_invoice_number(),
            "amount_paid": 0,
        },
    )

    if invoice.paid_at is not None:
        logger.info(
            "Ignoring duplicate settlement for %s; %s is already paid.",
            order.order_number,
            invoice.invoice_number,
        )
        return invoice, False

    invoice.amount_paid = order.total if amount is None else amount
    invoice.paid_at = timezone.now()
    if payment_id:
        invoice.paymongo_payment_id = payment_id
    invoice.save(
        update_fields=["amount_paid", "paid_at", "paymongo_payment_id"]
    )

    if order.payment_status != PaymentStatus.PAID:
        order.payment_status = PaymentStatus.PAID
        order.save(update_fields=["payment_status"])

    # The GCash gate: an unpaid order never reached the kitchen, and this is
    # the moment it does. A COD order settles at the door and is already
    # `delivered`, so it is left where it is.
    was_awaiting_payment = order.status == OrderStatus.PENDING_PAYMENT
    if was_awaiting_payment:
        apply_status_change(order, OrderStatus.PENDING, changed_by)

    logger.info(
        "Settled %s for %s (%s).", invoice.invoice_number, order.order_number, payment_id or "cod"
    )

    # A paid GCash order has just reached the kitchen for the first time, so
    # to the admin it is a new order. COD orders were announced at placement
    # and settle at the door, so they are not re-announced here.
    if was_awaiting_payment:
        notify_new_order(order)

    return invoice, True


@transaction.atomic
def mark_payment_failed(order):
    """A GCash source or payment that did not go through."""
    order = type(order).objects.select_for_update().get(pk=order.pk)

    invoice = Invoice.objects.filter(order=order).first()
    if invoice is not None and invoice.paid_at is not None:
        # A later success already landed; do not undo it.
        return False

    if order.payment_status != PaymentStatus.FAILED:
        order.payment_status = PaymentStatus.FAILED
        order.save(update_fields=["payment_status"])
    return True


def record_cod_payment(order, changed_by=None):
    """
    COD settles when the rider hands the food over. No PayMongo reference —
    `paymongo_payment_id` stays blank, exactly as the schema notes.
    """
    return settle_payment(order, payment_id="", changed_by=changed_by)
