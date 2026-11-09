"""
One order flow for the three apps.

The store accepts, prepares and readies an order and may cancel it; it
assigns a rider only after accepting and may change the rider until pickup.
The rider picks up (only once the order is ready), sets off, and delivers or
reports a failed drop-off. Any cancel closes the rider's open delivery.
"""

from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, Rider, User
from delivery.models import Delivery
from orders.constants import DeliveryStatus, OrderStatus, PaymentStatus, PaymentType
from orders.models import Order
from orders.tests_admin import make_order

PASSWORD = "Kidapawan!2026"


class FlowBase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        self.cust_user = User.objects.create_user("maria", "maria@example.com", PASSWORD)
        self.customer = Customer.objects.create(user=self.cust_user)
        self.rider = self.make_rider("rico")
        self.other_rider = self.make_rider("rina")

    def make_rider(self, username):
        user = User.objects.create_user(username, f"{username}@example.com", PASSWORD, is_rider=True, is_customer=False)
        return Rider.objects.create(user=user, vehicle_type="Motorcycle", is_available=True)

    def order(self, **fields):
        return make_order(self.customer, **fields)

    def deliver(self, order, rider=None, **fields):
        return Delivery.objects.create(order=order, rider=rider or self.rider, rider_earnings=Decimal("50.00"), **fields)

    # -- as the admin --

    def set_status(self, order, value, **extra):
        self.client.force_authenticate(self.admin)
        return self.client.patch(reverse("admin-order-status", args=[order.pk]), {"status": value, **extra}, format="json")

    def assign(self, order, rider):
        self.client.force_authenticate(self.admin)
        return self.client.post(reverse("admin-order-assign", args=[order.pk]), {"rider_id": rider.pk}, format="json")

    def admin_view(self, order):
        self.client.force_authenticate(self.admin)
        return self.client.get(reverse("admin-order-detail", args=[order.pk])).data

    # -- as the rider --

    def rider_move(self, delivery, value, rider=None, **extra):
        self.client.force_authenticate((rider or self.rider).user)
        return self.client.patch(
            reverse("delivery:rider-delivery-status", args=[delivery.pk]), {"status": value, **extra}, format="json"
        )

    def rider_view(self, delivery, rider=None):
        self.client.force_authenticate((rider or self.rider).user)
        return self.client.get(reverse("delivery:rider-delivery-detail", args=[delivery.pk])).data


