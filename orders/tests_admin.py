"""Admin order endpoints, dashboard, rider management and the reaper command."""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, Rider, User
from delivery.models import Delivery
from menu.models import Menu

from .constants import DeliveryStatus, OrderStatus, PaymentType
from .models import Order, OrderStatusLog


def make_order(customer, **overrides):
    fields = {
        "order_number": f"YR-T{Order.objects.count() + 1:04d}",
        "customer": customer,
        "status": OrderStatus.PENDING,
        "payment_type": PaymentType.COD,
        "subtotal": Decimal("299.00"),
        "delivery_fee": Decimal("30.00"),
        "total": Decimal("329.00"),
        "delivery_address": "Blue gate, Quezon Blvd",
        "delivery_latitude": Decimal("7.008300"),
        "delivery_longitude": Decimal("125.089400"),
    }
    fields.update(overrides)
    return Order.objects.create(**fields)


class AdminTestBase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="boss", email="boss@example.com", password="Kidapawan!2026"
        )
        self.cust_user = User.objects.create_user(
            username="maria", email="maria@example.com", password="Kidapawan!2026"
        )
        self.customer = Customer.objects.create(user=self.cust_user)

        self.rider_user = User.objects.create_user(
            username="rico",
            email="rico@example.com",
            password="Kidapawan!2026",
            is_rider=True,
            is_customer=False,
        )
        self.rider = Rider.objects.create(
            user=self.rider_user, vehicle_type="Motorcycle", is_available=True
        )

        self.menu = Menu.objects.create(name="Pepperoni", price=Decimal("299.00"))
        self.order = make_order(self.customer)

        self.list_url = reverse("admin-order-list")
        self.dashboard_url = reverse("admin-dashboard")
        self.riders_url = reverse("admin-rider-list")
        self.available_url = reverse("admin-rider-available")

    def status_url(self, order):
        return reverse("admin-order-status", args=[order.pk])

    def assign_url(self, order):
        return reverse("admin-order-assign", args=[order.pk])


