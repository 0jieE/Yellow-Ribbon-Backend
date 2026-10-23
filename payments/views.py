"""Payment endpoints (spec section 7)."""

import json
import logging

from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status as http
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from core.permissions import IsCustomerUser
from orders.constants import OrderStatus, PaymentStatus, PaymentType
from orders.models import Order

from .gateway import (
    PayMongoError,
    create_gcash_source,
    create_payment_from_source,
    retrieve_source_status,
)
from .models import Invoice
from .services import issue_invoice, mark_payment_failed, settle_payment
from .signature import verify_signature

logger = logging.getLogger(__name__)

# Events we act on. Anything else is acknowledged and ignored — PayMongo
# treats a non-2xx as a failure and will keep retrying.
PAID_EVENTS = {"payment.paid"}
CHARGEABLE_EVENTS = {"source.chargeable"}
FAILED_EVENTS = {"payment.failed", "source.failed"}


@api_view(["POST"])
@permission_classes([IsCustomerUser])
def checkout(request):
    """
    POST /api/payments/checkout/ — body {"order_id": N}

    Creates a GCash source for an order that is waiting on payment and
    returns the checkout_url for the app to open.
    """
    order_id = request.data.get("order_id")
    if order_id is None:
        return Response(
            {"error": "order_id is required."}, status=http.HTTP_400_BAD_REQUEST
        )

    order = Order.objects.filter(
        pk=order_id, customer__user=request.user
    ).first()
    if order is None:
        return Response(
            {"error": "No Order matches the given query."},
            status=http.HTTP_404_NOT_FOUND,
        )

    if order.payment_type != PaymentType.GCASH:
        return Response(
            {"error": "This order is cash on delivery; there is nothing to pay online."},
            status=http.HTTP_400_BAD_REQUEST,
        )

    if order.status != OrderStatus.PENDING_PAYMENT:
        return Response(
            {
                "error": (
                    f"{order.order_number} is {order.get_status_display().lower()} "
                    "and is not awaiting payment."
                )
            },
            status=http.HTTP_400_BAD_REQUEST,
        )

    existing = Invoice.objects.filter(order=order).first()
    if existing is not None and existing.paid_at is not None:
        return Response(
            {"error": f"{order.order_number} has already been paid."},
            status=http.HTTP_400_BAD_REQUEST,
        )
    if existing is not None and existing.paymongo_checkout_url:
        source_status = "pending"
        if existing.paymongo_payment_id.startswith("src_"):
            try:
                source_status = retrieve_source_status(existing.paymongo_payment_id)
            except PayMongoError as exc:
                logger.warning(
                    "Could not check the source for %s: %s", order.order_number, exc
                )
                return Response({"error": str(exc)}, status=http.HTTP_502_BAD_GATEWAY)

        if source_status == "pending":
            # Reuse the live source rather than billing a new one every tap.
            return Response(
                {
                    "order_id": order.pk,
                    "order_number": order.order_number,
                    "invoice_number": existing.invoice_number,
                    "amount": str(order.total),
                    "checkout_url": existing.paymongo_checkout_url,
                    "reused": True,
                }
            )

        if source_status in ("chargeable", "paid"):
            # Authorised already; the webhook is about to settle it. A second
            # source here could charge the customer twice.
            return Response(
                {
                    "error": (
                        "Your GCash payment went through and is being "
                        "confirmed. Refresh the order in a moment."
                    )
                },
                status=http.HTTP_409_CONFLICT,
            )

        # cancelled / expired / failed: that link is dead. Fall through and
        # issue a fresh source on the same invoice.

    # PayMongo needs http(s) redirect targets. When none are configured we
    # derive them from this request, so the URLs are correct whether the API
    # is reached on localhost or through a public tunnel.
    success_url = settings.PAYMONGO_SUCCESS_REDIRECT_URL or request.build_absolute_uri(
        reverse("payments:return-success")
    )
    failed_url = settings.PAYMONGO_FAILED_REDIRECT_URL or request.build_absolute_uri(
        reverse("payments:return-failed")
    )

    try:
        source = create_gcash_source(order, success_url, failed_url)
    except PayMongoError as exc:
        logger.warning("Checkout failed for %s: %s", order.order_number, exc)
        return Response({"error": str(exc)}, status=http.HTTP_502_BAD_GATEWAY)

    invoice, created = issue_invoice(
        order,
        source_id=source["source_id"],
        checkout_url=source["checkout_url"],
    )
    if not created:
        # A retry: point the invoice at the new source so its webhook
        # matches this order instead of arriving unmatched.
        invoice.paymongo_payment_id = source["source_id"]
        invoice.paymongo_checkout_url = source["checkout_url"]
        invoice.save(
            update_fields=["paymongo_payment_id", "paymongo_checkout_url"]
        )

    if order.payment_status == PaymentStatus.FAILED:
        # A fresh attempt, so the earlier failure no longer describes it.
        order.payment_status = PaymentStatus.UNPAID
        order.save(update_fields=["payment_status"])

    return Response(
        {
            "order_id": order.pk,
            "order_number": order.order_number,
            "invoice_number": invoice.invoice_number,
            "amount": str(order.total),
            "checkout_url": source["checkout_url"],
            "reused": False,
        },
        status=http.HTTP_201_CREATED,
    )


