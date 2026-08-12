"""Rider endpoint tests."""

from datetime import timedelta
from decimal import Decimal
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, Rider, User
from menu.models import Menu
from orders.constants import (
    DeliveryStatus,
    OrderStatus,
    PaymentStatus,
    PaymentType,
    is_valid_delivery_transition,
)
from orders.models import Order, OrderStatusLog

from .models import Delivery


def photo(name="proof.png"):
    buffer = BytesIO()
    Image.new("RGB", (32, 32), (255, 204, 0)).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class RiderTestBase(APITestCase):
    def setUp(self):
        self.cust_user = User.objects.create_user(
            username="maria", email="maria@example.com", password="Kidapawan!2026"
        )
        self.customer = Customer.objects.create(user=self.cust_user)

        self.rider_user = User.objects.create_user(
            username="rico",
            email="rico@example.com",
            password="Kidapawan!2026",
            first_name="Rico",
            last_name="Cruz",
            phone="09171234567",
            is_rider=True,
            is_customer=False,
        )
        self.rider = Rider.objects.create(
            user=self.rider_user, vehicle_type="Motorcycle", is_available=True
        )

        self.other_rider_user = User.objects.create_user(
            username="bong",
            email="bong@example.com",
            password="Kidapawan!2026",
            is_rider=True,
            is_customer=False,
        )
        self.other_rider = Rider.objects.create(user=self.other_rider_user)

        self.admin = User.objects.create_superuser(
            username="boss", email="boss@example.com", password="Kidapawan!2026"
        )
        self.menu = Menu.objects.create(name="Pepperoni", price=Decimal("299.00"))

        self.order = self.make_order(status=OrderStatus.READY)
        self.delivery = Delivery.objects.create(
            order=self.order, rider=self.rider, rider_earnings=Decimal("30.00")
        )

        self.list_url = reverse("delivery:rider-delivery-list")
        self.history_url = reverse("delivery:rider-delivery-history")
        self.location_url = reverse("delivery:location")
        self.availability_url = reverse("delivery:availability")
        self.dashboard_url = reverse("delivery:dashboard")

    def make_order(self, **overrides):
        fields = {
            "order_number": f"YR-R{Order.objects.count() + 1:04d}",
            "customer": self.customer,
            "status": OrderStatus.READY,
            "payment_type": PaymentType.COD,
            "subtotal": Decimal("598.00"),
            "delivery_fee": Decimal("30.00"),
            "total": Decimal("628.00"),
            "delivery_address": "Blue gate, Quezon Blvd",
            "delivery_latitude": Decimal("7.008300"),
            "delivery_longitude": Decimal("125.089400"),
        }
        fields.update(overrides)
        return Order.objects.create(**fields)

    def status_url(self, delivery):
        return reverse("delivery:rider-delivery-status", args=[delivery.pk])

    def proof_url(self, delivery):
        return reverse("delivery:rider-delivery-proof", args=[delivery.pk])

    def advance(self, delivery, *statuses):
        self.client.force_authenticate(self.rider_user)
        for value in statuses:
            response = self.client.patch(
                self.status_url(delivery), {"status": value}, format="json"
            )
            assert response.status_code == 200, (value, response.data)
        delivery.refresh_from_db()