class AdminStatusTests(FlowBase):
    """B1: the store moves an order by hand, never out for delivery or delivered."""

    def test_the_admin_cannot_send_an_order_out_or_mark_it_delivered(self):
        ready = self.order(status=OrderStatus.READY)
        out = self.order(status=OrderStatus.OUT_FOR_DELIVERY)
        for order, target in ((ready, OrderStatus.OUT_FOR_DELIVERY), (out, OrderStatus.DELIVERED),
                              (ready, OrderStatus.DELIVERED)):
            response = self.set_status(order, target)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, (order.status, target))
            self.assertIn("rider app", str(response.data))
            order.refresh_from_db()
            self.assertNotEqual(order.status, target)

    def test_the_store_steps_still_work(self):
        order = self.order()
        for target in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
            self.assertEqual(self.set_status(order, target).status_code, status.HTTP_200_OK, target)

    def test_next_statuses_offer_only_store_steps(self):
        expected = {
            OrderStatus.PENDING_PAYMENT: ["cancelled"],
            OrderStatus.PENDING: ["cancelled", "confirmed"],
            OrderStatus.CONFIRMED: ["cancelled", "preparing"],
            OrderStatus.PREPARING: ["cancelled", "ready"],
            OrderStatus.READY: ["cancelled"],
            OrderStatus.OUT_FOR_DELIVERY: ["cancelled"],
            OrderStatus.DELIVERED: [],
            OrderStatus.CANCELLED: [],
        }
        for current, statuses in expected.items():
            data = self.admin_view(self.order(status=current))
            self.assertEqual(sorted(data["next_statuses"]), statuses, current)

    def test_the_django_admin_follows_the_same_rule(self):
        from django.contrib.admin.sites import site

        order = self.order(status=OrderStatus.READY)
        model_admin = site._registry[Order]
        request = self.client_request()
        moved = model_admin._move(request, Order.objects.filter(pk=order.pk), OrderStatus.OUT_FOR_DELIVERY)
        self.assertEqual(moved, 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.READY)

    def client_request(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        request = RequestFactory().post("/admin/")
        request.user = self.admin
        request.session = self.client.session
        request._messages = FallbackStorage(request)
        return request


class AssignTests(FlowBase):
    """B2 and B3: assign after accepting; change the rider until pickup."""

    def test_assigning_before_the_store_accepts_is_refused(self):
        for current, words in ((OrderStatus.PENDING, "Accept the order before assigning a rider."),
                               (OrderStatus.PENDING_PAYMENT, "waiting for payment")):
            order = self.order(status=current)
            response = self.assign(order, self.rider)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, current)
            self.assertIn(words, str(response.data))
            self.assertFalse(Delivery.objects.filter(order=order).exists())

    def test_assigning_once_accepted_works(self):
        for current in (OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY):
            order = self.order(status=current)
            self.assertEqual(self.assign(order, self.rider).status_code, status.HTTP_201_CREATED, current)

    def test_finished_or_sent_out_orders_cannot_be_assigned(self):
        for current in (OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED, OrderStatus.CANCELLED):
            response = self.assign(self.order(status=current), self.rider)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, current)

    def test_the_payload_says_what_the_admin_may_do(self):
        pending = self.admin_view(self.order())
        self.assertEqual((pending["can_assign_rider"], pending["can_change_rider"]), (False, False))

        confirmed = self.order(status=OrderStatus.CONFIRMED)
        self.assertEqual(self.admin_view(confirmed)["can_assign_rider"], True)

        self.deliver(confirmed)
        data = self.admin_view(confirmed)
        self.assertEqual((data["can_assign_rider"], data["can_change_rider"]), (False, True))

        ready = self.order(status=OrderStatus.READY)
        self.deliver(ready, status=DeliveryStatus.PICKED_UP)
        data = self.admin_view(ready)
        self.assertEqual((data["can_assign_rider"], data["can_change_rider"]), (False, False))

    @patch("orders.admin_api.notify_rider_unassigned")
    @patch("orders.admin_api.notify_rider_assigned")
    def test_changing_the_rider_before_pickup_hands_it_over(self, assigned, unassigned):
        order = self.order(status=OrderStatus.PREPARING)
        delivery = self.deliver(order)
        Delivery.objects.filter(pk=delivery.pk).update(rider_earnings=Decimal("1.00"))

        response = self.assign(order, self.other_rider)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        delivery.refresh_from_db()
        self.assertEqual(delivery.rider, self.other_rider)
        self.assertEqual(delivery.status, DeliveryStatus.ASSIGNED)
        self.assertNotEqual(delivery.rider_earnings, Decimal("1.00"))  # recomputed
        self.assertEqual(Delivery.objects.filter(order=order).count(), 1)
        assigned.assert_called_once_with(order_matching(order), self.other_rider)
        unassigned.assert_called_once_with(order_matching(order), self.rider)
        self.assertEqual(response.data["rider"]["username"], "rina")

        # The old rider no longer sees it or can move it.
        self.assertEqual(self.rider_move(delivery, DeliveryStatus.PICKED_UP).status_code, status.HTTP_404_NOT_FOUND)

    def test_the_same_rider_again_is_refused(self):
        order = self.order(status=OrderStatus.CONFIRMED)
        self.deliver(order)
        response = self.assign(order, self.rider)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already assigned", str(response.data))

    def test_changing_the_rider_after_pickup_is_refused(self):
        order = self.order(status=OrderStatus.OUT_FOR_DELIVERY)
        delivery = self.deliver(order, status=DeliveryStatus.PICKED_UP)
        response = self.assign(order, self.other_rider)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already picked up", str(response.data))
        delivery.refresh_from_db()
        self.assertEqual(delivery.rider, self.rider)


def order_matching(order):
    """Matches any Order instance for the same row (the view re-reads it)."""

    class _Same:
        def __eq__(self, other):
            return getattr(other, "pk", None) == order.pk

    return _Same()


class RiderPickupTests(FlowBase):
    """B4: pickup only once ready, and the rider is told why not."""

    def test_pickup_before_ready_is_refused_and_not_offered(self):
        order = self.order(status=OrderStatus.PREPARING)
        delivery = self.deliver(order)

        data = self.rider_view(delivery)
        self.assertEqual(data["next_statuses"], [])
        self.assertEqual(data["waiting_for"], "The store is still preparing this order.")

        response = self.rider_move(delivery, DeliveryStatus.PICKED_UP)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, DeliveryStatus.ASSIGNED)

    def test_once_ready_pickup_is_offered_and_works(self):
        order = self.order(status=OrderStatus.READY)
        delivery = self.deliver(order)
        data = self.rider_view(delivery)
        self.assertEqual((data["next_statuses"], data["waiting_for"]), (["picked_up"], None))
        self.assertEqual(self.rider_move(delivery, DeliveryStatus.PICKED_UP).status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.OUT_FOR_DELIVERY)

    def test_failing_is_only_for_after_pickup(self):
        order = self.order(status=OrderStatus.READY)
        delivery = self.deliver(order)
        response = self.rider_move(delivery, DeliveryStatus.FAILED, failure_reason="Flat tyre")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        Delivery.objects.filter(pk=delivery.pk).update(status=DeliveryStatus.PICKED_UP)
        self.order_out(order)
        self.assertEqual(self.rider_view(delivery)["next_statuses"], ["failed", "in_transit"])
        response = self.rider_move(delivery, DeliveryStatus.FAILED, failure_reason="Nobody home")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        delivery.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual((delivery.status, delivery.failure_reason), (DeliveryStatus.FAILED, "Nobody home"))
        self.assertIsNotNone(delivery.failed_at)
        self.assertEqual(order.status, OrderStatus.OUT_FOR_DELIVERY)  # the store decides next
        self.assertEqual(order.cancel_reason, "")

    def order_out(self, order):
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.OUT_FOR_DELIVERY)

    def test_the_admin_sees_why_a_delivery_failed(self):
        order = self.order(status=OrderStatus.OUT_FOR_DELIVERY)
        self.deliver(order, status=DeliveryStatus.FAILED, failure_reason="Nobody home")
        self.assertEqual(self.admin_view(order)["rider"]["failure_reason"], "Nobody home")


