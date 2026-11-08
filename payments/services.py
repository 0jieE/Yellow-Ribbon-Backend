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

from . import gateway, maya
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


# GCash source statuses, as PayMongo reports them.
GCASH_COLLECTED = {"paid", "consumed"}
GCASH_DEAD = {"cancelled", "expired", "failed"}


def collect_gcash_source(order, source_id):
    """
    Take the money for a chargeable GCash source and settle the order, once.

    The webhook (source.chargeable) and the app's sync both end up here, and
    may do so at the same moment. The order and invoice rows are locked, in
    the same order settle_payment locks them, while PayMongo is asked to
    collect, so whichever comes second finds the invoice paid and collects
    nothing.

    Returns (invoice, changed). Raises gateway.PayMongoError when PayMongo
    will not collect; nothing is written then.
    """
    with transaction.atomic():
        locked = type(order).objects.select_for_update().get(pk=order.pk)
        invoice = Invoice.objects.select_for_update().get(order=locked)
        if invoice.paid_at is not None:
            return invoice, False
        payment = gateway.create_payment_from_source(
            source_id, locked.total, description=locked.order_number
        )
        return settle_payment(locked, payment_id=payment["payment_id"])


def sync_gcash_order(order):
    """
    Ask PayMongo where this order's GCash payment stands and act on it.

    The webhook normally settles GCash, but a webhook can be missed (the
    server asleep, a wrong secret, PayMongo giving up on it). This lets the
    app's poll, the payment return and the stale-order reaper heal that. It
    acts only on what PayMongo itself reports, for the order's exact amount.

    Returns SyncResult like sync_maya_order, with gateway_status the source's
    status. Raises gateway.PayMongoError when PayMongo cannot be asked.
    """
    invoice = Invoice.objects.filter(order=order).first()
    if invoice is not None and invoice.paid_at is not None:
        return SyncResult(state="paid", settled=False, gateway_status="paid")
    source_id = invoice.paymongo_payment_id if invoice is not None else ""
    if not source_id.startswith("src_"):
        return SyncResult(state="no_checkout", settled=False, gateway_status="")

    source = gateway.retrieve_source(source_id)
    status = source["status"]

    if status == "chargeable" or status in GCASH_COLLECTED:
        if source["amount_centavos"] != gateway.to_centavos(order.total):
            logger.error(
                "PayMongo source %s for %s is for %s centavos but the order total is %s; "
                "not settling.",
                source_id, order.order_number, source["amount_centavos"], order.total,
            )
            return SyncResult(state="failed", settled=False, gateway_status="amount_mismatch")

    if status == "chargeable":
        _, changed = collect_gcash_source(order, source_id)
        return SyncResult(state="paid", settled=changed, gateway_status=status)

    if status in GCASH_COLLECTED:
        # Collected already, but whatever collected it never settled the order.
        payment = gateway.find_payment_for_source(source_id)
        if payment is None:
            logger.warning(
                "%s: source %s is %s but its payment was not found; settling without "
                "a payment id (a refund would need doing by hand).",
                order.order_number, source_id, status,
            )
        _, changed = settle_payment(order, payment_id=payment["payment_id"] if payment else "")
        return SyncResult(state="paid", settled=changed, gateway_status=status)

    if status in GCASH_DEAD:
        mark_payment_failed(order)
        return SyncResult(state="failed", settled=False, gateway_status=status)

    return SyncResult(state="pending", settled=False, gateway_status=status)


def sync_order_payment(order):
    """
    Re-check an unpaid online order with its gateway (Maya or GCash).

    Returns a SyncResult, or None for an order there is nothing to ask about
    (cash, or already paid). Raises maya.MayaError or gateway.PayMongoError
    when the gateway cannot be asked.
    """
    if order.payment_status == PaymentStatus.PAID:
        return None
    if order.payment_type == PaymentType.MAYA:
        return sync_maya_order(order)
    if order.payment_type == PaymentType.GCASH:
        return sync_gcash_order(order)
    return None


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
