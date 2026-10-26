"""
The rider is told when the store finishes an order they are assigned to, and an
assignment made after the food is ready says so.

`notify_users` is patched at the point core.notifications calls it, so each
test sees exactly who was pushed what. A separate test checks the real token
targeting.
"""

from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, DeviceToken, Rider, User
from delivery.models import Delivery
from orders.constants import DeliveryStatus, OrderStatus
from orders.tests_admin import make_order

PASSWORD = "Kidapawan!2026"


class ReadyPushBase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        self.cust_user = User.objects.create_user("maria", "maria@example.com", PASSWORD)
        self.customer = Customer.objects.create(user=self.cust_user)
        self.rider_user = User.objects.create_user(
            "rico", "rico@example.com", PASSWORD, is_rider=True, is_customer=False
        )
        self.rider = Rider.objects.create(user=self.rider_user, is_available=True)
        self.other_rider_user = User.objects.create_user(
            "rey", "rey@example.com", PASSWORD, is_rider=True, is_customer=False
        )
        self.other_rider = Rider.objects.create(user=self.other_rider_user, is_available=True)
        self.client.force_authenticate(self.admin)

    def order(self, status_=OrderStatus.PREPARING, **kwargs):
        return make_order(self.customer, status=status_, **kwargs)

    def assigned(self, order=None, rider=None, delivery_status=DeliveryStatus.ASSIGNED):
        order = order or self.order()
        Delivery.objects.create(
            order=order,
            rider=rider or self.rider,
            rider_earnings=Decimal("30.00"),
            status=delivery_status,
        )
        return order

    def set_status(self, order, new_status):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.patch(
                reverse("admin-order-status", args=[order.pk]), {"status": new_status}, format="json"
            )

    def pushes(self, mocked):
        """[(username, title, body, data)] for every push queued."""
        out = []
        for call in mocked.call_args_list:
            user, title, body = call.args[:3]
            data = call.args[3] if len(call.args) > 3 else call.kwargs.get("data")
            out.append((user.username, title, body, data))
        return out


@patch("core.notifications.notify_users")
class RiderReadyPushTests(ReadyPushBase):
    def test_the_assigned_rider_is_told_when_the_order_is_ready(self, notify):
        order = self.assigned()

        response = self.set_status(order, "ready")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rider_pushes = [p for p in self.pushes(notify) if p[0] == "rico"]
        self.assertEqual(len(rider_pushes), 1)
        _, title, body, data = rider_pushes[0]
        self.assertEqual(title, "Order ready for pickup")
        self.assertEqual(body, f"{order.order_number} is ready. Pick it up at the store.")
        self.assertEqual(data["event"], "ready")
        self.assertEqual(data["rider_id"], self.rider.pk)
        self.assertEqual(data["order_id"], order.pk)
        self.assertEqual(data["order_number"], order.order_number)
        self.assertEqual(data["type"], "order")
        self.assertEqual(data["status"], "ready")

    def test_nothing_goes_to_the_rider_when_nobody_is_assigned(self, notify):
        order = self.order()

        self.set_status(order, "ready")

        self.assertEqual([p[0] for p in self.pushes(notify)], ["maria"])  # only the customer

    def test_only_the_assigned_rider_is_told_not_other_riders(self, notify):
        order = self.assigned(rider=self.rider)
        self.assigned(order=self.order(), rider=self.other_rider)

        self.set_status(order, "ready")

        names = [p[0] for p in self.pushes(notify) if p[1] == "Order ready for pickup"]
        self.assertEqual(names, ["rico"])

    def test_a_rider_who_already_picked_it_up_is_not_told_to_pick_it_up(self, notify):
        order = self.assigned(delivery_status=DeliveryStatus.PICKED_UP)
        self.set_status(order, "ready")
        self.assertNotIn("rico", [p[0] for p in self.pushes(notify)])

    def test_a_finished_delivery_is_not_disturbed(self, notify):
        for finished in (DeliveryStatus.DELIVERED, DeliveryStatus.FAILED):
            notify.reset_mock()
            order = self.assigned(order=self.order(), delivery_status=finished)
            self.set_status(order, "ready")
            self.assertNotIn("rico", [p[0] for p in self.pushes(notify)], finished)

    def test_other_status_changes_send_the_rider_no_ready_push(self, notify):
        for current, new in (
            (OrderStatus.PENDING, "confirmed"),
            (OrderStatus.CONFIRMED, "preparing"),
        ):
            notify.reset_mock()
            order = self.assigned(order=self.order(current))
            self.set_status(order, new)
            self.assertNotIn("Order ready for pickup", [p[1] for p in self.pushes(notify)], new)

    def test_the_customers_ready_push_is_unchanged(self, notify):
        order = self.assigned()

        self.set_status(order, "ready")

        customer = [p for p in self.pushes(notify) if p[0] == "maria"]
        self.assertEqual(len(customer), 1)
        _, title, body, data = customer[0]
        self.assertEqual(title, "Ready for pickup")
        self.assertEqual(body, f"{order.order_number} is ready and waiting for a rider.")
        self.assertEqual(data["event"], "status_change")

    def test_it_works_through_the_django_admin_mark_ready_action(self, notify):
        order = self.assigned()
        self.client.force_login(self.admin)

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("admin:orders_order_changelist"),
                {"action": "mark_ready", "_selected_action": [order.pk], "index": "0"},
                follow=True,
            )

        titles = [(p[0], p[1]) for p in self.pushes(notify)]
        self.assertIn(("rico", "Order ready for pickup"), titles)

    def test_a_bulk_action_tells_each_orders_own_rider(self, notify):
        first = self.assigned(order=self.order(), rider=self.rider)
        second = self.assigned(order=self.order(), rider=self.other_rider)
        unassigned = self.order()
        self.client.force_login(self.admin)

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("admin:orders_order_changelist"),
                {
                    "action": "mark_ready",
                    "_selected_action": [first.pk, second.pk, unassigned.pk],
                    "index": "0",
                },
                follow=True,
            )

        rider_pushes = {(p[0], p[2]) for p in self.pushes(notify) if p[1] == "Order ready for pickup"}
        self.assertEqual(
            rider_pushes,
            {
                ("rico", f"{first.order_number} is ready. Pick it up at the store."),
                ("rey", f"{second.order_number} is ready. Pick it up at the store."),
            },
        )

    def test_it_works_through_the_order_pages_advance_button(self, notify):
        order = self.assigned()
        self.client.force_login(self.admin)

        with self.captureOnCommitCallbacks(execute=True):
            self.client.get(reverse("admin:orders_order_advance_order", args=[order.pk]), follow=True)

        self.assertIn("Order ready for pickup", [p[1] for p in self.pushes(notify)])


