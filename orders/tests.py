"""Order placement, cancellation and access-scoping tests."""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, Rider, User
from core.geofence import haversine_km
from menu.models import Menu

from .constants import (
    ORDER_STATUS,
    OrderStatus,
    PaymentStatus,
    PaymentType,
    is_valid_transition,
)
from .models import Order, OrderItem, OrderStatusLog

# Kidapawan City centre, per settings.
IN_AREA = {"delivery_latitude": "7.008300", "delivery_longitude": "125.089400"}
# ~45 km east-north-east of centre, comfortably outside the 8 km radius.
OUT_OF_AREA = {"delivery_latitude": "7.190700", "delivery_longitude": "125.455300"}


class OrderTestBase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="maria", email="maria@example.com", password="Kidapawan!2026"
        )
        self.customer = Customer.objects.create(user=self.user)

        self.other_user = User.objects.create_user(
            username="pedro", email="pedro@example.com", password="Kidapawan!2026"
        )
        self.other_customer = Customer.objects.create(user=self.other_user)

        self.admin = User.objects.create_superuser(
            username="boss", email="boss@example.com", password="Kidapawan!2026"
        )

        self.pizza = Menu.objects.create(name="Pepperoni", price=Decimal("299.00"))
        self.pasta = Menu.objects.create(name="Carbonara", price=Decimal("150.50"))
        self.sold_out = Menu.objects.create(
            name="Seasonal", price=Decimal("399.00"), is_available=False
        )

        self.list_url = reverse("orders:order-list")

    def payload(self, **overrides):
        body = {
            "items": [{"menu_id": self.pizza.pk, "quantity": 2}],
            "delivery_address": "Blue gate, Quezon Blvd",
            "delivery_notes": "Beside the sari-sari store",
            "payment_type": PaymentType.COD.value,
            **IN_AREA,
        }
        body.update(overrides)
        return body

    def place(self, **overrides):
        self.client.force_authenticate(self.user)
        return self.client.post(self.list_url, self.payload(**overrides), format="json")


class GeofenceUtilTests(APITestCase):
    def test_haversine_matches_known_distance(self):
        km = haversine_km(7.0083, 125.0894, 7.1907, 125.4553)
        self.assertAlmostEqual(km, 45.18, delta=0.1)

    def test_zero_distance(self):
        self.assertEqual(haversine_km(7.0083, 125.0894, 7.0083, 125.0894), 0.0)

    def test_one_degree_of_latitude_is_about_111_km(self):
        self.assertAlmostEqual(haversine_km(7.0, 125.0, 8.0, 125.0), 111.19, delta=0.1)

    def test_symmetric(self):
        a = haversine_km(7.0083, 125.0894, 7.1907, 125.4553)
        b = haversine_km(7.1907, 125.4553, 7.0083, 125.0894)
        self.assertAlmostEqual(a, b, places=9)