class CancelClosesDeliveryTests(FlowBase):
    """B5: any cancel closes an open delivery, and the rider cannot finish it."""

    @patch("orders.admin_api.notify_rider_order_cancelled")
    def test_an_admin_cancel_closes_the_delivery_and_tells_the_rider(self, told):
        order = self.order(status=OrderStatus.OUT_FOR_DELIVERY)
        delivery = self.deliver(order, status=DeliveryStatus.IN_TRANSIT)

        self.assertEqual(self.set_status(order, OrderStatus.CANCELLED).status_code, status.HTTP_200_OK)

        delivery.refresh_from_db()
        self.assertEqual((delivery.status, delivery.failure_reason), (DeliveryStatus.FAILED, "Order cancelled"))
        self.assertIsNotNone(delivery.failed_at)
        told.assert_called_once()

        # Gone from the rider's active list, in their history instead.
        self.client.force_authenticate(self.rider.user)
        self.assertEqual(self.client.get(reverse("delivery:rider-delivery-list")).data, [])
        history = self.client.get(reverse("delivery:rider-delivery-history")).data
        self.assertEqual([d["id"] for d in history], [delivery.pk])

    def test_the_rider_cannot_deliver_a_cancelled_order(self):
        order = self.order(status=OrderStatus.OUT_FOR_DELIVERY, payment_type=PaymentType.COD)
        delivery = self.deliver(order, status=DeliveryStatus.IN_TRANSIT)
        self.set_status(order, OrderStatus.CANCELLED)

        response = self.rider_move(delivery, DeliveryStatus.DELIVERED)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        self.assertEqual((order.status, order.payment_status), (OrderStatus.CANCELLED, PaymentStatus.UNPAID))
        self.assertFalse(hasattr(order, "invoice"))

    def test_rider_changes_are_refused_on_a_finished_order_even_with_an_odd_delivery(self):
        # Data from before this rule: the order finished but the delivery was left open.
        for current in (OrderStatus.CANCELLED, OrderStatus.DELIVERED):
            order = self.order(status=current)
            delivery = self.deliver(order, status=DeliveryStatus.IN_TRANSIT, rider=self.make_rider(f"r{current}"))
            response = self.rider_move(delivery, DeliveryStatus.DELIVERED, rider=delivery.rider)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, current)
            self.assertEqual(self.rider_view(delivery, rider=delivery.rider)["next_statuses"], [])

    def test_a_customer_cancel_closes_an_assigned_delivery(self):
        # Odd but possible in old data: assigned while still pending.
        order = self.order()
        delivery = self.deliver(order)
        self.client.force_authenticate(self.cust_user)
        response = self.client.post(reverse("orders:order-cancel", args=[order.pk]), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, DeliveryStatus.FAILED)

    def test_a_finished_delivery_is_left_alone(self):
        order = self.order(status=OrderStatus.OUT_FOR_DELIVERY)
        delivery = self.deliver(order, status=DeliveryStatus.FAILED, failure_reason="Nobody home")
        self.set_status(order, OrderStatus.CANCELLED)
        delivery.refresh_from_db()
        self.assertEqual(delivery.failure_reason, "Nobody home")


class OddStatesTests(FlowBase):
    """Orders already in states the new rules would not produce must still load."""

    def test_old_odd_combinations_do_not_crash(self):
        odd = [
            (OrderStatus.OUT_FOR_DELIVERY, DeliveryStatus.ASSIGNED),
            (OrderStatus.PENDING, DeliveryStatus.ASSIGNED),
            (OrderStatus.DELIVERED, DeliveryStatus.PICKED_UP),
        ]
        for order_status, delivery_status in odd:
            order = self.order(status=order_status)
            rider = self.make_rider(f"odd{order_status}")
            delivery = self.deliver(order, rider=rider, status=delivery_status)
            data = self.admin_view(order)
            self.assertIn("can_change_rider", data)
            self.assertIsInstance(self.rider_view(delivery, rider=rider)["next_statuses"], list)
