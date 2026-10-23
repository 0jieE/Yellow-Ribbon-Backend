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
from orders.constants import OrderStatus, PaymentStatus, PaymentType

from . import maya
from .fees import split
from .models import Invoice, Provider, next_invoice_number

logger = logging.getLogger(__name__)

PROVIDER_BY_PAYMENT_TYPE = {
    PaymentType.GCASH: Provider.PAYMONGO,
    PaymentType.MAYA: Provider.MAYA,
    PaymentType.COD: Provider.COD,
}


def provider_for(order):
    return PROVIDER_BY_PAYMENT_TYPE.get(order.payment_type, "")


@transaction.atomic
def issue_invoice(order, *, source_id="", checkout_url="", rrn=""):
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
            "provider": provider_for(order),
            "provider_rrn": rrn,
        },
    )
    return invoice, created


@transaction.atomic
def settle_payment(order, *, amount=None, payment_id="", changed_by=None, payer=""):
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
            "provider": provider_for(order),
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
    invoice.provider = invoice.provider or provider_for(order)
    if payer:
        invoice.payer = payer
    # Snapshot what the gateway keeps, at today's rates, for the earnings report.
    invoice.fee, invoice.net_amount = split(invoice.provider, invoice.amount_paid)
    invoice.save(
        update_fields=[
            "amount_paid",
            "paid_at",
            "paymongo_payment_id",
            "provider",
            "payer",
            "fee",
            "net_amount",
        ]
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

    # Paid after the order was cancelled (the customer finished checking out
    # after it timed out, or after cancelling): the shop owes the money back.
    if order.status == OrderStatus.CANCELLED:
        from .refunds import schedule_refund

        logger.warning("%s was paid after it was cancelled; refunding.", order.order_number)
        schedule_refund(order)

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



class SyncResult(dict):
    """What a sync found: state, whether this call settled it, gateway status."""


def sync_maya_order(order):
    """
    Ask Maya where this order's payment stands and act on the answer.

    Maya has no webhook here, so this is how a Maya payment ever settles: the
    return page, the app's poll and the stale-order reaper all call it. It
    settles only when Maya itself reports the payment paid for the right amount.

    Returns SyncResult(state=..., settled=bool, gateway_status=str) where
    state is one of: no_checkout, paid, failed, pending.
    Raises maya.MayaError when Maya cannot be asked.
    """
    invoice = Invoice.objects.filter(order=order, provider=Provider.MAYA).first()
    if invoice is None or not invoice.provider_rrn:
        return SyncResult(state="no_checkout", settled=False, gateway_status="")

    if invoice.paid_at is not None:
        return SyncResult(state="paid", settled=False, gateway_status="PAYMENT_SUCCESS")

    payment = maya.retrieve_payment(invoice.provider_rrn)

    if payment["state"] == "paid":
        if payment["amount"] != order.total:
            # Never settle a different amount than the order's total.
            logger.error(
                "Maya reports %s paid for %s but the order total is %s; not settling.",
                payment["amount"],
                order.order_number,
                order.total,
            )
            return SyncResult(
                state="failed", settled=False, gateway_status="AMOUNT_MISMATCH"
            )
        _, changed = settle_payment(
            order, amount=order.total, payment_id=payment["id"], payer=payment["payer"]
        )
        return SyncResult(state="paid", settled=changed, gateway_status=payment["status"])

    if payment["state"] == "failed":
        mark_payment_failed(order)

    return SyncResult(state=payment["state"], settled=False, gateway_status=payment["status"])
