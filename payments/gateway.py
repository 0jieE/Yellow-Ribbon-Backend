"""
PayMongo REST client.

Test keys only (spec section 9). The secret key never leaves the server and
is never logged.

Flow for GCash:
    create_source()  -> customer is redirected to checkout_url
    customer pays    -> PayMongo fires source.chargeable
    create_payment() -> actually collects against the chargeable source
    PayMongo fires payment.paid
"""

import base64
import logging
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

API_ROOT = "https://api.paymongo.com/v1"
SOURCES_URL = f"{API_ROOT}/sources"
PAYMENTS_URL = f"{API_ROOT}/payments"

# PayMongo rejects anything under one peso.
MINIMUM_CENTAVOS = 100


class PayMongoError(Exception):
    """A PayMongo call failed. Carries a customer-safe message."""

    def __init__(self, message, *, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def to_centavos(amount):
    """PayMongo bills in integer centavos. Decimal in, int out — never float."""
    return int((Decimal(amount) * 100).quantize(Decimal("1")))


def _auth_header():
    secret = settings.PAYMONGO_SECRET_KEY
    if not secret:
        raise PayMongoError("Online payment is not configured.")
    token = base64.b64encode(f"{secret}:".encode()).decode()
    return f"Basic {token}"


def _post(url, payload):
    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": _auth_header(),
                "Content-Type": "application/json",
            },
            timeout=settings.PAYMONGO_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        logger.warning("PayMongo timed out calling %s.", url)
        raise PayMongoError("PayMongo did not respond. Please try again.")
    except requests.RequestException as exc:
        logger.warning("PayMongo request to %s failed: %s", url, exc)
        raise PayMongoError("Could not reach PayMongo. Please try again.")

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code >= 400:
        # Log PayMongo's own error detail; the Authorization header is never
        # part of the response, so this is safe.
        errors = body.get("errors") or [{}]
        detail = errors[0].get("detail", response.text[:300])
        logger.warning(
            "PayMongo returned %s for %s: %s", response.status_code, url, detail
        )
        raise PayMongoError(
            f"PayMongo rejected the request: {detail}",
            status_code=response.status_code,
            payload=body,
        )

    return body


def _check_redirect(url, label):
    """
    PayMongo only accepts http(s) redirect targets.

    A custom scheme like `yellowribbon://payment/success` is rejected with
    "redirect.failed format is invalid", so catch it here with a message
    that says what to do rather than letting PayMongo phrase it.
    """
    if not url or not str(url).startswith(("http://", "https://")):
        raise PayMongoError(
            f"PAYMONGO_{label}_REDIRECT_URL must be an http(s) URL, not "
            f"{url!r}. PayMongo rejects custom app schemes; point it at a web "
            "page that deep-links into the app instead."
        )
    return url


def create_gcash_source(order, success_url=None, failed_url=None):
    """
    Create a GCash source for an order and return the bits we store.

    Returns {"source_id": str, "checkout_url": str, "amount_centavos": int}.

    The redirect URLs default to the configured ones; the view passes
    request-derived absolute URLs when nothing is configured, so this works
    behind a tunnel without extra setup.
    """
    centavos = to_centavos(order.total)
    if centavos < MINIMUM_CENTAVOS:
        raise PayMongoError("Order total is below the minimum PayMongo accepts.")

    success = _check_redirect(
        success_url or settings.PAYMONGO_SUCCESS_REDIRECT_URL, "SUCCESS"
    )
    failed = _check_redirect(
        failed_url or settings.PAYMONGO_FAILED_REDIRECT_URL, "FAILED"
    )

    payload = {
        "data": {
            "attributes": {
                "amount": centavos,
                "currency": "PHP",
                "type": "gcash",
                "redirect": {"success": success, "failed": failed},
            }
        }
    }

    body = _post(SOURCES_URL, payload)
    data = body.get("data") or {}
    attributes = data.get("attributes") or {}
    checkout_url = (attributes.get("redirect") or {}).get("checkout_url")

    if not data.get("id") or not checkout_url:
        logger.warning("PayMongo source response was missing an id or checkout_url.")
        raise PayMongoError("PayMongo did not return a checkout link.")

    return {
        "source_id": data["id"],
        "checkout_url": checkout_url,
        "amount_centavos": centavos,
    }


def create_payment_from_source(source_id, amount, description=""):
    """
    Collect against a chargeable source.

    A source going chargeable only means the customer authorised it. Without
    this call the money is never actually taken.
    """
    payload = {
        "data": {
            "attributes": {
                "amount": to_centavos(amount),
                "currency": "PHP",
                "source": {"id": source_id, "type": "source"},
                "description": description,
            }
        }
    }

    body = _post(PAYMENTS_URL, payload)
    data = body.get("data") or {}
    if not data.get("id"):
        raise PayMongoError("PayMongo did not return a payment id.")

    return {
        "payment_id": data["id"],
        "status": (data.get("attributes") or {}).get("status", ""),
    }