RETURN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:0;min-height:100vh;display:flex;
      align-items:center;justify-content:center;background:#faf7f0;color:#2b2b2b}}
 .card{{max-width:22rem;padding:2rem;text-align:center}}
 .mark{{font-size:3rem;line-height:1}}
 h1{{font-size:1.25rem;margin:.75rem 0 .5rem}}
 p{{margin:0;color:#5b5b5b;line-height:1.5}}
 a{{display:inline-block;margin-top:1.5rem;padding:.7rem 1.4rem;border-radius:.5rem;
    background:#e8b800;color:#2b2b2b;text-decoration:none;font-weight:600}}
</style></head>
<body><div class="card">
  <div class="mark">{mark}</div>
  <h1>{title}</h1>
  <p>{message}</p>
  <a href="{deep_link}">Return to the app</a>
</div>
<script>setTimeout(function(){{location.href="{deep_link}";}},600);</script>
</body></html>"""


@csrf_exempt
def return_success(_request):
    """
    Where PayMongo sends the customer after a successful GCash payment.

    This page is cosmetic — the order is settled by the webhook, not by the
    customer's browser landing here. It exists because PayMongo requires an
    http(s) redirect target, and it hands off to the app via deep link.
    """
    return HttpResponse(
        RETURN_PAGE.format(
            mark="&#10004;",
            title="Payment received",
            message=(
                "Thanks! Your order is being confirmed. You can close this "
                "page and go back to the Yellow Ribbon app."
            ),
            deep_link="yellowribbon://payment/success",
        )
    )


@csrf_exempt
def return_failed(_request):
    """Where PayMongo sends the customer after a failed or cancelled payment."""
    return HttpResponse(
        RETURN_PAGE.format(
            mark="&#10007;",
            title="Payment not completed",
            message=(
                "Your order has not been paid for. Open the app to try again "
                "or cancel the order."
            ),
            deep_link="yellowribbon://payment/failed",
        )
    )


def _collect_ids(node, found):
    """Walk a PayMongo event payload collecting every src_/pay_ id in it."""
    if isinstance(node, dict):
        identifier = node.get("id")
        if isinstance(identifier, str) and identifier.startswith(("src_", "pay_")):
            found.append(identifier)
        for value in node.values():
            _collect_ids(value, found)
    elif isinstance(node, list):
        for value in node:
            _collect_ids(value, found)
    return found


def _find_invoice(event_data):
    """
    Match an event back to an invoice.

    Checkout stores the source id on the invoice, so a later payment.paid —
    which references that source — still resolves to the right order.
    """
    ids = _collect_ids(event_data, [])
    if not ids:
        return None, []

    queryset = Invoice.objects.select_related("order")

    invoice = queryset.filter(paymongo_payment_id__in=ids).first()
    if invoice is not None:
        return invoice, ids

    # Once settled, paymongo_payment_id holds the payment id, so a retried
    # source.chargeable no longer matches on it. The checkout URL still
    # carries the source id, which keeps that retry on the explicit
    # "already paid" path instead of falling through as unmatched.
    for identifier in ids:
        if identifier.startswith("src_"):
            invoice = queryset.filter(
                paymongo_checkout_url__contains=identifier
            ).first()
            if invoice is not None:
                return invoice, ids

    return None, ids


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def webhook(request):
    """
    POST /api/payments/webhook/

    Unauthenticated and CSRF-exempt by design (spec section 9): the
    Paymongo-Signature header is the authentication. No JWT here — PayMongo
    has no token to present.
    """
    # Read the raw bytes before anything parses the stream. Re-serialising
    # the JSON would change the signed payload and break verification.
    raw_body = request.body

    if not verify_signature(
        raw_body,
        request.META.get("HTTP_PAYMONGO_SIGNATURE", ""),
        settings.PAYMONGO_WEBHOOK_SECRET,
        settings.PAYMONGO_WEBHOOK_TOLERANCE_SECONDS,
    ):
        # Nothing in the body has been trusted at this point.
        return Response(
            {"error": "Invalid signature."}, status=http.HTTP_401_UNAUTHORIZED
        )

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        return Response({"error": "Malformed JSON."}, status=http.HTTP_400_BAD_REQUEST)

    attributes = ((payload.get("data") or {}).get("attributes")) or {}
    event_type = attributes.get("type", "")
    event_data = attributes.get("data") or {}

    if event_type not in (PAID_EVENTS | CHARGEABLE_EVENTS | FAILED_EVENTS):
        # 200 so PayMongo stops retrying an event we simply do not handle.
        logger.info("Ignoring unhandled PayMongo event '%s'.", event_type)
        return Response({"status": "ignored", "event": event_type})

    invoice, ids = _find_invoice(event_data)
    if invoice is None:
        # Acknowledge: retrying will not conjure an order we never issued.
        logger.warning(
            "PayMongo event '%s' matched no invoice (ids=%s).", event_type, ids
        )
        return Response({"status": "unmatched", "event": event_type})

    order = invoice.order

    if event_type in FAILED_EVENTS:
        mark_payment_failed(order)
        return Response({"status": "failed", "order_number": order.order_number})

    if invoice.paid_at is not None:
        # The idempotency guarantee: a retry of an already-settled event
        # changes nothing and creates no second invoice.
        logger.info(
            "Duplicate '%s' for %s ignored; already settled.",
            event_type,
            order.order_number,
        )
        return Response(
            {"status": "already_paid", "order_number": order.order_number}
        )

    payment_id = ""
    if event_type in CHARGEABLE_EVENTS:
        # A chargeable source is authorisation, not money. Collect it.
        source_id = next((i for i in ids if i.startswith("src_")), None)
        try:
            payment = create_payment_from_source(
                source_id, order.total, description=order.order_number
            )
            payment_id = payment["payment_id"]
        except PayMongoError as exc:
            # 502 asks PayMongo to retry; the order stays pending_payment.
            logger.warning(
                "Could not collect payment for %s: %s", order.order_number, exc
            )
            return Response(
                {"error": "Could not create the payment."},
                status=http.HTTP_502_BAD_GATEWAY,
            )
    else:
        payment_id = next((i for i in ids if i.startswith("pay_")), "")

    invoice, changed = settle_payment(order, payment_id=payment_id)

    return Response(
        {
            "status": "paid" if changed else "already_paid",
            "order_number": order.order_number,
            "invoice_number": invoice.invoice_number,
        }
    )
