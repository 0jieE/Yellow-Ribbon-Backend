"""
Paymongo-Signature verification (spec section 9).

The webhook is unauthenticated by design — this signature *is* the
authentication, so nothing else in the request may be trusted until it
checks out.

Header format:

    t=1496734173,te=<test mode signature>,li=<live mode signature>

The signed payload is "{t}.{raw request body}", HMAC-SHA256 with the
webhook secret. The raw bytes matter: re-serialising the JSON would change
the payload and break the comparison.
"""

import hashlib
import hmac
import logging
import time

logger = logging.getLogger(__name__)


def parse_signature_header(header):
    """Split the header into its comma-separated key=value parts."""
    parts = {}
    for chunk in (header or "").split(","):
        key, _, value = chunk.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            parts[key] = value
    return parts


def verify_signature(raw_body, header, secret, tolerance_seconds=0):
    """
    True when `header` is a valid signature for `raw_body`.

    `tolerance_seconds=0` disables the replay window — see
    PAYMONGO_WEBHOOK_TOLERANCE_SECONDS in settings for why that is the
    default.
    """
    if not secret:
        logger.error("PAYMONGO_WEBHOOK_SECRET is not set; rejecting the webhook.")
        return False

    parts = parse_signature_header(header)
    timestamp = parts.get("t")
    if not timestamp:
        logger.warning("Paymongo-Signature header has no timestamp.")
        return False

    if tolerance_seconds:
        try:
            age = abs(time.time() - int(timestamp))
        except ValueError:
            logger.warning("Paymongo-Signature timestamp is not an integer.")
            return False
        if age > tolerance_seconds:
            logger.warning("Paymongo-Signature is %.0fs old; outside tolerance.", age)
            return False

    if isinstance(raw_body, str):
        raw_body = raw_body.encode()

    expected = hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()

    # PayMongo populates `te` for test-mode webhooks and `li` for live ones.
    # Only the one matching the secret's mode can validate.
    for field in ("te", "li"):
        candidate = parts.get(field)
        if candidate and hmac.compare_digest(expected, candidate):
            return True

    logger.warning("Paymongo-Signature did not match the computed digest.")
    return False