@patch("core.notifications.notify_users")
class AssignedWhileReadyTests(ReadyPushBase):
    def assign(self, order):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse("admin-order-assign", args=[order.pk]), {"rider_id": self.rider.pk}, format="json"
            )

    def assigned_push(self, notify):
        pushes = [p for p in self.pushes(notify) if p[0] == "rico"]
        self.assertEqual(len(pushes), 1)
        return pushes[0]

    def test_an_order_assigned_after_it_is_ready_says_so(self, notify):
        order = self.order(OrderStatus.READY, delivery_address="Blue gate, Quezon Blvd")

        response = self.assign(order)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        _, title, body, data = self.assigned_push(notify)
        self.assertEqual(title, "New delivery assigned")
        self.assertEqual(body, f"{order.order_number} to Blue gate, Quezon Blvd. Ready for pickup now.")
        self.assertEqual(data["event"], "assigned")  # unchanged
        self.assertEqual(data["rider_id"], self.rider.pk)

    def test_an_order_still_being_prepared_gets_the_plain_message(self, notify):
        order = self.order(OrderStatus.PREPARING, delivery_address="Blue gate, Quezon Blvd")

        self.assign(order)

        _, _, body, data = self.assigned_push(notify)
        self.assertEqual(body, f"{order.order_number} to Blue gate, Quezon Blvd.")
        self.assertNotIn("Ready", body)
        self.assertEqual(data["event"], "assigned")

    def test_assigning_a_ready_order_sends_one_push_not_two(self, notify):
        """The ready push is for a status change; here nothing changes status."""
        order = self.order(OrderStatus.READY)
        self.assign(order)
        titles = [p[1] for p in self.pushes(notify) if p[0] == "rico"]
        self.assertEqual(titles, ["New delivery assigned"])

    def test_the_django_admin_assign_action_says_so_too(self, notify):
        order = self.order(OrderStatus.READY, delivery_address="Blue gate, Quezon Blvd")
        self.client.force_login(self.admin)

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("admin:orders_order_changelist"),
                {
                    "action": "assign_rider",
                    "_selected_action": [order.pk],
                    "rider": self.rider.pk,
                    "index": "0",
                },
                follow=True,
            )

        _, _, body, _ = self.assigned_push(notify)
        self.assertTrue(body.endswith("Ready for pickup now."))


class RealTargetingTests(ReadyPushBase):
    """No patching of notify_users: who actually gets the message."""

    def test_the_ready_push_reaches_the_riders_tokens_and_not_the_customers(self):
        DeviceToken.objects.create(user=self.rider_user, fcm_token="rider-phone", platform="android")
        DeviceToken.objects.create(user=self.cust_user, fcm_token="customer-phone", platform="android")
        order = self.assigned()

        with patch("core.fcm._dispatch") as dispatch:
            self.set_status(order, "ready")

        by_title = {call.args[1]: call.args[0] for call in dispatch.call_args_list}
        self.assertEqual(by_title["Order ready for pickup"], ["rider-phone"])
        self.assertEqual(by_title["Ready for pickup"], ["customer-phone"])

    def test_the_push_is_filed_under_the_apps_channel(self):
        from unittest.mock import MagicMock

        DeviceToken.objects.create(user=self.rider_user, fcm_token="rider-phone", platform="android")
        order = self.assigned()
        messaging = MagicMock()
        messaging.send_each_for_multicast.return_value = MagicMock(
            responses=[MagicMock(success=True)], success_count=1, failure_count=0
        )

        with patch("core.fcm._get_app", return_value=object()):
            with patch.dict("sys.modules", {"firebase_admin.messaging": messaging}):
                self.set_status(order, "ready")

        messaging.AndroidNotification.assert_called_with(channel_id="yellowribbon_orders")
