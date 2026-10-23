"""
Maya Checkout REST client (sandbox by default).

How this differs from PayMongo:

* No webhook. The sandbox keys are shared by every developer, so a callback
  URL registered on them would receive everyone's payments. Instead the
  server looks the payment up with the secret key when the customer returns
  (and when the app asks), and settles only if Maya itself says it is paid.
* Payments are found by a reference number we generate (`requestReference
  Number`, "rrn"), which only we know. Never by anything the client sends.

Which key does what (checked against the sandbox):
  create a checkout  -> public key  (pk-...)
  every lookup, void, refund -> secret key (sk-...); the public key is refused.

The secret key never leaves the server and is never logged.
"""

import base64
import logging
from datetime import datetime
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Maya's own states, grouped by what they mean to us.
PAID_STATES = {"PAYMENT_SUCCESS", "DONE", "CAPTURED"}
FAILED_STATES = {
    "PAYMENT_FAILED",
    "PAYMENT_EXPIRED",
    "PAYMENT_CANCELLED",
    "PAYMENT_INVALID",
    "AUTH_FAILED",
    "ACCOUNT_ABUSE",
}
# Paid, then reversed. Not something to settle an order against.
RETURNED_STATES = {"REFUNDED", "VOIDED"}

# A checkout link dies an hour after creation; reuse it only while it has
# comfortably longer than that to live.
CHECKOUT_LIFETIME_MINUTES = 60
REUSE_BEFORE_MINUTES = 50


class MayaError(Exception):
    """A Maya call failed. Carries a message safe to show an admin."""

    def __init__(self, message, *, status_code=None, code=""):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _auth_header(key):
    if not key:
        raise MayaError("Maya is not configured.")
    return "Basic " + base64.b64encode(f"{key}:".encode()).decode()


