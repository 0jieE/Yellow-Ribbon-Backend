"""
Estimated delivery time via the Google Routes API (spec section 9).

Deliberately uses **Compute Routes** (one origin, one destination) and not
Compute Route Matrix, which bills per element.

Called from exactly one place, in this backend and never from a Flutter
app: when a delivery goes `in_transit`, measuring from the rider's current
position to the customer.

There is deliberately no ETA at assignment time. Riders collect a batch at
the store and work through it one leg at a time, so a figure calculated
when the order was handed over would be wrong before the rider set off.
`eta_minutes`, `eta_at` and `route_polyline` stay null until the leg for
that order actually starts.

It is never called from the location ping. The rider app pings every
10-15 seconds; billing a Routes call each time is the main cost risk in
this system.

Nothing here raises. A Maps outage costs the order its fresh ETA, not its
existence.
"""

import logging
from datetime import timedelta
from math import ceil

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

COMPUTE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

# Only the fields we actually read. The Routes API requires a field mask and
# charges by SKU based on what you ask for, so keep it minimal. Routes returns
# nothing that is not listed here — the polyline is absent unless asked for.
# All three of these sit in the same (Essentials) tier.
FIELD_MASK = (
    "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline"
)


def _parse_duration_seconds(value):
    """Routes returns protobuf durations as strings like '1234s' or '1234.5s'."""
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    try:
        return int(round(float(value[:-1])))
    except ValueError:
        return None


def compute_route(origin, destination):
    """
    One origin to one destination.

    Returns {"duration_seconds": int, "distance_meters": int|None}, or None
    if the ETA could not be determined for any reason. Never raises.
    """
    api_key = settings.GOOGLE_MAPS_SERVER_KEY
    if not api_key:
        logger.info("GOOGLE_MAPS_SERVER_KEY is not set; skipping ETA lookup.")
        return None

    origin_lat, origin_lng = origin
    dest_lat, dest_lng = destination

    payload = {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": float(origin_lat),
                    "longitude": float(origin_lng),
                }
            }
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": float(dest_lat),
                    "longitude": float(dest_lng),
                }
            }
        },
        "travelMode": settings.GOOGLE_ROUTES_TRAVEL_MODE,
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": False,
        "units": "METRIC",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }

    try:
        response = requests.post(
            COMPUTE_ROUTES_URL,
            json=payload,
            headers=headers,
            timeout=settings.GOOGLE_ROUTES_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except requests.Timeout:
        logger.warning("Routes API timed out after %ss.", settings.GOOGLE_ROUTES_TIMEOUT_SECONDS)
        return None
    except requests.RequestException as exc:
        # Log the status and body, never the API key.
        body = getattr(exc.response, "text", "")[:500] if exc.response is not None else ""
        logger.warning("Routes API call failed: %s %s", exc, body)
        return None
    except ValueError:
        logger.warning("Routes API returned a non-JSON body.")
        return None

    routes = data.get("routes") or []
    if not routes:
        # A valid response with no route: unreachable destination, or the
        # travel mode is not supported for this region.
        logger.warning("Routes API returned no route for the requested pair.")
        return None

    seconds = _parse_duration_seconds(routes[0].get("duration"))
    if seconds is None:
        logger.warning("Routes API response had no usable duration.")
        return None

    return {
        "duration_seconds": seconds,
        "distance_meters": routes[0].get("distanceMeters"),
        # Encoded polyline for the leg, ready to draw on a Flutter map.
        "polyline": (routes[0].get("polyline") or {}).get("encodedPolyline", ""),
    }


def store_location():
    """
    Where the food comes from.

    The spec treats the service-area centre as the store (section 11 tells
    you to verify it against the real shop address), so there is one
    coordinate pair to keep correct rather than two.
    """
    return (settings.SERVICE_AREA_CENTER_LAT, settings.SERVICE_AREA_CENTER_LNG)


def refresh_order_eta(order, origin=None, *, trigger=""):
    """
    Recompute the EDT and store it on the order.

    Returns True if the order was updated. On any failure the order keeps
    its last known ETA — a stale estimate beats no estimate, and beats a
    500 during rider assignment.
    """
    try:
        if origin is None:
            origin = store_location()

        result = compute_route(
            origin, (order.delivery_latitude, order.delivery_longitude)
        )
        if result is None:
            logger.warning(
                "Keeping last known ETA for %s (trigger=%s): Routes lookup failed.",
                order.order_number,
                trigger or "unspecified",
            )
            return False

        seconds = result["duration_seconds"]
        order.eta_minutes = max(1, ceil(seconds / 60))
        order.eta_at = timezone.now() + timedelta(seconds=seconds)
        # Overwritten wholesale on every successful refresh: a polyline from
        # the previous leg would draw the wrong route.
        order.route_polyline = result.get("polyline") or ""
        order.save(update_fields=["eta_minutes", "eta_at", "route_polyline"])

        logger.info(
            "ETA for %s set to %s min (trigger=%s, %sm).",
            order.order_number,
            order.eta_minutes,
            trigger or "unspecified",
            result.get("distance_meters"),
        )
        return True

    except Exception:
        # Belt and braces: whatever went wrong, the caller's flow continues.
        logger.exception(
            "Unexpected error computing the ETA for %s; keeping the last known value.",
            getattr(order, "order_number", "?"),
        )
        return False


def refresh_eta_from_rider(order, rider, *, trigger=""):
    """
    ETA from where the rider actually is. Falls back to the store when the
    rider app has not reported a position yet.
    """
    if rider.current_latitude is not None and rider.current_longitude is not None:
        origin = (rider.current_latitude, rider.current_longitude)
    else:
        logger.info(
            "Rider %s has no reported location; measuring the ETA from the store.",
            rider.pk,
        )
        origin = None

    return refresh_order_eta(order, origin, trigger=trigger)
