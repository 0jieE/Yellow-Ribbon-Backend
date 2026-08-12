"""
Uniform error shape for all three Flutter apps (spec section 13):

    {"error": "message"}

Field-level validation errors keep their detail under "fields" so the
client can highlight the offending input, but "error" is always present
and always a plain string.
"""

from django.db.models import ProtectedError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import exception_handler as drf_exception_handler


# Keys that name the error rather than a form field — their message is
# already a complete sentence and must not be prefixed.
_UNPREFIXED_KEYS = {"detail", api_settings.NON_FIELD_ERRORS_KEY}


def _first_message(detail):
    """Pull one human-readable string out of DRF's nested error detail."""
    if isinstance(detail, dict):
        for key, value in detail.items():
            message = _first_message(value)
            if message:
                return message if key in _UNPREFIXED_KEYS else f"{key}: {message}"
        return None
    if isinstance(detail, (list, tuple)):
        for item in detail:
            message = _first_message(item)
            if message:
                return message
        return None
    return str(detail) if detail else None


def api_exception_handler(exc, context):
    if isinstance(exc, ProtectedError):
        # A menu item referenced by an existing order cannot be deleted —
        # historical receipts must not change (spec section 4). Answer with a
        # clear 409 instead of letting it surface as a 500.
        return Response(
            {
                "error": (
                    "This item appears on existing orders and cannot be deleted. "
                    "Mark it unavailable instead."
                )
            },
            status=status.HTTP_409_CONFLICT,
        )

    response = drf_exception_handler(exc, context)
    if response is None:
        # Unhandled exception: let Django's own 500 handling deal with it.
        return None

    detail = response.data
    message = _first_message(detail) or "Request failed."

    payload = {"error": message}
    if isinstance(detail, dict) and set(detail) - {"detail"}:
        payload["fields"] = detail

    response.data = payload
    return response