class OrderPlacementTests(OrderTestBase):
    def test_cod_order_lands_pending_and_unpaid(self):
        response = self.place()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get()
        self.assertEqual(order.status, OrderStatus.PENDING)
        self.assertEqual(order.payment_status, PaymentStatus.UNPAID)
        self.assertEqual(order.customer, self.customer)

    def test_gcash_order_is_gated_at_pending_payment(self):
        """An unpaid GCash order must not reach the kitchen."""
        response = self.place(payment_type=PaymentType.GCASH.value)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(order.payment_status, PaymentStatus.UNPAID)

    def test_totals_are_computed_server_side(self):
        response = self.place(
            items=[
                {"menu_id": self.pizza.pk, "quantity": 2},
                {"menu_id": self.pasta.pk, "quantity": 1},
            ]
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get()
        self.assertEqual(order.subtotal, Decimal("748.50"))  # 299*2 + 150.50
        self.assertEqual(order.delivery_fee, Decimal("30.00"))
        self.assertEqual(order.total, Decimal("778.50"))

    def test_client_supplied_totals_are_ignored(self):
        response = self.place(
            subtotal="1.00", delivery_fee="0.00", total="1.00", order_number="YR-9999"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get()
        self.assertEqual(order.total, Decimal("628.00"))  # 299*2 + 30
        self.assertNotEqual(order.order_number, "YR-9999")
        self.assertRegex(order.order_number, r"^YR-\d{4,}$")

    def test_unit_price_is_snapshotted_at_checkout(self):
        self.place()
        item = OrderItem.objects.get()
        self.assertEqual(item.unit_price, Decimal("299.00"))

        self.pizza.price = Decimal("999.00")
        self.pizza.save()

        item.refresh_from_db()
        self.assertEqual(item.unit_price, Decimal("299.00"))
        self.assertEqual(item.line_subtotal, Decimal("598.00"))
        self.assertEqual(Order.objects.get().total, Decimal("628.00"))

    def test_order_numbers_are_sequential_and_padded(self):
        for _ in range(3):
            self.place()
        numbers = list(
            Order.objects.order_by("id").values_list("order_number", flat=True)
        )

        # Absolute values are not asserted: the backing sequence is not rolled
        # back between tests, which is exactly the property that stops two
        # concurrent checkouts sharing a number.
        for number in numbers:
            self.assertRegex(number, r"^YR-\d{4,}$")

        counters = [int(n.removeprefix("YR-")) for n in numbers]
        self.assertEqual(counters, [counters[0], counters[0] + 1, counters[0] + 2])
        self.assertEqual(len(set(numbers)), 3)

    def test_status_log_written_on_placement(self):
        self.place()
        log = OrderStatusLog.objects.get()
        self.assertEqual(log.status, OrderStatus.PENDING)
        self.assertEqual(log.changed_by, self.user)

    def test_repeat_menu_lines_are_merged(self):
        response = self.place(
            items=[
                {"menu_id": self.pizza.pk, "quantity": 2},
                {"menu_id": self.pizza.pk, "quantity": 3},
            ]
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        item = OrderItem.objects.get()
        self.assertEqual(item.quantity, 5)
        self.assertEqual(item.line_subtotal, Decimal("1495.00"))

    def test_response_includes_items_and_eta_keys(self):
        data = self.place().data
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["menu_name"], "Pepperoni")
        self.assertIn("eta_minutes", data)
        self.assertIn("eta_at", data)
        self.assertIn("rider_location", data)


class GeofenceOrderingTests(OrderTestBase):
    """Spec section 8 step 1: the geofence runs before anything else."""

    def test_out_of_area_is_rejected(self):
        response = self.place(**OUT_OF_AREA)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Kidapawan", response.data["error"])
        self.assertEqual(Order.objects.count(), 0)

    def test_error_message_is_customer_readable(self):
        """It goes straight onto a phone screen — no serializer jargon."""
        response = self.place(**OUT_OF_AREA)
        message = response.data["error"]

        self.assertNotIn("non_field_errors", message)
        self.assertTrue(message.startswith("We only deliver"), message)
        self.assertIn("8 km", message)

    def test_geofence_runs_before_menu_validation(self):
        """Bad location AND a bad menu id -> the location error wins."""
        response = self.place(
            items=[{"menu_id": 999999, "quantity": 1}], **OUT_OF_AREA
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Kidapawan", response.data["error"])

    def test_nothing_is_created_when_the_geofence_rejects(self):
        self.place(**OUT_OF_AREA)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)
        self.assertEqual(OrderStatusLog.objects.count(), 0)

    def test_radius_is_read_from_the_store_settings(self):
        from core.models import StoreSettings

        store = StoreSettings.load()
        store.service_area_radius_km = 100
        store.save()
        response = self.place(**OUT_OF_AREA)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_just_inside_the_boundary_is_accepted(self):
        # 7.005 km north of centre, inside the 8 km radius.
        response = self.place(
            delivery_latitude="7.071300", delivery_longitude="125.089400"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_just_outside_the_boundary_is_rejected(self):
        # 8.045 km north of centre, outside the 8 km radius.
        response = self.place(
            delivery_latitude="7.080654", delivery_longitude="125.089400"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Order.objects.count(), 0)


class MenuValidationTests(OrderTestBase):
    def test_unknown_menu_id_rejected(self):
        response = self.place(items=[{"menu_id": 999999, "quantity": 1}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Order.objects.count(), 0)

    def test_unavailable_item_rejected(self):
        response = self.place(items=[{"menu_id": self.sold_out.pk, "quantity": 1}])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("unavailable", response.data["error"].lower())
        self.assertEqual(Order.objects.count(), 0)

    def test_empty_items_rejected(self):
        response = self.place(items=[])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_zero_quantity_rejected(self):
        response = self.place(items=[{"menu_id": self.pizza.pk, "quantity": 0}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_negative_quantity_rejected(self):
        response = self.place(items=[{"menu_id": self.pizza.pk, "quantity": -5}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bad_payment_type_rejected(self):
        response = self.place(payment_type="bitcoin")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_placement_is_atomic(self):
        """A partial order must never survive."""
        self.place(items=[{"menu_id": self.sold_out.pk, "quantity": 1}])
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)


class OrderAccessTests(OrderTestBase):
    def setUp(self):
        super().setUp()
        self.place()
        self.order = Order.objects.get()

    def detail_url(self, order):
        return reverse("orders:order-detail", args=[order.pk])

    def test_anonymous_is_rejected(self):
        self.client.force_authenticate(None)
        self.assertEqual(
            self.client.get(self.list_url).status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_customer_lists_only_their_own_orders(self):
        Order.objects.create(
            order_number="YR-9000",
            customer=self.other_customer,
            payment_type=PaymentType.COD,
            subtotal=Decimal("10.00"),
            delivery_fee=Decimal("30.00"),
            total=Decimal("40.00"),
            delivery_address="Elsewhere",
            delivery_latitude=Decimal("7.008300"),
            delivery_longitude=Decimal("125.089400"),
        )

        self.client.force_authenticate(self.user)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        numbers = [o["order_number"] for o in response.data]
        self.assertEqual(numbers, [self.order.order_number])

    def test_customer_cannot_read_another_customers_order(self):
        self.client.force_authenticate(self.other_user)
        response = self.client.get(self.detail_url(self.order))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_cannot_use_the_customer_endpoint(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_detail_includes_items_and_status_logs(self):
        self.client.force_authenticate(self.user)
        data = self.client.get(self.detail_url(self.order)).data

        self.assertEqual(data["order_number"], self.order.order_number)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(len(data["status_logs"]), 1)


class OrderCancelTests(OrderTestBase):
    def setUp(self):
        super().setUp()
        self.place()
        self.order = Order.objects.get()

    def cancel_url(self, order):
        return reverse("orders:order-cancel", args=[order.pk])

    def test_pending_order_can_be_cancelled(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            self.cancel_url(self.order), {"cancel_reason": "Changed my mind"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertEqual(self.order.cancel_reason, "Changed my mind")
        self.assertIsNotNone(self.order.cancelled_at)

    def test_cancel_writes_a_status_log(self):
        self.client.force_authenticate(self.user)
        self.client.post(self.cancel_url(self.order), {}, format="json")

        statuses = list(
            OrderStatusLog.objects.filter(order=self.order).values_list(
                "status", flat=True
            )
        )
        self.assertEqual(statuses, [OrderStatus.PENDING, OrderStatus.CANCELLED])

    def test_cannot_cancel_once_confirmed(self):
        self.order.status = OrderStatus.CONFIRMED
        self.order.save()

        self.client.force_authenticate(self.user)
        response = self.client.post(self.cancel_url(self.order), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CONFIRMED)

    def test_cannot_cancel_a_delivered_order(self):
        self.order.status = OrderStatus.DELIVERED
        self.order.save()

        self.client.force_authenticate(self.user)
        response = self.client.post(self.cancel_url(self.order), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_cancel_someone_elses_order(self):
        self.client.force_authenticate(self.other_user)
        response = self.client.post(self.cancel_url(self.order), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING)

    def test_double_cancel_is_rejected(self):
        self.client.force_authenticate(self.user)
        self.client.post(self.cancel_url(self.order), {}, format="json")
        response = self.client.post(self.cancel_url(self.order), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class StatusTransitionTests(APITestCase):
    """Spec section 5: illegal jumps are rejected."""

    def test_happy_path_steps_are_legal(self):
        path = [
            OrderStatus.PENDING,
            OrderStatus.CONFIRMED,
            OrderStatus.PREPARING,
            OrderStatus.READY,
            OrderStatus.OUT_FOR_DELIVERY,
            OrderStatus.DELIVERED,
        ]
        for current, nxt in zip(path, path[1:]):
            self.assertTrue(is_valid_transition(current, nxt), f"{current}->{nxt}")

    def test_pending_cannot_jump_to_delivered(self):
        self.assertFalse(
            is_valid_transition(OrderStatus.PENDING, OrderStatus.DELIVERED)
        )

    def test_cannot_skip_preparing(self):
        self.assertFalse(is_valid_transition(OrderStatus.CONFIRMED, OrderStatus.READY))

    def test_terminal_statuses_are_dead_ends(self):
        for terminal in (OrderStatus.DELIVERED, OrderStatus.CANCELLED):
            for target in OrderStatus:
                self.assertFalse(is_valid_transition(terminal, target))

    def test_cannot_move_backwards(self):
        self.assertFalse(
            is_valid_transition(OrderStatus.PREPARING, OrderStatus.CONFIRMED)
        )

    def test_gcash_gate_only_opens_to_pending(self):
        self.assertTrue(
            is_valid_transition(OrderStatus.PENDING_PAYMENT, OrderStatus.PENDING)
        )
        self.assertFalse(
            is_valid_transition(OrderStatus.PENDING_PAYMENT, OrderStatus.CONFIRMED)
        )


class ProtectedMenuTests(OrderTestBase):
    """A menu item referenced by an order cannot be deleted (on_delete=PROTECT)."""

    def test_menu_with_orders_cannot_be_deleted(self):
        self.place()
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.pizza.delete()

    def test_admin_delete_endpoint_returns_an_error_not_a_500(self):
        self.place()
        self.client.force_authenticate(self.admin)
        response = self.client.delete(reverse("menu:menu-detail", args=[self.pizza.pk]))

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(Menu.objects.filter(pk=self.pizza.pk).exists())


class ConstantsEndpointTests(OrderTestBase):
    url = reverse("constants")

    def test_requires_authentication(self):
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_returns_enums_fee_and_service_area(self):
        self.client.force_authenticate(self.user)
        data = self.client.get(self.url).data

        self.assertEqual(data["order_status"], ORDER_STATUS)
        self.assertEqual(data["payment_type"], ["cod", "gcash", "maya"])
        self.assertEqual(data["delivery_fee"], "30.00")
        self.assertEqual(data["service_area"]["radius_km"], 8.0)
        self.assertIn("center_lat", data["service_area"])

    def test_all_roles_can_read_it(self):
        rider_user = User.objects.create_user(
            username="rico",
            email="rico@example.com",
            password="Kidapawan!2026",
            is_rider=True,
            is_customer=False,
        )
        Rider.objects.create(user=rider_user)

        for user in (self.user, self.admin, rider_user):
            self.client.force_authenticate(user)
            self.assertEqual(
                self.client.get(self.url).status_code, status.HTTP_200_OK, user
            )


class UniqueLineConstraintTests(OrderTestBase):
    def test_duplicate_line_is_rejected_at_the_database(self):
        self.place()
        order = Order.objects.get()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OrderItem.objects.create(
                    order=order,
                    menu=self.pizza,
                    quantity=1,
                    unit_price=Decimal("299.00"),
                    line_subtotal=Decimal("299.00"),
                )
