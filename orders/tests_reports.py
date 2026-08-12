"""Date-range reporting tests."""

from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, Rider, User
from delivery.models import Delivery
from menu.models import Menu

from .constants import DeliveryStatus, OrderStatus, PaymentType
from .models import Order, OrderItem


class ReportTestBase(APITestCase):
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
            first_name="Rico",
            last_name="Cruz",
            is_rider=True,
            is_customer=False,
        )
        self.rider = Rider.objects.create(user=self.rider_user)

        self.pizza = Menu.objects.create(name="Pepperoni", price=Decimal("299.00"))
        self.pasta = Menu.objects.create(name="Carbonara", price=Decimal("150.00"))

        self.today = timezone.localdate()
        self.sales_url = reverse("admin-report-sales")
        self.riders_url = reverse("admin-report-riders")
        self.menu_url = reverse("admin-report-menu")

    def make_order(self, **overrides):
        fields = {
            "order_number": f"YR-R{Order.objects.count() + 1:04d}",
            "customer": self.customer,
            "status": OrderStatus.PENDING,
            "payment_type": PaymentType.COD,
            "subtotal": Decimal("598.00"),
            "delivery_fee": Decimal("30.00"),
            "total": Decimal("628.00"),
            "delivery_address": "Blue gate",
            "delivery_latitude": Decimal("7.008300"),
            "delivery_longitude": Decimal("125.089400"),
        }
        fields.update(overrides)
        return Order.objects.create(**fields)

    def delivered_order(self, days_ago=0, subtotal="598.00", fee="30.00", items=None):
        """A delivered order dated `days_ago` days back, in local time."""
        subtotal = Decimal(subtotal)
        fee = Decimal(fee)
        order = self.make_order(
            status=OrderStatus.DELIVERED,
            subtotal=subtotal,
            delivery_fee=fee,
            total=subtotal + fee,
        )
        when = timezone.now() - timedelta(days=days_ago)
        Order.objects.filter(pk=order.pk).update(delivered_at=when, placed_at=when)
        order.refresh_from_db()

        for menu, quantity in items or []:
            OrderItem.objects.create(
                order=order,
                menu=menu,
                quantity=quantity,
                unit_price=menu.price,
                line_subtotal=menu.price * quantity,
            )
        return order

    def get(self, url, **params):
        self.client.force_authenticate(self.admin)
        return self.client.get(url, params)


class ReportPermissionTests(ReportTestBase):
    def test_all_three_require_admin(self):
        for url in (self.sales_url, self.riders_url, self.menu_url):
            self.client.force_authenticate(None)
            self.assertEqual(
                self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED, url
            )

            self.client.force_authenticate(self.cust_user)
            self.assertEqual(
                self.client.get(url).status_code, status.HTTP_403_FORBIDDEN, url
            )

            self.client.force_authenticate(self.rider_user)
            self.assertEqual(
                self.client.get(url).status_code, status.HTTP_403_FORBIDDEN, url
            )