class DeliveryListTests(RiderTestBase):
    def test_requires_rider_role(self):
        for user in (None, self.cust_user, self.admin):
            self.client.force_authenticate(user)
            expected = (
                status.HTTP_401_UNAUTHORIZED
                if user is None
                else status.HTTP_403_FORBIDDEN
            )
            self.assertEqual(self.client.get(self.list_url).status_code, expected)

    def test_lists_active_deliveries(self):
        self.client.force_authenticate(self.rider_user)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["status"], DeliveryStatus.ASSIGNED)

    def test_rider_never_sees_another_riders_delivery(self):
        other_order = self.make_order()
        Delivery.objects.create(order=other_order, rider=self.other_rider)

        self.client.force_authenticate(self.rider_user)
        response = self.client.get(self.list_url)
        self.assertEqual(len(response.data), 1)

    def test_detail_of_another_riders_delivery_is_404(self):
        other_order = self.make_order()
        theirs = Delivery.objects.create(order=other_order, rider=self.other_rider)

        self.client.force_authenticate(self.rider_user)
        response = self.client.get(
            reverse("delivery:rider-delivery-detail", args=[theirs.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_completed_delivery_leaves_the_active_list(self):
        self.advance(self.delivery, "picked_up", "in_transit", "delivered")

        self.client.force_authenticate(self.rider_user)
        self.assertEqual(len(self.client.get(self.list_url).data), 0)
        self.assertEqual(len(self.client.get(self.history_url).data), 1)

    def test_history_excludes_active_deliveries(self):
        self.client.force_authenticate(self.rider_user)
        self.assertEqual(len(self.client.get(self.history_url).data), 0)

    def test_payload_carries_what_the_rider_needs(self):
        self.client.force_authenticate(self.rider_user)
        item = self.client.get(self.list_url).data[0]

        self.assertEqual(item["rider_earnings"], "30.00")
        self.assertEqual(item["next_statuses"], ["failed", "picked_up"])

        order = item["order"]
        self.assertEqual(order["order_number"], self.order.order_number)
        # maria has no first/last name, so the username is the fallback.
        self.assertEqual(order["customer_name"], "maria")
        self.assertEqual(order["customer_phone"], "")
        self.assertEqual(order["delivery_address"], "Blue gate, Quezon Blvd")
        self.assertEqual(order["delivery_latitude"], "7.008300")

    def test_cod_shows_the_amount_to_collect(self):
        self.client.force_authenticate(self.rider_user)
        order = self.client.get(self.list_url).data[0]["order"]
        self.assertEqual(order["amount_to_collect"], "628.00")

    def test_paid_gcash_collects_nothing(self):
        self.order.payment_type = PaymentType.GCASH
        self.order.payment_status = PaymentStatus.PAID
        self.order.save()

        self.client.force_authenticate(self.rider_user)
        order = self.client.get(self.list_url).data[0]["order"]
        self.assertEqual(order["amount_to_collect"], "0.00")


class DeliveryStatusTests(RiderTestBase):
    def test_transition_table(self):
        path = [
            DeliveryStatus.ASSIGNED,
            DeliveryStatus.PICKED_UP,
            DeliveryStatus.IN_TRANSIT,
            DeliveryStatus.DELIVERED,
        ]
        for current, nxt in zip(path, path[1:]):
            self.assertTrue(is_valid_delivery_transition(current, nxt))

        self.assertFalse(
            is_valid_delivery_transition(
                DeliveryStatus.ASSIGNED, DeliveryStatus.DELIVERED
            )
        )
        self.assertFalse(
            is_valid_delivery_transition(
                DeliveryStatus.DELIVERED, DeliveryStatus.IN_TRANSIT
            )
        )

    def test_happy_path(self):
        self.client.force_authenticate(self.rider_user)
        for value in ("picked_up", "in_transit", "delivered"):
            response = self.client.patch(
                self.status_url(self.delivery), {"status": value}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, value)

        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, DeliveryStatus.DELIVERED)
        self.assertIsNotNone(self.delivery.picked_up_at)
        self.assertIsNotNone(self.delivery.delivered_at)

    def test_cannot_skip_to_delivered(self):
        self.client.force_authenticate(self.rider_user)
        response = self.client.patch(
            self.status_url(self.delivery), {"status": "delivered"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, DeliveryStatus.ASSIGNED)

    def test_cannot_move_backwards(self):
        self.advance(self.delivery, "picked_up", "in_transit")

        self.client.force_authenticate(self.rider_user)
        response = self.client.patch(
            self.status_url(self.delivery), {"status": "picked_up"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_terminal_delivery_cannot_change(self):
        self.advance(self.delivery, "picked_up", "in_transit", "delivered")

        self.client.force_authenticate(self.rider_user)
        response = self.client.patch(
            self.status_url(self.delivery), {"status": "failed"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no longer change", response.data["error"])

    def test_cannot_pick_up_an_order_that_is_not_ready(self):
        self.order.status = OrderStatus.PREPARING
        self.order.save()

        self.client.force_authenticate(self.rider_user)
        response = self.client.patch(
            self.status_url(self.delivery), {"status": "picked_up"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not ready for pickup", response.data["error"])
        self.delivery.refresh_from_db()
        self.assertEqual(self.delivery.status, DeliveryStatus.ASSIGNED)

    def test_pickup_moves_the_order_out_for_delivery(self):
        self.advance(self.delivery, "picked_up")

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.OUT_FOR_DELIVERY)

    def test_completion_moves_the_order_to_delivered(self):
        self.advance(self.delivery, "picked_up", "in_transit", "delivered")

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.DELIVERED)
        self.assertIsNotNone(self.order.delivered_at)

    def test_cod_order_is_marked_paid_on_completion(self):
        self.advance(self.delivery, "picked_up", "in_transit", "delivered")

        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)

    def test_gcash_payment_status_is_left_alone(self):
        self.order.payment_type = PaymentType.GCASH
        self.order.payment_status = PaymentStatus.PAID
        self.order.save()

        self.advance(self.delivery, "picked_up", "in_transit", "delivered")
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)

    def test_order_status_logs_are_written(self):
        self.advance(self.delivery, "picked_up", "in_transit", "delivered")

        statuses = list(
            OrderStatusLog.objects.filter(order=self.order)
            .order_by("changed_at", "id")
            .values_list("status", flat=True)
        )
        self.assertEqual(
            statuses, [OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED]
        )

    def test_failed_delivery_leaves_the_order_for_the_admin(self):
        self.advance(self.delivery, "picked_up")

        self.client.force_authenticate(self.rider_user)
        response = self.client.patch(
            self.status_url(self.delivery),
            {"status": "failed", "failure_reason": "Nobody at the gate"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.OUT_FOR_DELIVERY)
        self.assertEqual(self.order.cancel_reason, "Nobody at the gate")

    def test_cannot_touch_another_riders_delivery(self):
        other_order = self.make_order()
        theirs = Delivery.objects.create(order=other_order, rider=self.other_rider)

        self.client.force_authenticate(self.rider_user)
        response = self.client.patch(
            self.status_url(theirs), {"status": "picked_up"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, DeliveryStatus.ASSIGNED)

    def test_completed_delivery_frees_the_rider(self):
        self.advance(self.delivery, "picked_up", "in_transit", "delivered")

        self.client.force_authenticate(self.admin)
        available = self.client.get(reverse("admin-rider-available")).data
        self.assertEqual([r["username"] for r in available], ["rico"])


class BatchDeliveryTests(RiderTestBase):
    """
    A rider holds a batch: many at assigned/picked_up, one in_transit.

    Keeping in_transit exclusive is what lets the EDT stay a single
    origin-to-destination leg with no waypoints.
    """

    def setUp(self):
        super().setUp()
        # self.delivery from the base class is the first of the batch.
        self.second_order = self.make_order(status=OrderStatus.READY)
        self.second = Delivery.objects.create(
            order=self.second_order, rider=self.rider, rider_earnings=Decimal("30.00")
        )
        self.third_order = self.make_order(status=OrderStatus.READY)
        self.third = Delivery.objects.create(
            order=self.third_order, rider=self.rider, rider_earnings=Decimal("30.00")
        )

    def set_status(self, delivery, value):
        self.client.force_authenticate(self.rider_user)
        return self.client.patch(
            self.status_url(delivery), {"status": value}, format="json"
        )

    def test_the_whole_batch_shows_in_the_active_list(self):
        self.client.force_authenticate(self.rider_user)
        response = self.client.get(self.list_url)
        self.assertEqual(len(response.data), 3)

    def test_every_delivery_in_the_batch_can_be_picked_up(self):
        for delivery in (self.delivery, self.second, self.third):
            response = self.set_status(delivery, "picked_up")
            self.assertEqual(response.status_code, status.HTTP_200_OK)

        statuses = set(
            Delivery.objects.filter(rider=self.rider).values_list("status", flat=True)
        )
        self.assertEqual(statuses, {DeliveryStatus.PICKED_UP})

    def test_only_one_delivery_may_be_in_transit(self):
        self.set_status(self.delivery, "picked_up")
        self.set_status(self.second, "picked_up")

        first = self.set_status(self.delivery, "in_transit")
        self.assertEqual(first.status_code, status.HTTP_200_OK)

        blocked = self.set_status(self.second, "in_transit")
        self.assertEqual(blocked.status_code, status.HTTP_400_BAD_REQUEST)

        self.second.refresh_from_db()
        self.assertEqual(self.second.status, DeliveryStatus.PICKED_UP)

    def test_the_refusal_names_the_order_in_progress(self):
        self.set_status(self.delivery, "picked_up")
        self.set_status(self.second, "picked_up")
        self.set_status(self.delivery, "in_transit")

        blocked = self.set_status(self.second, "in_transit")
        self.assertIn(self.order.order_number, blocked.data["error"])
        self.assertIn("already delivering", blocked.data["error"])

    def test_the_next_leg_starts_once_the_first_is_delivered(self):
        self.set_status(self.delivery, "picked_up")
        self.set_status(self.second, "picked_up")
        self.set_status(self.delivery, "in_transit")
        self.set_status(self.delivery, "delivered")

        response = self.set_status(self.second, "in_transit")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_next_leg_starts_once_the_first_fails(self):
        self.set_status(self.delivery, "picked_up")
        self.set_status(self.second, "picked_up")
        self.set_status(self.delivery, "in_transit")
        self.set_status(self.delivery, "failed")

        response = self.set_status(self.second, "in_transit")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_whole_batch_can_be_worked_through_in_sequence(self):
        for delivery in (self.delivery, self.second, self.third):
            self.assertEqual(
                self.set_status(delivery, "picked_up").status_code, status.HTTP_200_OK
            )

        for delivery in (self.delivery, self.second, self.third):
            self.assertEqual(
                self.set_status(delivery, "in_transit").status_code,
                status.HTTP_200_OK,
            )
            self.assertEqual(
                self.set_status(delivery, "delivered").status_code, status.HTTP_200_OK
            )

        self.assertEqual(
            Delivery.objects.filter(
                rider=self.rider, status=DeliveryStatus.DELIVERED
            ).count(),
            3,
        )

    def test_another_riders_in_transit_delivery_is_not_a_blocker(self):
        other_order = self.make_order(status=OrderStatus.READY)
        Delivery.objects.create(
            order=other_order,
            rider=self.other_rider,
            status=DeliveryStatus.IN_TRANSIT,
        )

        self.set_status(self.delivery, "picked_up")
        response = self.set_status(self.delivery, "in_transit")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_database_refuses_a_second_in_transit_row(self):
        """The serializer check is not the only guard — two taps could race."""
        self.set_status(self.delivery, "picked_up")
        self.set_status(self.second, "picked_up")
        self.set_status(self.delivery, "in_transit")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Delivery.objects.filter(pk=self.second.pk).update(
                    status=DeliveryStatus.IN_TRANSIT
                )

    def test_two_riders_may_each_have_one_in_transit(self):
        other_order = self.make_order(status=OrderStatus.READY)
        other = Delivery.objects.create(order=other_order, rider=self.other_rider)

        self.set_status(self.delivery, "picked_up")
        self.set_status(self.delivery, "in_transit")

        # Constraint is per rider, so this must not collide.
        Delivery.objects.filter(pk=other.pk).update(status=DeliveryStatus.IN_TRANSIT)
        other.refresh_from_db()
        self.assertEqual(other.status, DeliveryStatus.IN_TRANSIT)


class ProofOfDeliveryTests(RiderTestBase):
    def test_requires_pickup_first(self):
        self.client.force_authenticate(self.rider_user)
        response = self.client.post(
            self.proof_url(self.delivery),
            {"proof_of_delivery": photo()},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Pick the order up", response.data["error"])

    def test_upload_after_pickup(self):
        self.advance(self.delivery, "picked_up", "in_transit")

        self.client.force_authenticate(self.rider_user)
        response = self.client.post(
            self.proof_url(self.delivery),
            {"proof_of_delivery": photo()},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.delivery.refresh_from_db()
        self.assertTrue(self.delivery.proof_of_delivery)
        self.assertIn("delivery_proofs/", self.delivery.proof_of_delivery.name)

    def test_response_returns_an_absolute_url(self):
        self.advance(self.delivery, "picked_up", "in_transit")

        self.client.force_authenticate(self.rider_user)
        response = self.client.post(
            self.proof_url(self.delivery),
            {"proof_of_delivery": photo()},
            format="multipart",
        )
        self.assertTrue(response.data["proof_of_delivery"].startswith("http"))

    def test_missing_file_rejected(self):
        self.advance(self.delivery, "picked_up")

        self.client.force_authenticate(self.rider_user)
        response = self.client.post(self.proof_url(self.delivery), {}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_upload_to_another_riders_delivery(self):
        other_order = self.make_order()
        theirs = Delivery.objects.create(
            order=other_order, rider=self.other_rider, status=DeliveryStatus.IN_TRANSIT
        )

        self.client.force_authenticate(self.rider_user)
        response = self.client.post(
            self.proof_url(theirs), {"proof_of_delivery": photo()}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def tearDown(self):
        for delivery in Delivery.objects.exclude(proof_of_delivery=""):
            if delivery.proof_of_delivery:
                delivery.proof_of_delivery.delete(save=False)


class LocationPingTests(RiderTestBase):
    def test_requires_rider_role(self):
        for user in (None, self.cust_user, self.admin):
            self.client.force_authenticate(user)
            expected = (
                status.HTTP_401_UNAUTHORIZED
                if user is None
                else status.HTTP_403_FORBIDDEN
            )
            response = self.client.post(
                self.location_url, {"lat": 7.0, "lng": 125.0}, format="json"
            )
            self.assertEqual(response.status_code, expected)

    def test_ping_overwrites_coordinates(self):
        self.client.force_authenticate(self.rider_user)
        response = self.client.post(
            self.location_url, {"lat": 7.008300, "lng": 125.089400}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.rider.refresh_from_db()
        self.assertEqual(self.rider.current_latitude, Decimal("7.008300"))
        self.assertEqual(self.rider.current_longitude, Decimal("125.089400"))
        self.assertIsNotNone(self.rider.last_location_update)

    def test_second_ping_replaces_the_first(self):
        self.client.force_authenticate(self.rider_user)
        self.client.post(self.location_url, {"lat": 7.0, "lng": 125.0}, format="json")
        self.client.post(self.location_url, {"lat": 7.05, "lng": 125.05}, format="json")

        self.rider.refresh_from_db()
        self.assertEqual(self.rider.current_latitude, Decimal("7.050000"))
        # Rider rows are overwritten, never appended.
        self.assertEqual(Rider.objects.count(), 2)

    def test_ping_writes_no_status_log(self):
        before = OrderStatusLog.objects.count()

        self.client.force_authenticate(self.rider_user)
        for _ in range(5):
            self.client.post(
                self.location_url, {"lat": 7.0, "lng": 125.0}, format="json"
            )

        self.assertEqual(OrderStatusLog.objects.count(), before)

    def test_ping_is_a_single_update_query(self):
        self.client.force_authenticate(self.rider_user)
        # One UPDATE. Anything more means a serializer or instance load crept in.
        with self.assertNumQueries(1):
            self.client.post(
                self.location_url, {"lat": 7.0, "lng": 125.0}, format="json"
            )

    def test_missing_coordinates_rejected(self):
        self.client.force_authenticate(self.rider_user)
        for body in ({}, {"lat": 7.0}, {"lng": 125.0}):
            response = self.client.post(self.location_url, body, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, body)

    def test_non_numeric_coordinates_rejected(self):
        self.client.force_authenticate(self.rider_user)
        response = self.client.post(
            self.location_url, {"lat": "north", "lng": "west"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_out_of_range_coordinates_rejected(self):
        self.client.force_authenticate(self.rider_user)
        for body in ({"lat": 91, "lng": 0}, {"lat": 0, "lng": 181}):
            response = self.client.post(self.location_url, body, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, body)

    def test_ping_is_not_geofenced(self):
        """A rider may briefly leave the service area; that is not an error."""
        self.client.force_authenticate(self.rider_user)
        response = self.client.post(
            self.location_url, {"lat": 14.5995, "lng": 120.9842}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


class AvailabilityTests(RiderTestBase):
    def test_toggle_off_and_on(self):
        self.client.force_authenticate(self.rider_user)

        response = self.client.patch(
            self.availability_url, {"is_available": False}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_available"])
        self.rider.refresh_from_db()
        self.assertFalse(self.rider.is_available)

        self.client.patch(
            self.availability_url, {"is_available": True}, format="json"
        )
        self.rider.refresh_from_db()
        self.assertTrue(self.rider.is_available)

    def test_missing_field_rejected(self):
        self.client.force_authenticate(self.rider_user)
        response = self.client.patch(self.availability_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_requires_rider_role(self):
        self.client.force_authenticate(self.cust_user)
        response = self.client.patch(
            self.availability_url, {"is_available": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_going_off_duty_removes_the_rider_from_available(self):
        self.client.force_authenticate(self.rider_user)
        self.client.patch(
            self.availability_url, {"is_available": False}, format="json"
        )

        self.client.force_authenticate(self.admin)
        available = self.client.get(reverse("admin-rider-available")).data
        self.assertNotIn("rico", [r["username"] for r in available])


class RiderDashboardTests(RiderTestBase):
    def test_requires_rider_role(self):
        self.client.force_authenticate(self.cust_user)
        self.assertEqual(
            self.client.get(self.dashboard_url).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_counts_and_earnings(self):
        self.advance(self.delivery, "picked_up", "in_transit", "delivered")

        second = self.make_order(status=OrderStatus.READY)
        Delivery.objects.create(
            order=second, rider=self.rider, rider_earnings=Decimal("30.00")
        )

        self.client.force_authenticate(self.rider_user)
        data = self.client.get(self.dashboard_url).data

        self.assertEqual(data["assigned"], 1)
        self.assertEqual(data["completed"], 1)
        self.assertEqual(data["failed"], 0)
        self.assertEqual(data["earnings"]["total"], "30.00")
        self.assertEqual(data["earnings"]["today"], "30.00")
        self.assertEqual(data["today"]["completed"], 1)

    def test_earnings_exclude_other_riders(self):
        other_order = self.make_order()
        Delivery.objects.create(
            order=other_order,
            rider=self.other_rider,
            status=DeliveryStatus.DELIVERED,
            delivered_at=timezone.now(),
            rider_earnings=Decimal("500.00"),
        )

        self.client.force_authenticate(self.rider_user)
        data = self.client.get(self.dashboard_url).data
        self.assertEqual(data["earnings"]["total"], "0.00")

    def test_earnings_exclude_incomplete_deliveries(self):
        self.client.force_authenticate(self.rider_user)
        data = self.client.get(self.dashboard_url).data
        self.assertEqual(data["earnings"]["total"], "0.00")
        self.assertEqual(data["assigned"], 1)

    def test_todays_earnings_exclude_yesterday(self):
        self.advance(self.delivery, "picked_up", "in_transit", "delivered")
        Delivery.objects.filter(pk=self.delivery.pk).update(
            delivered_at=timezone.now() - timedelta(days=1)
        )

        self.client.force_authenticate(self.rider_user)
        data = self.client.get(self.dashboard_url).data
        self.assertEqual(data["earnings"]["total"], "30.00")
        self.assertEqual(data["earnings"]["today"], "0.00")

    def test_reports_current_location(self):
        self.client.force_authenticate(self.rider_user)
        self.client.post(
            self.location_url, {"lat": 7.0083, "lng": 125.0894}, format="json"
        )

        data = self.client.get(self.dashboard_url).data
        self.assertEqual(data["current_location"]["latitude"], "7.008300")
        self.assertIsNotNone(data["current_location"]["updated_at"])


class CustomerSeesRiderLocationTests(RiderTestBase):
    """The tracking map on the customer's order detail."""

    def detail(self):
        self.client.force_authenticate(self.cust_user)
        return self.client.get(
            reverse("orders:order-detail", args=[self.order.pk])
        ).data

    def ping(self):
        self.client.force_authenticate(self.rider_user)
        self.client.post(
            self.location_url, {"lat": 7.0083, "lng": 125.0894}, format="json"
        )

    def test_null_before_the_rider_reports(self):
        self.assertIsNone(self.detail()["rider_location"])

    def test_visible_once_assigned_and_reporting(self):
        self.ping()
        location = self.detail()["rider_location"]

        self.assertEqual(location["latitude"], "7.008300")
        self.assertEqual(location["longitude"], "125.089400")
        self.assertEqual(location["rider_name"], "Rico Cruz")
        self.assertEqual(location["vehicle_type"], "Motorcycle")
        self.assertEqual(location["delivery_status"], DeliveryStatus.ASSIGNED)

    def test_hidden_once_the_delivery_is_done(self):
        self.ping()
        self.advance(self.delivery, "picked_up", "in_transit", "delivered")
        self.assertIsNone(self.detail()["rider_location"])

    def test_customer_cannot_see_a_rider_on_someone_elses_order(self):
        self.ping()
        stranger = User.objects.create_user(
            username="pedro", email="pedro@example.com", password="Kidapawan!2026"
        )
        Customer.objects.create(user=stranger)

        self.client.force_authenticate(stranger)
        response = self.client.get(
            reverse("orders:order-detail", args=[self.order.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
