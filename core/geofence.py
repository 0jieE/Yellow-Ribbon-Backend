"""
Service-area enforcement (spec section 8, step 1).

Kidapawan City only. Checked server-side on every order placement,
before anything is created.
"""

import math

from django.conf import settings

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in kilometres between two WGS84 points."""
    lat1, lng1, lat2, lng2 = (float(v) for v in (lat1, lng1, lat2, lng2))

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def distance_from_service_center_km(lat, lng):
    return haversine_km(
        settings.SERVICE_AREA_CENTER_LAT,
        settings.SERVICE_AREA_CENTER_LNG,
        lat,
        lng,
    )


def is_within_service_area(lat, lng):
    return distance_from_service_center_km(lat, lng) <= settings.SERVICE_AREA_RADIUS_KM