class DateRangeTests(ReportTestBase):
    def test_defaults_to_the_last_thirty_days(self):
        data = self.get(self.sales_url).data

        self.assertEqual(data["end"], self.today.isoformat())
        self.assertEqual(
            data["start"], (self.today - timedelta(days=29)).isoformat()
        )
        self.assertEqual(data["days"], 30)

    def test_explicit_range_is_honoured(self):
        start = (self.today - timedelta(days=6)).isoformat()
        data = self.get(self.sales_url, start=start, end=self.today.isoformat()).data

        self.assertEqual(data["start"], start)
        self.assertEqual(data["days"], 7)

    def test_a_single_day_is_allowed(self):
        day = self.today.isoformat()
        data = self.get(self.sales_url, start=day, end=day).data
        self.assertEqual(data["days"], 1)

    def test_start_after_end_is_rejected(self):
        response = self.get(
            self.sales_url,
            start=self.today.isoformat(),
            end=(self.today - timedelta(days=5)).isoformat(),
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("is after end", response.data["error"])

    def test_malformed_dates_are_rejected(self):
        for params in (
            {"start": "not-a-date"},
            {"end": "2026-13-45"},
            {"start": "12/08/2026"},
        ):
            response = self.get(self.sales_url, **params)
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST, params
            )
            self.assertIn("YYYY-MM-DD", response.data["error"])

    def test_an_absurd_range_is_refused(self):
        response = self.get(
            self.sales_url, start="2000-01-01", end=self.today.isoformat()
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_range_applies_to_every_report(self):
        for url in (self.riders_url, self.menu_url):
            data = self.get(url, start="2026-01-01", end="2026-01-31").data
            self.assertEqual(data["start"], "2026-01-01")
            self.assertEqual(data["end"], "2026-01-31")


class SalesReportTests(ReportTestBase):
    def test_revenue_splits_into_food_and_fees(self):
        self.delivered_order(subtotal="500.00", fee="30.00")
        self.delivered_order(subtotal="200.00", fee="30.00")

        totals = self.get(self.sales_url).data["totals"]

        self.assertEqual(totals["revenue"], "760.00")
        self.assertEqual(totals["food_sales"], "700.00")
        self.assertEqual(totals["delivery_fees"], "60.00")
        self.assertEqual(totals["orders_delivered"], 2)

    def test_undelivered_orders_do_not_inflate_revenue(self):
        """Revenue recognises on delivery, like the dashboard."""
        self.make_order(status=OrderStatus.PREPARING, total=Decimal("999.00"))
        self.make_order(status=OrderStatus.PENDING, total=Decimal("999.00"))
        self.make_order(
            status=OrderStatus.PENDING_PAYMENT, total=Decimal("999.00")
        )

        totals = self.get(self.sales_url).data["totals"]
        self.assertEqual(totals["revenue"], "0.00")
        self.assertEqual(totals["orders_delivered"], 0)

    def test_deliveries_outside_the_range_are_excluded(self):
        self.delivered_order(days_ago=2, subtotal="100.00")
        self.delivered_order(days_ago=40, subtotal="900.00")

        totals = self.get(self.sales_url).data["totals"]
        self.assertEqual(totals["food_sales"], "100.00")

    def test_cancelled_orders_are_counted_not_charged(self):
        order = self.make_order(status=OrderStatus.CANCELLED)
        Order.objects.filter(pk=order.pk).update(cancelled_at=timezone.now())

        totals = self.get(self.sales_url).data["totals"]
        self.assertEqual(totals["orders_cancelled"], 1)
        self.assertEqual(totals["revenue"], "0.00")

    def test_daily_series_covers_every_day_including_empty_ones(self):
        self.delivered_order(days_ago=1, subtotal="100.00")

        data = self.get(
            self.sales_url,
            start=(self.today - timedelta(days=4)).isoformat(),
            end=self.today.isoformat(),
        ).data

        self.assertEqual(len(data["daily"]), 5)
        dates = [row["date"] for row in data["daily"]]
        self.assertEqual(dates, sorted(dates))

        yesterday = (self.today - timedelta(days=1)).isoformat()
        row = next(r for r in data["daily"] if r["date"] == yesterday)
        self.assertEqual(row["food_sales"], "100.00")

        empty = next(r for r in data["daily"] if r["date"] != yesterday)
        self.assertEqual(empty["revenue"], "0.00")
        self.assertEqual(empty["orders_delivered"], 0)

    def test_totals_equal_the_sum_of_the_daily_rows(self):
        """A chart and its headline figure must not disagree."""
        for days_ago in (0, 1, 2, 2):
            self.delivered_order(days_ago=days_ago, subtotal="100.00", fee="30.00")

        data = self.get(self.sales_url).data
        daily_revenue = sum(Decimal(r["revenue"]) for r in data["daily"])

        self.assertEqual(Decimal(data["totals"]["revenue"]), daily_revenue)
        self.assertEqual(
            data["totals"]["orders_delivered"],
            sum(r["orders_delivered"] for r in data["daily"]),
        )

    def test_average_order_value(self):
        self.delivered_order(subtotal="270.00", fee="30.00")
        self.delivered_order(subtotal="570.00", fee="30.00")

        totals = self.get(self.sales_url).data["totals"]
        self.assertEqual(totals["average_order_value"], "450.00")

    def test_average_is_zero_with_no_deliveries(self):
        totals = self.get(self.sales_url).data["totals"]
        self.assertEqual(totals["average_order_value"], "0.00")

    def test_empty_range_returns_zeroes_not_an_error(self):
        data = self.get(self.sales_url, start="2020-01-01", end="2020-01-07").data

        self.assertEqual(data["totals"]["revenue"], "0.00")
        self.assertEqual(len(data["daily"]), 7)


class RidersReportTests(ReportTestBase):
    def complete(self, days_ago=0, earnings="30.00"):
        order = self.delivered_order(days_ago=days_ago)
        delivery = Delivery.objects.create(
            order=order,
            rider=self.rider,
            status=DeliveryStatus.DELIVERED,
            rider_earnings=Decimal(earnings),
        )
        Delivery.objects.filter(pk=delivery.pk).update(
            delivered_at=timezone.now() - timedelta(days=days_ago)
        )
        return delivery

    def test_counts_and_earnings(self):
        self.complete()
        self.complete(earnings="45.00")

        order = self.make_order()
        Delivery.objects.create(
            order=order, rider=self.rider, status=DeliveryStatus.FAILED
        )

        rows = self.get(self.riders_url).data["riders"]
        row = next(r for r in rows if r["username"] == "rico")

        self.assertEqual(row["deliveries_completed"], 2)
        self.assertEqual(row["deliveries_failed"], 1)
        self.assertEqual(row["earnings"], "75.00")
        self.assertEqual(row["name"], "Rico Cruz")

    def test_idle_riders_still_appear(self):
        rows = self.get(self.riders_url).data["riders"]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["deliveries_completed"], 0)
        self.assertEqual(rows[0]["earnings"], "0.00")

    def test_ordered_by_deliveries_completed(self):
        busier = Rider.objects.create(
            user=User.objects.create_user(
                username="bong",
                email="bong@example.com",
                password="Kidapawan!2026",
                is_rider=True,
                is_customer=False,
            )
        )
        self.complete()
        for _ in range(3):
            order = self.delivered_order()
            delivery = Delivery.objects.create(
                order=order,
                rider=busier,
                status=DeliveryStatus.DELIVERED,
                rider_earnings=Decimal("30.00"),
            )
            Delivery.objects.filter(pk=delivery.pk).update(
                delivered_at=timezone.now()
            )

        rows = self.get(self.riders_url).data["riders"]
        self.assertEqual([r["username"] for r in rows], ["bong", "rico"])
        self.assertEqual(rows[0]["deliveries_completed"], 3)

    def test_work_outside_the_range_is_excluded(self):
        self.complete(days_ago=40, earnings="500.00")

        row = self.get(self.riders_url).data["riders"][0]
        self.assertEqual(row["deliveries_completed"], 0)
        self.assertEqual(row["earnings"], "0.00")

    def test_incomplete_deliveries_do_not_earn(self):
        order = self.make_order()
        Delivery.objects.create(
            order=order,
            rider=self.rider,
            status=DeliveryStatus.IN_TRANSIT,
            rider_earnings=Decimal("30.00"),
        )

        row = self.get(self.riders_url).data["riders"][0]
        self.assertEqual(row["earnings"], "0.00")
        self.assertEqual(row["deliveries_completed"], 0)

    def test_totals_line(self):
        self.complete()
        self.complete()

        totals = self.get(self.riders_url).data["totals"]
        self.assertEqual(totals["deliveries_completed"], 2)
        self.assertEqual(totals["earnings"], "60.00")


