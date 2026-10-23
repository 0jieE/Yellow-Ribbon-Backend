"""
Refunds for paid online orders that get cancelled.

The rule the rest of the system relies on: **a cancel is never lost because a
refund failed, and a refund is never made for a cancel that did not stick.**

* `schedule_refund` runs inside the cancel's own transaction. It only records
  the intent (refund_status=pending); no money moves yet.
* The gateway is called after that transaction commits (`transaction.on_commit`),
  so a rollback cannot leave the customer refunded for a live order.
* If the call fails, the invoice says `failed` with the reason, the admin is
  told, and `retry_refund` (admin API / admin action) tries again. A refund
  left `pending` by a crash is picked up by the same retry.
"""

import logging

from django.db import transaction
from django.utils import timezone

from core.notifications import notify_admins, notify_refund
from orders.constants import PaymentStatus

from . import gateway, maya
from .models import Invoice, Provider, RefundStatus

logger = logging.getLogger(__name__)

ONLINE_PROVIDERS = {Provider.PAYMONGO, Provider.MAYA}


class RefundError(Exception):
    """A refund that cannot go ahead, with a message an admin can act on."""


def is_refundable(invoice):
    """Paid through a gateway, and not already refunded."""
    return (
        invoice is not None
        and invoice.paid_at is not None
        and invoice.provider in ONLINE_PROVIDERS
        and invoice.refund_status != RefundStatus.REFUNDED
    )


def schedule_refund(order):
    """
    Queue a refund for a cancelled order that was paid online.

    Safe to call for any cancelled order: unpaid, COD and already-refunded
    orders are left alone. Returns True when a refund was queued.
    """
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().filter(order=order).first()
        if not is_refundable(invoice) or invoice.refund_status != RefundStatus.NONE:
            return False

        invoice.refund_status = RefundStatus.PENDING
        invoice.refund_amount = invoice.amount_paid
        invoice.refund_error = ""
        invoice.save(update_fields=["refund_status", "refund_amount", "refund_error"])

        pk = invoice.pk
        transaction.on_commit(lambda: process_refund(pk))
    return True


def _reason(invoice):
    return f"Order {invoice.order.order_number} cancelled"


def _request_gateway(invoice):
    """Ask the gateway to refund (or check on a refund already asked for)."""
    amount = invoice.refund_amount or invoice.amount_paid

    if invoice.provider == Provider.PAYMONGO:
        if invoice.refund_reference:
            return gateway.retrieve_refund(invoice.refund_reference)
        if not invoice.paymongo_payment_id.startswith("pay_"):
            raise RefundError(
                "This invoice has no PayMongo payment id, so there is nothing to refund yet."
            )
        return gateway.create_refund(invoice.paymongo_payment_id, amount, _reason(invoice))

    if invoice.provider == Provider.MAYA:
        if not invoice.provider_rrn:
            raise RefundError("This invoice has no Maya reference to refund against.")
        payment = maya.retrieve_payment(invoice.provider_rrn)

        if invoice.refund_reference:
            # Already asked: Maya's payment now reads REFUNDED or VOIDED.
            returned = payment["status"] in maya.RETURNED_STATES
            return {
                "reference": invoice.refund_reference,
                "outcome": "refunded" if returned else "pending",
            }

        # Maya says which one applies: a same-day payment can only be voided,
        # an older one only refunded.
        if payment["can_void"]:
            return maya.void_payment(invoice.provider_rrn, _reason(invoice))
        if payment["can_refund"]:
            return maya.refund_payment(invoice.provider_rrn, amount, _reason(invoice))
        raise RefundError(
            "Maya cannot void or refund this payment right now "
            f"(its status is {payment['status'] or 'unknown'}). Try again later."
        )

    raise RefundError(f"{invoice.provider or 'This'} payments cannot be refunded online.")


