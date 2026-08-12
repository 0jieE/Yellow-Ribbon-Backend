"""
Google Routes / EDT tests.

Every HTTP call is mocked — the suite must never bill the real API.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import requests
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, Rider, User
from core.maps import (
    COMPUTE_ROUTES_URL,
    _parse_duration_seconds,
    compute_route,
    refresh_eta_from_rider,
    refresh_order_eta,
)
from delivery.models import Delivery
from menu.models import Menu
from orders.constants import DeliveryStatus, OrderStatus, PaymentType
from orders.models import Order

FAKE_KEY = "test-key-not-real"


POLYLINE = "_ywi@qonzVdDzA|DbBbCn@|@\\vDfBuEuB_@OqBoAuQ{HcIsD"


def routes_response(seconds=600, meters=3200, polyline=POLYLINE):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            route = {"duration": f"{seconds}s", "distanceMeters": meters}
            if polyline is not None:
                route["polyline"] = {"encodedPolyline": polyline}
            return {"routes": [route]}

    return FakeResponse()


class DurationParsingTests(APITestCase):
    def test_parses_protobuf_durations(self):
        self.assertEqual(_parse_duration_seconds("600s"), 600)
        self.assertEqual(_parse_duration_seconds("1234.5s"), 1234)
        self.assertEqual(_parse_duration_seconds("0s"), 0)

    def test_rejects_junk(self):
        for value in (None, "", "600", "abcs", 600, {}):
            self.assertIsNone(_parse_duration_seconds(value))


@override_settings(GOOGLE_MAPS_SERVER_KEY=FAKE_KEY)
class ComputeRouteTests(APITestCase):
    origin = (7.0083, 125.0894)
    destination = (7.0400, 125.1000)

    @patch("core.maps.requests.post")
    def test_calls_compute_routes_not_the_matrix_endpoint(self, post):
        post.return_value = routes_response()
        compute_route(self.origin, self.destination)

        url = post.call_args.args[0]
        self.assertEqual(url, COMPUTE_ROUTES_URL)
        self.assertIn("directions/v2:computeRoutes", url)
        # Compute Route Matrix bills per element and is explicitly banned.
        self.assertNotIn("computeRouteMatrix", url)

    @patch("core.maps.requests.post")
    def test_request_shape(self, post):
        post.return_value = routes_response()
        compute_route(self.origin, self.destination)

        kwargs = post.call_args.kwargs
        headers = kwargs["headers"]
        self.assertEqual(headers["X-Goog-Api-Key"], FAKE_KEY)
        self.assertEqual(
            headers["X-Goog-FieldMask"],
            "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline",
        )

        body = kwargs["json"]
        self.assertEqual(
            body["origin"]["location"]["latLng"],
            {"latitude": 7.0083, "longitude": 125.0894},
        )
        self.assertEqual(
            body["destination"]["location"]["latLng"],
            {"latitude": 7.04, "longitude": 125.1},
        )
        self.assertEqual(body["travelMode"], "TWO_WHEELER")
        self.assertFalse(body["computeAlternativeRoutes"])
        self.assertEqual(kwargs["timeout"], 5.0)

    @patch("core.maps.requests.post")
    def test_one_origin_one_destination_per_call(self, post):
        post.return_value = routes_response()
        compute_route(self.origin, self.destination)

        body = post.call_args.kwargs["json"]
        # Singular keys only — no waypoint arrays that would fan out billing.
        self.assertIsInstance(body["origin"], dict)
        self.assertIsInstance(body["destination"], dict)
        self.assertNotIn("origins", body)
        self.assertNotIn("destinations", body)

    @patch("core.maps.requests.post")
    def test_parses_a_successful_response(self, post):
        post.return_value = routes_response(seconds=930, meters=5400)
        result = compute_route(self.origin, self.destination)

        self.assertEqual(result["duration_seconds"], 930)
        self.assertEqual(result["distance_meters"], 5400)

    @patch("core.maps.requests.post")
    def test_field_mask_asks_for_the_polyline(self, post):
        """Routes returns nothing that is not in the mask."""
        post.return_value = routes_response()
        compute_route(self.origin, self.destination)

        mask = post.call_args.kwargs["headers"]["X-Goog-FieldMask"]
        self.assertIn("routes.polyline.encodedPolyline", mask)
        self.assertIn("routes.duration", mask)
        # Anything beyond the Essentials tier would change the billing SKU.
        self.assertNotIn("routes.legs", mask)
        self.assertNotIn("travelAdvisory", mask)

    @patch("core.maps.requests.post")
    def test_polyline_is_parsed_out(self, post):
        post.return_value = routes_response()
        result = compute_route(self.origin, self.destination)
        self.assertEqual(result["polyline"], POLYLINE)

    @patch("core.maps.requests.post")
    def test_a_missing_polyline_is_not_fatal(self, post):
        post.return_value = routes_response(polyline=None)
        result = compute_route(self.origin, self.destination)

        self.assertEqual(result["duration_seconds"], 600)
        self.assertEqual(result["polyline"], "")

    @override_settings(GOOGLE_ROUTES_TRAVEL_MODE="DRIVE")
    @patch("core.maps.requests.post")
    def test_travel_mode_is_configurable(self, post):
        post.return_value = routes_response()
        compute_route(self.origin, self.destination)
        self.assertEqual(post.call_args.kwargs["json"]["travelMode"], "DRIVE")

    # -- failure modes, all returning None rather than raising ----------

    @patch("core.maps.requests.post", side_effect=requests.Timeout())
    def test_timeout(self, _post):
        self.assertIsNone(compute_route(self.origin, self.destination))

    @patch("core.maps.requests.post", side_effect=requests.ConnectionError())
    def test_connection_error(self, _post):
        self.assertIsNone(compute_route(self.origin, self.destination))

    @patch("core.maps.requests.post")
    def test_http_error(self, post):
        post.return_value.raise_for_status.side_effect = requests.HTTPError(
            "403 Forbidden"
        )
        self.assertIsNone(compute_route(self.origin, self.destination))

    @patch("core.maps.requests.post")
    def test_non_json_body(self, post):
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.side_effect = ValueError("not json")
        self.assertIsNone(compute_route(self.origin, self.destination))

    @patch("core.maps.requests.post")
    def test_empty_routes(self, post):
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"routes": []}
        self.assertIsNone(compute_route(self.origin, self.destination))

    @patch("core.maps.requests.post")
    def test_missing_duration(self, post):
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"routes": [{"distanceMeters": 100}]}
        self.assertIsNone(compute_route(self.origin, self.destination))

    @override_settings(GOOGLE_MAPS_SERVER_KEY="")
    @patch("core.maps.requests.post")
    def test_no_key_means_no_http_call_at_all(self, post):
        self.assertIsNone(compute_route(self.origin, self.destination))
        post.assert_not_called()

    @patch("core.maps.requests.post")
    def test_api_key_is_never_logged(self, post):
        post.return_value.raise_for_status.side_effect = requests.HTTPError("boom")
        with self.assertLogs("core.maps", level="INFO") as logs:
            compute_route(self.origin, self.destination)
        self.assertNotIn(FAKE_KEY, "\n".join(logs.output))


class EtaFixtureMixin:
    def build(self):
        self.cust_user = User.objects.create_user(
            username="maria", email="maria@example.com", password="Kidapawan!2026"
        )
        self.customer = Customer.objects.create(user=self.cust_user)
        self.admin = User.objects.create_superuser(
            username="boss", email="boss@example.com", password="Kidapawan!2026"
        )
        self.rider_user = User.objects.create_user(
            username="rico",
            email="rico@example.com",
            password="Kidapawan!2026",
            is_rider=True,
            is_customer=False,
        )
        self.rider = Rider.objects.create(user=self.rider_user, is_available=True)
        self.menu = Menu.objects.create(name="Pepperoni", price=Decimal("299.00"))
        self.order = Order.objects.create(
            order_number=f"YR-E{Order.objects.count() + 1:04d}",
            customer=self.customer,
            status=OrderStatus.READY,
            payment_type=PaymentType.COD,
            subtotal=Decimal("299.00"),
            delivery_fee=Decimal("30.00"),
            total=Decimal("329.00"),
            delivery_address="Blue gate, Quezon Blvd",
            delivery_latitude=Decimal("7.040000"),
            delivery_longitude=Decimal("125.100000"),
        )


@override_settings(GOOGLE_MAPS_SERVER_KEY=FAKE_KEY)
class RefreshOrderEtaTests(EtaFixtureMixin, APITestCase):
    def setUp(self):
        self.build()

    @patch("core.maps.compute_route", return_value={"duration_seconds": 900, "distance_meters": 4000})
    def test_stores_eta_minutes_and_eta_at(self, _route):
        before = timezone.now()
        self.assertTrue(refresh_order_eta(self.order, trigger="test"))

        self.order.refresh_from_db()
        self.assertEqual(self.order.eta_minutes, 15)
        self.assertGreaterEqual(self.order.eta_at, before + timedelta(seconds=899))

    @patch("core.maps.compute_route", return_value={"duration_seconds": 61, "distance_meters": 100})
    def test_minutes_are_rounded_up(self, _route):
        refresh_order_eta(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.eta_minutes, 2)

    @patch("core.maps.compute_route", return_value={"duration_seconds": 5, "distance_meters": 10})
    def test_never_reports_zero_minutes(self, _route):
        refresh_order_eta(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.eta_minutes, 1)

    @patch(
        "core.maps.compute_route",
        return_value={
            "duration_seconds": 900,
            "distance_meters": 4000,
            "polyline": POLYLINE,
        },
    )
    def test_polyline_is_stored_on_the_order(self, _route):
        refresh_order_eta(self.order, trigger="test")

        self.order.refresh_from_db()
        self.assertEqual(self.order.route_polyline, POLYLINE)

    @patch(
        "core.maps.compute_route",
        return_value={
            "duration_seconds": 900,
            "distance_meters": 4000,
            "polyline": "NEWLEGPOLYLINE",
        },
    )
    def test_a_refresh_replaces_the_previous_leg(self, _route):
        """A polyline from the last leg would draw the wrong route."""
        self.order.route_polyline = "STALEPOLYLINE"
        self.order.save()

        refresh_order_eta(self.order, trigger="test")

        self.order.refresh_from_db()
        self.assertEqual(self.order.route_polyline, "NEWLEGPOLYLINE")

    @patch("core.maps.compute_route", return_value=None)
    def test_failure_keeps_the_last_known_polyline(self, _route):
        self.order.route_polyline = "KEEPME"
        self.order.save()

        self.assertFalse(refresh_order_eta(self.order, trigger="test"))

        self.order.refresh_from_db()
        self.assertEqual(self.order.route_polyline, "KEEPME")

    @patch("core.maps.compute_route", return_value=None)
    def test_failure_keeps_the_last_known_eta(self, _route):
        self.order.eta_minutes = 12
        self.order.eta_at = timezone.now() + timedelta(minutes=12)
        self.order.save()
        previous = self.order.eta_at

        self.assertFalse(refresh_order_eta(self.order, trigger="test"))

        self.order.refresh_from_db()
        self.assertEqual(self.order.eta_minutes, 12)
        self.assertEqual(self.order.eta_at, previous)

    @patch("core.maps.compute_route", side_effect=RuntimeError("something odd"))
    def test_unexpected_errors_are_swallowed(self, _route):
        self.assertFalse(refresh_order_eta(self.order, trigger="test"))
        self.order.refresh_from_db()
        self.assertIsNone(self.order.eta_minutes)

    @patch("core.maps.compute_route", return_value={"duration_seconds": 600, "distance_meters": 1})
    def test_origin_defaults_to_the_store(self, route):
        refresh_order_eta(self.order)
        origin = route.call_args.args[0]
        self.assertEqual(origin, (7.0083, 125.0894))

    @patch("core.maps.compute_route", return_value={"duration_seconds": 600, "distance_meters": 1})
    def test_destination_is_the_delivery_address(self, route):
        refresh_order_eta(self.order)
        destination = route.call_args.args[1]
        self.assertEqual(
            destination, (Decimal("7.040000"), Decimal("125.100000"))
        )

    @patch("core.maps.compute_route", return_value={"duration_seconds": 600, "distance_meters": 1})
    def test_rider_origin_is_used_when_known(self, route):
        self.rider.current_latitude = Decimal("7.020000")
        self.rider.current_longitude = Decimal("125.095000")
        self.rider.save()

        refresh_eta_from_rider(self.order, self.rider, trigger="test")
        self.assertEqual(
            route.call_args.args[0], (Decimal("7.020000"), Decimal("125.095000"))
        )

    @patch("core.maps.compute_route", return_value={"duration_seconds": 600, "distance_meters": 1})
    def test_falls_back_to_the_store_when_the_rider_has_not_pinged(self, route):
        refresh_eta_from_rider(self.order, self.rider, trigger="test")
        self.assertEqual(route.call_args.args[0], (7.0083, 125.0894))


@override_settings(GOOGLE_MAPS_SERVER_KEY=FAKE_KEY)
class EtaTriggerTests(EtaFixtureMixin, APITestCase):
    """Exactly two trigger points, and the location ping is not one of them."""

    def setUp(self):
        self.build()
        self.patcher = patch(
            "core.maps.compute_route",
            return_value={
                "duration_seconds": 900,
                "distance_meters": 4000,
                "polyline": "TESTPOLYLINE",
            },
        )
        self.route = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def assign(self):
        self.client.force_authenticate(self.admin)
        return self.client.post(
            reverse("admin-order-assign", args=[self.order.pk]),
            {"rider_id": self.rider.pk},
            format="json",
        )

    def set_delivery_status(self, delivery, value):
        self.client.force_authenticate(self.rider_user)
        return self.client.patch(
            reverse("delivery:rider-delivery-status", args=[delivery.pk]),
            {"status": value},
            format="json",
        )

    # -- assignment must NOT compute an ETA ----------------------------

    def test_assignment_does_not_call_routes(self):
        """
        A rider taking a batch does not leave for the first order straight
        away, so an assignment-time figure would be wrong on arrival.
        """
        response = self.assign()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.route.assert_not_called()

    def test_eta_stays_null_after_assignment(self):
        data = self.assign().data

        self.assertIsNone(data["eta_minutes"])
        self.assertIsNone(data["eta_at"])

        self.order.refresh_from_db()
        self.assertIsNone(self.order.eta_minutes)
        self.assertIsNone(self.order.eta_at)
        self.assertEqual(self.order.route_polyline, "")

    def test_assigning_a_whole_batch_calls_routes_never(self):
        for _ in range(3):
            order = Order.objects.create(
                order_number=f"YR-B{Order.objects.count() + 1:04d}",
                customer=self.customer,
                status=OrderStatus.READY,
                payment_type=PaymentType.COD,
                subtotal=Decimal("299.00"),
                delivery_fee=Decimal("30.00"),
                total=Decimal("329.00"),
                delivery_address="Blue gate",
                delivery_latitude=Decimal("7.040000"),
                delivery_longitude=Decimal("125.100000"),
            )
            self.client.force_authenticate(self.admin)
            response = self.client.post(
                reverse("admin-order-assign", args=[order.pk]),
                {"rider_id": self.rider.pk},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.route.assert_not_called()

    # -- the only trigger: in_transit ----------------------------------

    def test_in_transit_computes_the_eta(self):
        self.assign()
        delivery = Delivery.objects.get(order=self.order)

        self.set_delivery_status(delivery, "picked_up")
        self.assertEqual(self.route.call_count, 0, "pickup must not call Routes")

        self.set_delivery_status(delivery, "in_transit")
        self.assertEqual(self.route.call_count, 1)

        self.order.refresh_from_db()
        self.assertEqual(self.order.eta_minutes, 15)
        self.assertIsNotNone(self.order.eta_at)

    def test_in_transit_measures_from_the_rider(self):
        self.assign()
        delivery = Delivery.objects.get(order=self.order)

        self.client.force_authenticate(self.rider_user)
        self.client.post(
            reverse("delivery:location"),
            {"lat": 7.0200, "lng": 125.0950},
            format="json",
        )

        self.set_delivery_status(delivery, "picked_up")
        self.route.reset_mock()
        self.set_delivery_status(delivery, "in_transit")

        self.assertEqual(
            self.route.call_args.args[0], (Decimal("7.020000"), Decimal("125.095000"))
        )

    def test_in_transit_survives_a_maps_outage(self):
        self.assign()
        delivery = Delivery.objects.get(order=self.order)
        self.set_delivery_status(delivery, "picked_up")
        self.route.side_effect = requests.ConnectionError()

        response = self.set_delivery_status(delivery, "in_transit")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, DeliveryStatus.IN_TRANSIT)

    # -- everything that must NOT trigger it ---------------------------

    def test_location_ping_never_calls_routes(self):
        """The main cost risk in the system. The rider app pings constantly."""
        self.assign()
        self.route.reset_mock()

        self.client.force_authenticate(self.rider_user)
        for i in range(20):
            response = self.client.post(
                reverse("delivery:location"),
                {"lat": 7.01 + i * 0.001, "lng": 125.09},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.route.assert_not_called()

    def test_delivered_and_failed_do_not_call_routes(self):
        self.assign()
        delivery = Delivery.objects.get(order=self.order)
        self.set_delivery_status(delivery, "picked_up")
        self.set_delivery_status(delivery, "in_transit")
        self.route.reset_mock()

        self.set_delivery_status(delivery, "delivered")
        self.route.assert_not_called()

    def test_order_placement_does_not_call_routes(self):
        self.client.force_authenticate(self.cust_user)
        response = self.client.post(
            reverse("orders:order-list"),
            {
                "items": [{"menu_id": self.menu.pk, "quantity": 1}],
                "delivery_address": "Blue gate",
                "delivery_latitude": "7.008300",
                "delivery_longitude": "125.089400",
                "payment_type": "cod",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.route.assert_not_called()

    def test_admin_status_changes_do_not_call_routes(self):
        self.order.status = OrderStatus.PENDING
        self.order.save()

        self.client.force_authenticate(self.admin)
        for value in ("confirmed", "preparing", "ready"):
            self.client.patch(
                reverse("admin-order-status", args=[self.order.pk]),
                {"status": value},
                format="json",
            )

        self.route.assert_not_called()

    def test_reading_an_order_does_not_call_routes(self):
        self.assign()
        self.route.reset_mock()

        self.client.force_authenticate(self.cust_user)
        for _ in range(5):
            self.client.get(reverse("orders:order-detail", args=[self.order.pk]))

        self.route.assert_not_called()

    def start_leg(self):
        self.assign()
        delivery = Delivery.objects.get(order=self.order)
        self.set_delivery_status(delivery, "picked_up")
        self.set_delivery_status(delivery, "in_transit")
        return delivery

    def test_polyline_reaches_the_customer_order_detail(self):
        self.start_leg()

        self.client.force_authenticate(self.cust_user)
        data = self.client.get(
            reverse("orders:order-detail", args=[self.order.pk])
        ).data

        self.assertEqual(data["route_polyline"], "TESTPOLYLINE")

    def test_polyline_reaches_the_rider_delivery_detail(self):
        delivery = self.start_leg()

        self.client.force_authenticate(self.rider_user)
        data = self.client.get(
            reverse("delivery:rider-delivery-detail", args=[delivery.pk])
        ).data

        self.assertEqual(data["order"]["route_polyline"], "TESTPOLYLINE")

    def test_polyline_is_blank_until_the_leg_starts(self):
        self.assign()
        delivery = Delivery.objects.get(order=self.order)
        self.set_delivery_status(delivery, "picked_up")

        self.client.force_authenticate(self.cust_user)
        data = self.client.get(
            reverse("orders:order-detail", args=[self.order.pk])
        ).data

        self.assertEqual(data["route_polyline"], "")
        self.assertIsNone(data["eta_minutes"])

    def test_each_leg_writes_its_own_polyline(self):
        second = Order.objects.create(
            order_number=f"YR-D{Order.objects.count() + 1:04d}",
            customer=self.customer,
            status=OrderStatus.READY,
            payment_type=PaymentType.COD,
            subtotal=Decimal("299.00"),
            delivery_fee=Decimal("30.00"),
            total=Decimal("329.00"),
            delivery_address="Elsewhere",
            delivery_latitude=Decimal("7.020000"),
            delivery_longitude=Decimal("125.095000"),
        )

        self.client.force_authenticate(self.admin)
        for order in (self.order, second):
            self.client.post(
                reverse("admin-order-assign", args=[order.pk]),
                {"rider_id": self.rider.pk},
                format="json",
            )

        first_delivery = Delivery.objects.get(order=self.order)
        second_delivery = Delivery.objects.get(order=second)
        self.set_delivery_status(first_delivery, "picked_up")
        self.set_delivery_status(second_delivery, "picked_up")

        self.set_delivery_status(first_delivery, "in_transit")
        self.set_delivery_status(first_delivery, "delivered")

        self.route.return_value = {
            "duration_seconds": 300,
            "distance_meters": 1200,
            "polyline": "LEGTWOPOLYLINE",
        }
        self.set_delivery_status(second_delivery, "in_transit")

        self.order.refresh_from_db()
        second.refresh_from_db()
        # Each order keeps the route for its own leg.
        self.assertEqual(self.order.route_polyline, "TESTPOLYLINE")
        self.assertEqual(second.route_polyline, "LEGTWOPOLYLINE")

    def test_whole_journey_costs_exactly_one_call(self):
        self.assign()
        delivery = Delivery.objects.get(order=self.order)

        self.client.force_authenticate(self.rider_user)
        self.client.post(
            reverse("delivery:location"), {"lat": 7.01, "lng": 125.09}, format="json"
        )
        self.set_delivery_status(delivery, "picked_up")
        for _ in range(10):
            self.client.force_authenticate(self.rider_user)
            self.client.post(
                reverse("delivery:location"),
                {"lat": 7.02, "lng": 125.095},
                format="json",
            )
        self.set_delivery_status(delivery, "in_transit")
        for _ in range(10):
            self.client.force_authenticate(self.rider_user)
            self.client.post(
                reverse("delivery:location"),
                {"lat": 7.03, "lng": 125.098},
                format="json",
            )
        self.set_delivery_status(delivery, "delivered")

        self.assertEqual(self.route.call_count, 1)

    def test_a_batch_of_three_costs_one_call_per_leg(self):
        """One Routes call per leg driven, regardless of batch size."""
        orders = [self.order]
        for _ in range(2):
            orders.append(
                Order.objects.create(
                    order_number=f"YR-C{Order.objects.count() + 1:04d}",
                    customer=self.customer,
                    status=OrderStatus.READY,
                    payment_type=PaymentType.COD,
                    subtotal=Decimal("299.00"),
                    delivery_fee=Decimal("30.00"),
                    total=Decimal("329.00"),
                    delivery_address="Blue gate",
                    delivery_latitude=Decimal("7.040000"),
                    delivery_longitude=Decimal("125.100000"),
                )
            )

        self.client.force_authenticate(self.admin)
        for order in orders:
            self.client.post(
                reverse("admin-order-assign", args=[order.pk]),
                {"rider_id": self.rider.pk},
                format="json",
            )
        self.assertEqual(self.route.call_count, 0, "assignment must be free")

        deliveries = list(Delivery.objects.filter(rider=self.rider))
        for delivery in deliveries:
            self.set_delivery_status(delivery, "picked_up")
        self.assertEqual(self.route.call_count, 0, "pickup must be free")

        for delivery in deliveries:
            self.set_delivery_status(delivery, "in_transit")
            self.set_delivery_status(delivery, "delivered")

        self.assertEqual(self.route.call_count, 3)