def _send(method, path, *, key, json=None):
    url = f"{settings.MAYA_BASE_URL.rstrip('/')}{path}"
    try:
        response = requests.request(
            method,
            url,
            headers={"Authorization": _auth_header(key), "Content-Type": "application/json"},
            json=json,
            timeout=settings.MAYA_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        logger.warning("Maya timed out calling %s.", path)
        raise MayaError("Maya did not respond. Please try again.")
    except requests.RequestException as exc:
        logger.warning("Maya request to %s failed: %s", path, exc)
        raise MayaError("Could not reach Maya. Please try again.")

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code >= 400:
        # Maya's error bodies are {"code", "message"} or {"error", "code"}.
        detail = (
            (body.get("message") or body.get("error") or response.text[:200])
            if isinstance(body, dict)
            else response.text[:200]
        )
        code = body.get("code", "") if isinstance(body, dict) else ""
        logger.warning("Maya returned %s for %s: %s %s", response.status_code, path, code, detail)
        raise MayaError(
            f"Maya rejected the request: {detail}",
            status_code=response.status_code,
            code=code,
        )
    return body


def _amount(value):
    return f"{Decimal(value):.2f}"


def build_checkout_body(order, rrn, success_url, failure_url, cancel_url):
    """The create-checkout payload: the order's lines plus the delivery fee."""
    items = [
        {
            "name": item.menu.name[:255],
            "quantity": str(item.quantity),
            "amount": {"value": _amount(item.unit_price)},
            "totalAmount": {"value": _amount(item.line_subtotal)},
        }
        for item in order.items.select_related("menu")
    ]
    if order.delivery_fee:
        items.append(
            {
                "name": "Delivery fee",
                "quantity": "1",
                "amount": {"value": _amount(order.delivery_fee)},
                "totalAmount": {"value": _amount(order.delivery_fee)},
            }
        )
    return {
        "totalAmount": {"value": _amount(order.total), "currency": "PHP"},
        "requestReferenceNumber": rrn,
        "items": items,
        "redirectUrl": {"success": success_url, "failure": failure_url, "cancel": cancel_url},
    }


def create_checkout(order, rrn, success_url, failure_url, cancel_url):
    """
    Open a Maya checkout for an order.

    Returns {"checkout_id", "checkout_url"}.
    """
    body = _send(
        "POST",
        "/checkout/v1/checkouts",
        key=settings.MAYA_PUBLIC_KEY,
        json=build_checkout_body(order, rrn, success_url, failure_url, cancel_url),
    )
    if not body.get("checkoutId") or not body.get("redirectUrl"):
        raise MayaError("Maya did not return a checkout link.")
    return {"checkout_id": body["checkoutId"], "checkout_url": body["redirectUrl"]}


def classify(status, is_paid=False):
    """'paid', 'failed' or 'pending' for one of Maya's payment states."""
    if status in RETURNED_STATES:
        return "failed"
    if status in PAID_STATES or is_paid:
        return "paid"
    if status in FAILED_STATES:
        return "failed"
    return "pending"


def mask_payer(payment):
    """
    A masked wallet label from Maya's `fundSource`, or "" when it has none.

    Maya's reference says the payment lookup carries `fundSource` with a
    `type` ("maya-wallet" for a wallet) and a `description` holding the last
    four digits of the customer's account. This is read defensively because
    the sandbox's wallet login could not be completed to see a real one: any
    shape other than that yields "" rather than a guess.
    """
    source = payment.get("fundSource")
    if not isinstance(source, dict):
        return ""

    kind = str(source.get("type") or "").lower()
    label = "Maya wallet" if "wallet" in kind or "maya" in kind else kind.replace("-", " ").title()
    if not label:
        return ""

    digits = "".join(ch for ch in str(source.get("description") or "") if ch.isdigit())
    return f"{label} ••••{digits[-4:]}" if digits else label


def retrieve_payment(rrn):
    """
    Where a payment stands, looked up by our reference number.

    Returns {"id", "status", "state", "amount", "can_void", "can_refund",
    "created_at", "payer", "raw"}. `state` is paid / failed / pending.
    """
    body = _send("GET", f"/payments/v1/payment-rrns/{rrn}", key=settings.MAYA_SECRET_KEY)
    records = body if isinstance(body, list) else [body]
    records = [r for r in records if isinstance(r, dict) and r.get("id")]
    if not records:
        raise MayaError("Maya has no payment for that reference.")

    payment = records[0]
    status = payment.get("status") or payment.get("paymentStatus") or ""
    is_paid = bool(payment.get("isPaid"))
    try:
        amount = Decimal(str(payment.get("amount")))
    except Exception:
        amount = None

    return {
        "id": payment["id"],
        "status": status,
        "state": classify(status, is_paid),
        "amount": amount,
        "can_void": bool(payment.get("canVoid")),
        "can_refund": bool(payment.get("canRefund")),
        "created_at": parse_time(payment.get("createdAt")),
        "payer": mask_payer(payment),
        "raw": payment,
    }


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _outcome(body):
    """Maya's PENDING / SUCCESS / FAILED, as ours."""
    status = str(body.get("status", "")).upper()
    return {"SUCCESS": "refunded", "PENDING": "pending", "FAILED": "failed"}.get(status, "pending")


def void_payment(rrn, reason):
    """Full same-day reversal. Returns {"reference", "outcome"}."""
    body = _send(
        "POST",
        f"/payments/v1/payment-rrns/{rrn}/voids",
        key=settings.MAYA_SECRET_KEY,
        json={"reason": reason[:512]},
    )
    return {"reference": body.get("id", ""), "outcome": _outcome(body)}


def refund_payment(rrn, amount, reason):
    """Refund from the day after payment. Returns {"reference", "outcome"}."""
    body = _send(
        "POST",
        f"/payments/v1/payment-rrns/{rrn}/refunds",
        key=settings.MAYA_SECRET_KEY,
        json={
            "totalAmount": {"amount": float(Decimal(amount)), "currency": "PHP"},
            "reason": reason[:512],
        },
    )
    return {"reference": body.get("id", ""), "outcome": _outcome(body)}