class AdminOrderListTests(AdminTestBase):
    def test_requires_admin(self):
        for user in (None, self.cust_user, self.rider_user):
            self.client.force_authenticate(user)
            expected = (
                status.HTTP_401_UNAUTHORIZED if user is None else status.HTTP_403_FORBIDDEN
            )
            self.assertEqual(self.client.get(self.list_url).status_code, expected)

    def test_admin_sees_every_customers_orders(self):
        other = Customer.objects.create(
            user=User.objects.create_user(
                username="pedro", email="pedro@example.com", password="Kidapawan!2026"
            )
        )
        make_order(other)

        self.client.force_authenticate(self.admin)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 2)

    def test_filter_by_single_status(self):
        make_order(self.customer, status=OrderStatus.CONFIRMED)

        self.client.force_authenticate(self.admin)
        response = self.client.get(self.list_url, {"status": "confirmed"})

        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["status"], "confirmed")

    def test_filter_by_comma_separated_statuses(self):
        make_order(self.customer, status=OrderStatus.CONFIRMED)
        make_order(self.customer, status=OrderStatus.DELIVERED)

        self.client.force_authenticate(self.admin)
        response = self.client.get(self.list_url, {"status": "pending,confirmed"})

        self.assertEqual(response.data["count"], 2)

    def test_unknown_status_filter_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.list_url, {"status": "nonsense"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("nonsense", response.data["error"])

    def test_response_is_paginated(self):
        self.client.force_authenticate(self.admin)
        data = self.client.get(self.list_url).data

        self.assertEqual(set(data), {"count", "next", "previous", "results"})
        self.assertEqual(data["count"], 1)
        self.assertIsNone(data["next"])

    def test_second_page_is_reachable(self):
        for _ in range(30):
            make_order(self.customer)

        self.client.force_authenticate(self.admin)
        first = self.client.get(self.list_url).data

        self.assertEqual(first["count"], 31)
        self.assertEqual(len(first["results"]), 25)  # default page_size
        self.assertIsNotNone(first["next"])

        second = self.client.get(self.list_url, {"page": 2}).data
        self.assertEqual(len(second["results"]), 6)
        self.assertIsNotNone(second["previous"])

    def test_page_size_can_be_overridden(self):
        for _ in range(10):
            make_order(self.customer)

        self.client.force_authenticate(self.admin)
        data = self.client.get(self.list_url, {"page_size": 5}).data
        self.assertEqual(len(data["results"]), 5)

    def test_page_size_is_capped(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.list_url, {"page_size": 5000})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_filter_and_pagination_compose(self):
        for _ in range(30):
            make_order(self.customer, status=OrderStatus.CONFIRMED)

        self.client.force_authenticate(self.admin)
        data = self.client.get(self.list_url, {"status": "confirmed"}).data
        self.assertEqual(data["count"], 30)
        self.assertEqual(len(data["results"]), 25)

    def test_customer_order_list_stays_unpaginated(self):
        """Only the admin queue is paginated — other list shapes are unchanged."""
        self.client.force_authenticate(self.cust_user)
        data = self.client.get(reverse("orders:order-list")).data
        self.assertIsInstance(data, list)

    def test_detail_exposes_next_statuses(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(reverse("admin-order-detail", args=[self.order.pk]))

        self.assertEqual(response.data["next_statuses"], ["cancelled", "confirmed"])
        self.assertEqual(response.data["customer_name"], "maria")
        self.assertIsNone(response.data["rider"])


class AdminStatusUpdateTests(AdminTestBase):
    def test_legal_advance_is_accepted(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.status_url(self.order), {"status": "confirmed"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.CONFIRMED)

    def test_illegal_jump_is_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.status_url(self.order), {"status": "delivered"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING)

    def test_cannot_move_backwards(self):
        self.order.status = OrderStatus.PREPARING
        self.order.save()

        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.status_url(self.order), {"status": "confirmed"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_terminal_order_cannot_change(self):
        self.order.status = OrderStatus.DELIVERED
        self.order.save()

        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.status_url(self.order), {"status": "cancelled"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no longer change", response.data["error"])

    def test_setting_the_same_status_is_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.status_url(self.order), {"status": "pending"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_confirmed_at_is_stamped(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(self.status_url(self.order), {"status": "confirmed"}, format="json")

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.confirmed_at)

    def test_delivered_at_is_stamped(self):
        self.order.status = OrderStatus.OUT_FOR_DELIVERY
        self.order.save()

        self.client.force_authenticate(self.admin)
        self.client.patch(self.status_url(self.order), {"status": "delivered"}, format="json")

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.delivered_at)

    def test_status_change_writes_a_log_attributed_to_the_admin(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(self.status_url(self.order), {"status": "confirmed"}, format="json")

        log = OrderStatusLog.objects.filter(order=self.order).latest("changed_at")
        self.assertEqual(log.status, OrderStatus.CONFIRMED)
        self.assertEqual(log.changed_by, self.admin)

    def test_cancel_reason_is_recorded(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(
            self.status_url(self.order),
            {"status": "cancelled", "cancel_reason": "Out of dough"},
            format="json",
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.cancel_reason, "Out of dough")
        self.assertIsNotNone(self.order.cancelled_at)

    def test_non_admin_cannot_change_status(self):
        self.client.force_authenticate(self.cust_user)
        response = self.client.patch(
            self.status_url(self.order), {"status": "confirmed"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_full_happy_path(self):
        self.client.force_authenticate(self.admin)
        for nxt in ("confirmed", "preparing", "ready", "out_for_delivery", "delivered"):
            response = self.client.patch(
                self.status_url(self.order), {"status": nxt}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, nxt)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.DELIVERED)
        self.assertEqual(OrderStatusLog.objects.filter(order=self.order).count(), 5)


class AdminAssignTests(AdminTestBase):
    def setUp(self):
        super().setUp()
        self.order.status = OrderStatus.CONFIRMED
        self.order.save()

    def test_assign_creates_a_delivery(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        delivery = Delivery.objects.get(order=self.order)
        self.assertEqual(delivery.rider, self.rider)
        self.assertEqual(delivery.status, DeliveryStatus.ASSIGNED)

    @override_settings(RIDER_EARNINGS_PER_DELIVERY="42.50")
    def test_rider_earnings_are_snapshotted_from_settings(self):
        self.client.force_authenticate(self.admin)
        self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )

        delivery = Delivery.objects.get(order=self.order)
        self.assertEqual(delivery.rider_earnings, Decimal("42.50"))

    def test_earnings_do_not_shift_when_the_rate_changes(self):
        self.client.force_authenticate(self.admin)
        with override_settings(RIDER_EARNINGS_PER_DELIVERY="30.00"):
            self.client.post(
                self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
            )

        with override_settings(RIDER_EARNINGS_PER_DELIVERY="99.00"):
            delivery = Delivery.objects.get(order=self.order)
            self.assertEqual(delivery.rider_earnings, Decimal("30.00"))

    def test_response_includes_the_rider_block(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )

        self.assertEqual(response.data["rider"]["rider_id"], self.rider.pk)
        self.assertEqual(response.data["rider"]["username"], "rico")

    def test_cannot_assign_twice(self):
        self.client.force_authenticate(self.admin)
        self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )
        response = self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Delivery.objects.filter(order=self.order).count(), 1)

    def test_a_rider_can_be_given_a_batch(self):
        """Riders collect several orders at the store before setting off."""
        others = [
            make_order(self.customer, status=OrderStatus.CONFIRMED) for _ in range(3)
        ]

        self.client.force_authenticate(self.admin)
        first = self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        for order in others:
            response = self.client.post(
                self.assign_url(order), {"rider_id": self.rider.pk}, format="json"
            )
            self.assertEqual(
                response.status_code, status.HTTP_201_CREATED, order.order_number
            )

        self.assertEqual(Delivery.objects.filter(rider=self.rider).count(), 4)

    def test_batching_still_refuses_an_already_assigned_order(self):
        other_rider = Rider.objects.create(
            user=User.objects.create_user(
                username="bong",
                email="bong@example.com",
                password="Kidapawan!2026",
                is_rider=True,
                is_customer=False,
            )
        )

        self.client.force_authenticate(self.admin)
        self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )
        response = self.client.post(
            self.assign_url(self.order), {"rider_id": other_rider.pk}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Delivery.objects.filter(order=self.order).count(), 1)

    def test_batching_still_refuses_an_unpaid_order(self):
        gcash = make_order(
            self.customer,
            status=OrderStatus.PENDING_PAYMENT,
            payment_type=PaymentType.GCASH,
        )

        self.client.force_authenticate(self.admin)
        self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )
        response = self.client.post(
            self.assign_url(gcash), {"rider_id": self.rider.pk}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Delivery.objects.filter(order=gcash).exists())

    def test_batching_still_refuses_a_deactivated_rider(self):
        self.client.force_authenticate(self.admin)
        self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )

        self.rider_user.is_active = False
        self.rider_user.save()

        second = make_order(self.customer, status=OrderStatus.CONFIRMED)
        response = self.client.post(
            self.assign_url(second), {"rider_id": self.rider.pk}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rider_is_bookable_again_once_delivered(self):
        second = make_order(self.customer, status=OrderStatus.CONFIRMED)

        self.client.force_authenticate(self.admin)
        self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )
        Delivery.objects.filter(order=self.order).update(
            status=DeliveryStatus.DELIVERED
        )

        response = self.client.post(
            self.assign_url(second), {"rider_id": self.rider.pk}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_unpaid_gcash_order_cannot_be_assigned(self):
        gcash = make_order(
            self.customer,
            status=OrderStatus.PENDING_PAYMENT,
            payment_type=PaymentType.GCASH,
        )

        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.assign_url(gcash), {"rider_id": self.rider.pk}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Delivery.objects.filter(order=gcash).exists())

    def test_cancelled_order_cannot_be_assigned(self):
        self.order.status = OrderStatus.CANCELLED
        self.order.save()

        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_rider_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.assign_url(self.order), {"rider_id": 999999}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deactivated_rider_rejected(self):
        self.rider_user.is_active = False
        self.rider_user.save()

        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_admin_cannot_assign(self):
        self.client.force_authenticate(self.cust_user)
        response = self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AdminDashboardTests(AdminTestBase):
    def test_requires_admin(self):
        self.client.force_authenticate(self.cust_user)
        self.assertEqual(
            self.client.get(self.dashboard_url).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_counts_and_revenue(self):
        now = timezone.now()
        make_order(
            self.customer,
            status=OrderStatus.DELIVERED,
            delivered_at=now,
            subtotal=Decimal("500.00"),
            delivery_fee=Decimal("30.00"),
            total=Decimal("530.00"),
        )
        make_order(
            self.customer,
            status=OrderStatus.DELIVERED,
            delivered_at=now,
            subtotal=Decimal("200.00"),
            delivery_fee=Decimal("30.00"),
            total=Decimal("230.00"),
        )

        self.client.force_authenticate(self.admin)
        data = self.client.get(self.dashboard_url).data

        self.assertEqual(data["today"]["orders_delivered"], 2)
        self.assertEqual(data["today"]["revenue"], "760.00")
        self.assertEqual(data["today"]["food_sales"], "700.00")
        self.assertEqual(data["today"]["delivery_fees"], "60.00")

    def test_revenue_excludes_undelivered_orders(self):
        make_order(self.customer, status=OrderStatus.PREPARING, total=Decimal("999.00"))

        self.client.force_authenticate(self.admin)
        data = self.client.get(self.dashboard_url).data
        self.assertEqual(data["today"]["revenue"], "0.00")

    def test_revenue_excludes_yesterdays_deliveries(self):
        make_order(
            self.customer,
            status=OrderStatus.DELIVERED,
            delivered_at=timezone.now() - timedelta(days=1),
            total=Decimal("500.00"),
        )

        self.client.force_authenticate(self.admin)
        data = self.client.get(self.dashboard_url).data
        self.assertEqual(data["today"]["revenue"], "0.00")
        self.assertEqual(data["today"]["orders_delivered"], 0)

    def test_status_counts_cover_every_status(self):
        self.client.force_authenticate(self.admin)
        data = self.client.get(self.dashboard_url).data

        self.assertEqual(len(data["status_counts"]), 8)
        self.assertEqual(data["status_counts"]["pending"], 1)
        self.assertEqual(data["status_counts"]["delivered"], 0)

    def test_rider_counts(self):
        self.client.force_authenticate(self.admin)
        data = self.client.get(self.dashboard_url).data

        self.assertEqual(data["riders"]["total"], 1)
        self.assertEqual(data["riders"]["on_duty"], 1)
        self.assertEqual(data["riders"]["on_delivery"], 0)

        Delivery.objects.create(order=self.order, rider=self.rider)
        data = self.client.get(self.dashboard_url).data
        self.assertEqual(data["riders"]["on_delivery"], 1)

    def test_active_orders_excludes_terminal(self):
        make_order(self.customer, status=OrderStatus.DELIVERED)
        make_order(self.customer, status=OrderStatus.CANCELLED)

        self.client.force_authenticate(self.admin)
        data = self.client.get(self.dashboard_url).data
        self.assertEqual(data["active_orders"], 1)


class AdminRiderTests(AdminTestBase):
    payload = {
        "username": "bong",
        "email": "bong@example.com",
        "password": "Kidapawan!2026",
        "first_name": "Bong",
        "last_name": "Reyes",
        "phone": "09171234567",
        "vehicle_type": "Motorcycle",
        "plate_number": "ABC 1234",
    }

    def test_requires_admin(self):
        self.client.force_authenticate(self.cust_user)
        self.assertEqual(
            self.client.get(self.riders_url).status_code, status.HTTP_403_FORBIDDEN
        )
        self.assertEqual(
            self.client.post(self.riders_url, self.payload, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_admin_can_create_a_rider(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(self.riders_url, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username="bong")
        self.assertTrue(user.is_rider)
        self.assertFalse(user.is_customer)
        self.assertFalse(user.is_admin)
        self.assertTrue(user.check_password("Kidapawan!2026"))
        self.assertEqual(user.rider.plate_number, "ABC 1234")

    def test_new_rider_starts_off_duty(self):
        self.client.force_authenticate(self.admin)
        self.client.post(self.riders_url, self.payload, format="json")
        self.assertFalse(User.objects.get(username="bong").rider.is_available)

    def test_role_flags_cannot_be_escalated(self):
        self.client.force_authenticate(self.admin)
        self.client.post(
            self.riders_url, {**self.payload, "is_admin": True}, format="json"
        )
        self.assertFalse(User.objects.get(username="bong").is_admin)

    def test_password_is_never_echoed(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(self.riders_url, self.payload, format="json")
        self.assertNotIn("password", response.data)

    def test_weak_password_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.riders_url, {**self.payload, "password": "12345678"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(username="bong").exists())

    def test_duplicate_username_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.riders_url, {**self.payload, "username": "rico"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_riders(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.riders_url)

        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["username"], "rico")
        self.assertEqual(response.data[0]["vehicle_type"], "Motorcycle")

    def test_available_lists_on_duty_riders(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.available_url)
        self.assertEqual(len(response.data), 1)

    def test_available_excludes_off_duty(self):
        self.rider.is_available = False
        self.rider.save()

        self.client.force_authenticate(self.admin)
        self.assertEqual(len(self.client.get(self.available_url).data), 0)

    def test_available_excludes_riders_already_driving(self):
        Delivery.objects.create(
            order=self.order, rider=self.rider, status=DeliveryStatus.IN_TRANSIT
        )

        self.client.force_authenticate(self.admin)
        self.assertEqual(len(self.client.get(self.available_url).data), 0)

    def test_available_still_includes_a_rider_loading_a_batch(self):
        """Assigned and picked_up riders are still at the store."""
        for delivery_status in (DeliveryStatus.ASSIGNED, DeliveryStatus.PICKED_UP):
            Delivery.objects.all().delete()
            Delivery.objects.create(
                order=self.order, rider=self.rider, status=delivery_status
            )

            self.client.force_authenticate(self.admin)
            data = self.client.get(self.available_url).data
            self.assertEqual(
                [r["username"] for r in data], ["rico"], delivery_status
            )

    def test_available_rider_carrying_a_batch_reports_the_count(self):
        for _ in range(3):
            order = make_order(self.customer, status=OrderStatus.CONFIRMED)
            Delivery.objects.create(
                order=order, rider=self.rider, status=DeliveryStatus.PICKED_UP
            )

        self.client.force_authenticate(self.admin)
        rider = self.client.get(self.available_url).data[0]

        self.assertEqual(rider["deliveries_held"], 3)
        self.assertIsNone(rider["current_delivery"])

    def test_available_includes_riders_whose_delivery_finished(self):
        Delivery.objects.create(
            order=self.order, rider=self.rider, status=DeliveryStatus.DELIVERED
        )

        self.client.force_authenticate(self.admin)
        self.assertEqual(len(self.client.get(self.available_url).data), 1)

    def test_available_excludes_deactivated_accounts(self):
        self.rider_user.is_active = False
        self.rider_user.save()

        self.client.force_authenticate(self.admin)
        self.assertEqual(len(self.client.get(self.available_url).data), 0)

    def test_list_reports_the_batch_size_and_current_leg(self):
        """The admin needs '3 orders, currently on YR-xxxx' to judge a fourth."""
        driving = make_order(self.customer, status=OrderStatus.CONFIRMED)
        Delivery.objects.create(
            order=driving, rider=self.rider, status=DeliveryStatus.IN_TRANSIT
        )
        Delivery.objects.create(
            order=self.order, rider=self.rider, status=DeliveryStatus.PICKED_UP
        )
        Delivery.objects.create(
            order=make_order(self.customer, status=OrderStatus.CONFIRMED),
            rider=self.rider,
            status=DeliveryStatus.ASSIGNED,
        )

        self.client.force_authenticate(self.admin)
        rider = self.client.get(self.riders_url).data[0]

        self.assertEqual(rider["deliveries_held"], 3)
        self.assertEqual(
            rider["current_delivery"]["order_number"], driving.order_number
        )
        self.assertEqual(rider["current_delivery"]["status"], "in_transit")

    def test_current_delivery_is_null_while_loading_up(self):
        Delivery.objects.create(
            order=self.order, rider=self.rider, status=DeliveryStatus.PICKED_UP
        )

        self.client.force_authenticate(self.admin)
        rider = self.client.get(self.riders_url).data[0]

        self.assertEqual(rider["deliveries_held"], 1)
        self.assertIsNone(rider["current_delivery"])

    def test_completed_deliveries_are_not_counted_as_held(self):
        Delivery.objects.create(
            order=self.order, rider=self.rider, status=DeliveryStatus.DELIVERED
        )

        self.client.force_authenticate(self.admin)
        rider = self.client.get(self.riders_url).data[0]

        self.assertEqual(rider["deliveries_held"], 0)
        self.assertIsNone(rider["current_delivery"])

    def test_a_fresh_rider_holds_nothing(self):
        self.client.force_authenticate(self.admin)
        rider = self.client.get(self.riders_url).data[0]

        self.assertEqual(rider["deliveries_held"], 0)
        self.assertIsNone(rider["current_delivery"])


class RiderAccountManagementTests(AdminTestBase):
    """Editing, password recovery and deactivation."""

    def detail_url(self, rider=None):
        return reverse("admin-rider-detail", args=[(rider or self.rider).pk])

    def password_url(self, rider=None):
        return reverse("admin-rider-set-password", args=[(rider or self.rider).pk])

    def deactivate_url(self, rider=None):
        return reverse("admin-rider-deactivate", args=[(rider or self.rider).pk])

    # -- PATCH /api/admin/riders/{id}/ ---------------------------------

    def test_admin_can_update_profile_and_user_fields(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.detail_url(),
            {
                "first_name": "Rico",
                "last_name": "Delos Santos",
                "phone": "09998887777",
                "vehicle_type": "Scooter",
                "plate_number": "XYZ 9999",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.rider.refresh_from_db()
        self.rider_user.refresh_from_db()

        self.assertEqual(self.rider_user.first_name, "Rico")
        self.assertEqual(self.rider_user.phone, "09998887777")
        self.assertEqual(self.rider.vehicle_type, "Scooter")
        self.assertEqual(self.rider.plate_number, "XYZ 9999")
        self.assertEqual(response.data["name"], "Rico Delos Santos")

    def test_a_partial_update_leaves_other_fields_alone(self):
        self.rider.vehicle_type = "Motorcycle"
        self.rider.save()

        self.client.force_authenticate(self.admin)
        self.client.patch(
            self.detail_url(), {"plate_number": "AAA 1111"}, format="json"
        )

        self.rider.refresh_from_db()
        self.assertEqual(self.rider.vehicle_type, "Motorcycle")
        self.assertEqual(self.rider.plate_number, "AAA 1111")

    def test_email_can_be_changed(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.detail_url(), {"email": "rico.new@example.com"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.rider_user.refresh_from_db()
        self.assertEqual(self.rider_user.email, "rico.new@example.com")

    def test_duplicate_email_is_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.detail_url(), {"email": "boss@example.com"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.rider_user.refresh_from_db()
        self.assertEqual(self.rider_user.email, "rico@example.com")

    def test_keeping_your_own_email_is_not_a_clash(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.detail_url(),
            {"email": "rico@example.com", "vehicle_type": "Scooter"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_username_and_role_flags_cannot_be_changed(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(
            self.detail_url(),
            {"username": "hacked", "is_admin": True, "is_available": True},
            format="json",
        )

        self.rider.refresh_from_db()
        self.rider_user.refresh_from_db()
        self.assertEqual(self.rider_user.username, "rico")
        self.assertFalse(self.rider_user.is_admin)
        self.assertTrue(self.rider_user.is_rider)

    def test_put_is_not_allowed(self):
        self.client.force_authenticate(self.admin)
        response = self.client.put(
            self.detail_url(), {"vehicle_type": "Scooter"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_non_admins_cannot_update(self):
        for user in (self.cust_user, self.rider_user):
            self.client.force_authenticate(user)
            response = self.client.patch(
                self.detail_url(), {"vehicle_type": "Scooter"}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # -- POST /api/admin/riders/{id}/set-password/ ---------------------

    def test_admin_can_set_a_new_password(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.password_url(), {"password": "BrandNew!2026"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.rider_user.refresh_from_db()
        self.assertTrue(self.rider_user.check_password("BrandNew!2026"))

    def test_the_rider_can_log_in_with_the_new_password(self):
        """This is the whole recovery path — it has to actually work."""
        self.client.force_authenticate(self.admin)
        self.client.post(
            self.password_url(), {"password": "BrandNew!2026"}, format="json"
        )

        self.client.force_authenticate(None)
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "rico", "password": "BrandNew!2026"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_old_password_stops_working(self):
        self.client.force_authenticate(self.admin)
        self.client.post(
            self.password_url(), {"password": "BrandNew!2026"}, format="json"
        )

        self.client.force_authenticate(None)
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "rico", "password": "Kidapawan!2026"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_weak_password_returns_a_field_level_error(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.password_url(), {"password": "12345678"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.data["fields"])
        self.rider_user.refresh_from_db()
        self.assertTrue(self.rider_user.check_password("Kidapawan!2026"))

    def test_short_password_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.password_url(), {"password": "Ab1!"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_password_similar_to_the_username_is_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.password_url(), {"password": "rico"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_password_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(self.password_url(), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_password_is_never_echoed(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.password_url(), {"password": "BrandNew!2026"}, format="json"
        )
        self.assertNotIn("BrandNew!2026", str(response.data))

    def test_non_admins_cannot_set_a_password(self):
        for user in (self.cust_user, self.rider_user):
            self.client.force_authenticate(user)
            response = self.client.post(
                self.password_url(), {"password": "BrandNew!2026"}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # -- PATCH /api/admin/riders/{id}/deactivate/ ----------------------

    def test_deactivate_switches_the_account_off(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(self.deactivate_url(), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.rider_user.refresh_from_db()
        self.assertFalse(self.rider_user.is_active)

    def test_a_deactivated_rider_cannot_log_in(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(self.deactivate_url(), {}, format="json")

        self.client.force_authenticate(None)
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "rico", "password": "Kidapawan!2026"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_deactivation_takes_the_rider_off_duty(self):
        self.rider.is_available = True
        self.rider.save()

        self.client.force_authenticate(self.admin)
        self.client.patch(self.deactivate_url(), {}, format="json")

        self.rider.refresh_from_db()
        self.assertFalse(self.rider.is_available)

    def test_reactivation(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(self.deactivate_url(), {}, format="json")
        response = self.client.patch(
            self.deactivate_url(), {"is_active": True}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.rider_user.refresh_from_db()
        self.assertTrue(self.rider_user.is_active)

    def test_deactivation_is_refused_while_carrying_orders(self):
        Delivery.objects.create(
            order=self.order, rider=self.rider, status=DeliveryStatus.PICKED_UP
        )

        self.client.force_authenticate(self.admin)
        response = self.client.patch(self.deactivate_url(), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(self.order.order_number, response.data["error"])
        self.rider_user.refresh_from_db()
        self.assertTrue(self.rider_user.is_active)

    def test_the_refusal_names_every_held_order(self):
        second = make_order(self.customer, status=OrderStatus.CONFIRMED)
        Delivery.objects.create(
            order=self.order, rider=self.rider, status=DeliveryStatus.ASSIGNED
        )
        Delivery.objects.create(
            order=second, rider=self.rider, status=DeliveryStatus.IN_TRANSIT
        )

        self.client.force_authenticate(self.admin)
        response = self.client.patch(self.deactivate_url(), {}, format="json")

        self.assertIn(self.order.order_number, response.data["error"])
        self.assertIn(second.order_number, response.data["error"])

    def test_completed_deliveries_do_not_block_deactivation(self):
        Delivery.objects.create(
            order=self.order, rider=self.rider, status=DeliveryStatus.DELIVERED
        )

        self.client.force_authenticate(self.admin)
        response = self.client.patch(self.deactivate_url(), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.rider_user.refresh_from_db()
        self.assertFalse(self.rider_user.is_active)

    def test_history_survives_deactivation(self):
        """Deactivate rather than delete — the records must stay."""
        delivery = Delivery.objects.create(
            order=self.order, rider=self.rider, status=DeliveryStatus.DELIVERED
        )

        self.client.force_authenticate(self.admin)
        self.client.patch(self.deactivate_url(), {}, format="json")

        delivery.refresh_from_db()
        self.assertEqual(delivery.rider_id, self.rider.pk)
        self.assertTrue(Rider.objects.filter(pk=self.rider.pk).exists())

    def test_a_deactivated_rider_cannot_be_assigned(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(self.deactivate_url(), {}, format="json")

        response = self.client.post(
            self.assign_url(self.order), {"rider_id": self.rider.pk}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_admins_cannot_deactivate(self):
        for user in (self.cust_user, self.rider_user):
            self.client.force_authenticate(user)
            response = self.client.patch(self.deactivate_url(), {}, format="json")
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class CustomerCancelFromPendingPaymentTests(AdminTestBase):
    """The customer may now abandon an unpaid GCash checkout."""

    def test_pending_payment_order_can_be_cancelled(self):
        order = make_order(
            self.customer,
            status=OrderStatus.PENDING_PAYMENT,
            payment_type=PaymentType.GCASH,
        )

        self.client.force_authenticate(self.cust_user)
        response = self.client.post(
            reverse("orders:order-cancel", args=[order.pk]), {}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)

    def test_can_cancel_flag_is_true_for_pending_payment(self):
        order = make_order(self.customer, status=OrderStatus.PENDING_PAYMENT)

        self.client.force_authenticate(self.cust_user)
        data = self.client.get(reverse("orders:order-detail", args=[order.pk])).data
        self.assertTrue(data["can_cancel"])

    def test_confirmed_order_still_cannot_be_cancelled(self):
        order = make_order(self.customer, status=OrderStatus.CONFIRMED)

        self.client.force_authenticate(self.cust_user)
        response = self.client.post(
            reverse("orders:order-cancel", args=[order.pk]), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CancelStaleOrdersCommandTests(AdminTestBase):
    def stale_order(self, minutes_ago):
        order = make_order(
            self.customer,
            status=OrderStatus.PENDING_PAYMENT,
            payment_type=PaymentType.GCASH,
        )
        # placed_at is auto_now_add, so rewrite it directly.
        Order.objects.filter(pk=order.pk).update(
            placed_at=timezone.now() - timedelta(minutes=minutes_ago)
        )
        order.refresh_from_db()
        return order

    def run_command(self, *args):
        out = StringIO()
        call_command("cancel_stale_orders", *args, stdout=out)
        return out.getvalue()

    def test_cancels_orders_past_the_timeout(self):
        order = self.stale_order(45)
        output = self.run_command("--minutes", "30")

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertIn("Payment not completed", order.cancel_reason)
        self.assertIn(order.order_number, output)

    def test_leaves_fresh_orders_alone(self):
        order = self.stale_order(5)
        self.run_command("--minutes", "30")

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)

    def test_ignores_orders_in_other_statuses(self):
        order = make_order(self.customer, status=OrderStatus.PENDING)
        Order.objects.filter(pk=order.pk).update(
            placed_at=timezone.now() - timedelta(hours=5)
        )

        self.run_command("--minutes", "30")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING)

    def test_dry_run_writes_nothing(self):
        order = self.stale_order(45)
        output = self.run_command("--minutes", "30", "--dry-run")

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertIn("Dry run", output)
        self.assertIn(order.order_number, output)

    def test_writes_a_status_log_with_no_user(self):
        order = self.stale_order(45)
        self.run_command("--minutes", "30")

        log = OrderStatusLog.objects.filter(order=order).latest("changed_at")
        self.assertEqual(log.status, OrderStatus.CANCELLED)
        self.assertIsNone(log.changed_by)

    def test_stamps_cancelled_at(self):
        order = self.stale_order(45)
        self.run_command("--minutes", "30")

        order.refresh_from_db()
        self.assertIsNotNone(order.cancelled_at)

    @override_settings(PENDING_PAYMENT_TIMEOUT_MINUTES=10)
    def test_falls_back_to_the_setting(self):
        order = self.stale_order(20)
        self.run_command()

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)

    def test_reports_when_there_is_nothing_to_do(self):
        output = self.run_command("--minutes", "30")
        self.assertIn("No orders", output)