class MenuReportTests(ReportTestBase):
    def test_quantity_and_revenue_per_item(self):
        self.delivered_order(items=[(self.pizza, 2), (self.pasta, 1)])
        self.delivered_order(items=[(self.pizza, 3)])

        rows = {r["name"]: r for r in self.get(self.menu_url).data["items"]}

        self.assertEqual(rows["Pepperoni"]["quantity_sold"], 5)
        self.assertEqual(rows["Pepperoni"]["revenue"], "1495.00")
        self.assertEqual(rows["Carbonara"]["quantity_sold"], 1)
        self.assertEqual(rows["Carbonara"]["revenue"], "150.00")

    def test_best_seller_first_worst_last(self):
        self.delivered_order(items=[(self.pizza, 5)])
        self.delivered_order(items=[(self.pasta, 1)])
        never_sold = Menu.objects.create(name="Aloha", price=Decimal("259.00"))

        rows = self.get(self.menu_url).data["items"]
        names = [r["name"] for r in rows]

        self.assertEqual(names[0], "Pepperoni")
        self.assertEqual(names[-1], never_sold.name)
        self.assertEqual(rows[-1]["quantity_sold"], 0)

    def test_unsold_items_are_listed_with_zeroes(self):
        rows = self.get(self.menu_url).data["items"]

        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["quantity_sold"], 0)
            self.assertEqual(row["revenue"], "0.00")

    def test_undelivered_orders_do_not_count(self):
        pending = self.make_order(status=OrderStatus.PREPARING)
        OrderItem.objects.create(
            order=pending,
            menu=self.pizza,
            quantity=9,
            unit_price=self.pizza.price,
            line_subtotal=self.pizza.price * 9,
        )

        rows = {r["name"]: r for r in self.get(self.menu_url).data["items"]}
        self.assertEqual(rows["Pepperoni"]["quantity_sold"], 0)

    def test_sales_outside_the_range_are_excluded(self):
        self.delivered_order(days_ago=40, items=[(self.pizza, 7)])

        rows = {r["name"]: r for r in self.get(self.menu_url).data["items"]}
        self.assertEqual(rows["Pepperoni"]["quantity_sold"], 0)

    def test_revenue_uses_the_snapshotted_price(self):
        """An admin editing the price must not rewrite past reports."""
        self.delivered_order(items=[(self.pizza, 2)])

        self.pizza.price = Decimal("999.00")
        self.pizza.save()

        rows = {r["name"]: r for r in self.get(self.menu_url).data["items"]}
        self.assertEqual(rows["Pepperoni"]["revenue"], "598.00")

    def test_totals_line(self):
        self.delivered_order(items=[(self.pizza, 2), (self.pasta, 1)])

        totals = self.get(self.menu_url).data["totals"]
        self.assertEqual(totals["quantity_sold"], 3)
        self.assertEqual(totals["revenue"], "748.00")