def _record(invoice_pk, outcome, reference="", error=""):
    """Write the result down. Returns (invoice, notify_what)."""
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().select_related("order").get(pk=invoice_pk)
        previous = invoice.refund_status
        had_reference = bool(invoice.refund_reference)
        if previous == RefundStatus.REFUNDED:
            return invoice, None  # a concurrent run already finished it

        invoice.refund_status = outcome
        invoice.refund_error = error[:255]
        if reference:
            invoice.refund_reference = reference
        fields = ["refund_status", "refund_error", "refund_reference"]

        if outcome == RefundStatus.REFUNDED:
            invoice.refunded_at = timezone.now()
            invoice.refund_amount = invoice.refund_amount or invoice.amount_paid
            fields += ["refunded_at", "refund_amount"]

            order = invoice.order
            order.payment_status = PaymentStatus.REFUNDED
            order.save(update_fields=["payment_status"])

        invoice.save(update_fields=fields)

    notify = None
    if outcome == RefundStatus.FAILED:
        notify = "failed"
    elif outcome == RefundStatus.REFUNDED:
        notify = "refunded"
    elif outcome == RefundStatus.PENDING and reference and not had_reference:
        # The gateway has just accepted it. (`pending` alone proves nothing:
        # schedule_refund sets that before any gateway has heard of it.)
        notify = "pending"
    return invoice, notify


def process_refund(invoice_pk):
    """
    Carry out (or follow up) the refund queued on an invoice.

    Never raises: any failure is recorded on the invoice for the admin to
    retry. Returns the invoice.
    """
    invoice = Invoice.objects.select_related("order").get(pk=invoice_pk)
    if invoice.refund_status == RefundStatus.REFUNDED:
        return invoice

    try:
        result = _request_gateway(invoice)
        outcome, reference, error = result["outcome"], result.get("reference", ""), ""
        if outcome == RefundStatus.FAILED:
            error = "The gateway reported the refund as failed."
    except (gateway.PayMongoError, maya.MayaError, RefundError) as exc:
        logger.warning("Refund for %s failed: %s", invoice.order.order_number, exc)
        outcome, reference, error = RefundStatus.FAILED, "", str(exc)
    except Exception as exc:  # a bug must not strand a paid, cancelled order
        logger.exception("Unexpected error refunding %s", invoice.order.order_number)
        outcome, reference, error = RefundStatus.FAILED, "", f"Unexpected error: {exc}"

    invoice, notify = _record(invoice.pk, outcome, reference, error)

    if notify == "failed":
        notify_admins(
            "Refund failed",
            f"{invoice.order.order_number}: {invoice.refund_error or 'the refund did not go through'}. "
            "Open the order and retry.",
            {"type": "order", "event": "refund_failed", "order_id": invoice.order_id},
        )
    elif notify:
        notify_refund(invoice.order, invoice)
    return invoice


def retry_refund(order):
    """
    Admin retry: try the refund again now, synchronously.

    Works on a failed refund, a pending one (to ask the gateway how it is
    getting on), and on a cancelled paid order that never got one queued.
    """
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().filter(order=order).first()
        if invoice is None or invoice.paid_at is None:
            raise RefundError(f"{order.order_number} was never paid, so there is nothing to refund.")
        if invoice.provider not in ONLINE_PROVIDERS:
            raise RefundError("Cash on delivery payments are not refunded online.")
        if invoice.refund_status == RefundStatus.REFUNDED:
            raise RefundError(f"{order.order_number} has already been refunded.")
        if order.status != "cancelled":
            raise RefundError("Only a cancelled order can be refunded. Cancel it first.")

        if invoice.refund_status == RefundStatus.FAILED:
            invoice.refund_status = RefundStatus.PENDING
            invoice.refund_error = ""
            # A failed refund's reference is dead; ask for a fresh one.
            invoice.refund_reference = ""
        elif invoice.refund_status == RefundStatus.NONE:
            invoice.refund_status = RefundStatus.PENDING
            invoice.refund_amount = invoice.amount_paid
        invoice.save(
            update_fields=["refund_status", "refund_error", "refund_amount", "refund_reference"]
        )
        pk = invoice.pk

    return process_refund(pk)
