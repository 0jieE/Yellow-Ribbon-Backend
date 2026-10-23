"""
The customised admin: root redirect, dashboard, workflow actions, CSV export.

The actions are the part worth testing hard. They write order status, so they
have to leave the same trail as the mobile apps do (status log, timestamps)
and must refuse the moves the API refuses.
"""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Customer, Rider, User
from delivery.models import Delivery
from menu.models import Menu
from orders.constants import DeliveryStatus, OrderStatus, PaymentType
from orders.models import Order, OrderItem, OrderStatusLog
from orders.tests_admin import make_order

PASSWORD = "Kidapawan!2026"


class AdminUITestBase(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(
            username="boss", email="boss@example.com", password=PASSWORD
        )
        self.client.force_login(self.boss)

        self.cust_user = User.objects.create_user(
            username="maria",
            email="maria@example.com",
            password=PASSWORD,
            first_name="Maria",
            last_name="Santos",
            phone="09171234567",
        )
        self.customer = Customer.objects.create(user=self.cust_user)

        self.rider_user = User.objects.create_user(
            username="rico",
            email="rico@example.com",
            password=PASSWORD,
            is_rider=True,
            is_customer=False,
        )
        self.rider = Rider.objects.create(
            user=self.rider_user, vehicle_type="Motorcycle", is_available=True
        )

        self.changelist = reverse("admin:orders_order_changelist")

    def run_action(self, action, orders, url=None, **extra):
        data = {
            "action": action,
            "_selected_action": [o.pk for o in orders],
            "select_across": "0",
            "index": "0",
            **extra,
        }
        return self.client.post(url or self.changelist, data, follow=True)

    def flash(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


class RootRedirectTests(TestCase):
    def test_site_root_goes_to_admin(self):
        response = self.client.get("/")
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)
        self.assertEqual(response.status_code, 302)

    def test_signed_out_visitor_ends_at_the_login_form(self):
        response = self.client.get("/", follow=True)
        self.assertEqual(response.redirect_chain[-1][0].split("?")[0], "/admin/login/")
        self.assertEqual(response.status_code, 200)

    def test_api_routes_are_untouched(self):
        self.assertEqual(self.client.get("/api/health/").status_code, 200)


class DashboardTests(AdminUITestBase):
    def test_renders_on_an_empty_database(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Revenue today")
        self.assertContains(response, "₱0.00")

    def test_shows_todays_delivered_revenue(self):
        make_order(
            self.customer,
            status=OrderStatus.DELIVERED,
            delivered_at=timezone.now(),
            total=Decimal("329.00"),
        )
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, "₱329.00")

    def test_pending_and_unassigned_orders_are_called_out(self):
        pending = make_order(self.customer, status=OrderStatus.PENDING)
        ready = make_order(self.customer, status=OrderStatus.READY)

        response = self.client.get(reverse("admin:index"))

        self.assertContains(response, pending.order_number)
        self.assertContains(response, ready.order_number)
        self.assertContains(response, "1 waiting for the shop to accept")
        self.assertContains(response, "1 accepted, no rider assigned")

    def test_an_assigned_order_is_not_listed_as_needing_a_rider(self):
        ready = make_order(self.customer, status=OrderStatus.READY)
        Delivery.objects.create(order=ready, rider=self.rider)

        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, "0 accepted, no rider assigned")

    def test_older_revenue_is_not_counted_as_todays(self):
        from orders.admin_api import dashboard_summary

        make_order(
            self.customer,
            status=OrderStatus.DELIVERED,
            delivered_at=timezone.now() - timedelta(days=2),
            total=Decimal("777.00"),
        )
        self.assertEqual(dashboard_summary()["today"]["revenue"], "0.00")
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, "Revenue today")

    def test_numbers_match_the_api_endpoint(self):
        """The page and GET /api/admin/dashboard/ share one function."""
        from orders.admin_api import dashboard_summary

        make_order(
            self.customer,
            status=OrderStatus.DELIVERED,
            delivered_at=timezone.now(),
            total=Decimal("329.00"),
        )
        summary = dashboard_summary()
        self.assertEqual(summary["today"]["revenue"], "329.00")
        self.assertEqual(summary["today"]["orders_delivered"], 1)

    def test_top_sellers_count_delivered_items(self):
        menu = Menu.objects.create(name="Hawaiian", price=Decimal("299.00"))
        order = make_order(
            self.customer, status=OrderStatus.DELIVERED, delivered_at=timezone.now()
        )
        OrderItem.objects.create(
            order=order,
            menu=menu,
            quantity=3,
            unit_price=Decimal("299.00"),
            line_subtotal=Decimal("897.00"),
        )
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, "Hawaiian")

    def test_requires_staff_login(self):
        self.client.logout()
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_sidebar_counts_orders_awaiting_confirmation(self):
        make_order(self.customer, status=OrderStatus.PENDING)
        make_order(self.customer, status=OrderStatus.PENDING)
        make_order(self.customer, status=OrderStatus.CONFIRMED)

        from core.admin_dashboard import pending_orders_badge

        self.assertEqual(pending_orders_badge(None), "2")

    def test_sidebar_badge_hides_at_zero(self):
        from core.admin_dashboard import pending_orders_badge

        self.assertIsNone(pending_orders_badge(None))

    def test_branding_assets_are_wired(self):
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, "core/img/logo.png")
        self.assertContains(response, "core/css/admin.css")
        self.assertContains(response, "Yellow Ribbon Pizza")

    @override_settings(DEBUG=True)
    def test_environment_ribbon_follows_debug(self):
        from core.admin_dashboard import environment_callback

        self.assertEqual(environment_callback(None)[0], "Development")
        with override_settings(DEBUG=False):
            self.assertEqual(environment_callback(None)[0], "Production")


class OrderListTests(AdminUITestBase):
    def test_changelist_shows_badges_and_rider(self):
        order = make_order(self.customer, status=OrderStatus.READY)
        Delivery.objects.create(order=order, rider=self.rider)

        response = self.client.get(self.changelist)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ready")
        self.assertContains(response, "rico")

    def test_search_by_customer_full_name(self):
        order = make_order(self.customer)
        found = self.client.get(self.changelist, {"q": "Santos"})
        self.assertContains(found, order.order_number)

        missed = self.client.get(self.changelist, {"q": "Nobody"})
        self.assertNotContains(missed, order.order_number)

    def test_date_range_filter_loads(self):
        order = make_order(self.customer)
        response = self.client.get(
            self.changelist,
            {"placed_at_from": "2020-01-01", "placed_at_to": "2099-12-31"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, order.order_number)

        response = self.client.get(
            self.changelist,
            {"placed_at_from": "2098-01-01", "placed_at_to": "2099-12-31"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, order.order_number)

    def test_status_cannot_be_typed_into_the_order_form(self):
        """Editing it there would skip the status log and the push."""
        order = make_order(self.customer)
        response = self.client.get(reverse("admin:orders_order_change", args=[order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("status", response.context["adminform"].form.fields)

    def test_change_page_has_workflow_buttons(self):
        order = make_order(self.customer)
        response = self.client.get(reverse("admin:orders_order_change", args=[order.pk]))
        self.assertContains(response, "Advance to next step")
        self.assertContains(response, "Cancel order")


class OrderStatusActionTests(AdminUITestBase):
    def test_confirm_moves_order_and_writes_the_trail(self):
        order = make_order(self.customer, status=OrderStatus.PENDING)

        response = self.run_action("confirm_orders", [order])

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
        self.assertIsNotNone(order.confirmed_at)
        log = OrderStatusLog.objects.get(order=order)
        self.assertEqual(log.status, OrderStatus.CONFIRMED)
        self.assertEqual(log.changed_by, self.boss)
        self.assertTrue(any("1 order(s) moved to confirmed" in m for m in self.flash(response)))

    def test_walks_the_happy_path_one_step_at_a_time(self):
        order = make_order(self.customer, status=OrderStatus.PENDING)

        for action, expected in (
            ("confirm_orders", OrderStatus.CONFIRMED),
            ("start_preparing", OrderStatus.PREPARING),
            ("mark_ready", OrderStatus.READY),
        ):
            self.run_action(action, [order])
            order.refresh_from_db()
            self.assertEqual(order.status, expected)

        self.assertEqual(OrderStatusLog.objects.filter(order=order).count(), 3)

    def test_illegal_jump_is_refused_and_named(self):
        order = make_order(self.customer, status=OrderStatus.PENDING)

        response = self.run_action("mark_ready", [order])

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING)
        self.assertFalse(OrderStatusLog.objects.filter(order=order).exists())
        self.assertTrue(any(order.order_number in m for m in self.flash(response)))

    def test_mixed_selection_moves_only_the_legal_ones(self):
        good = make_order(self.customer, status=OrderStatus.PENDING)
        done = make_order(self.customer, status=OrderStatus.DELIVERED)

        self.run_action("confirm_orders", [good, done])

        good.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual(good.status, OrderStatus.CONFIRMED)
        self.assertEqual(done.status, OrderStatus.DELIVERED)

    def test_cancel_records_the_reason(self):
        order = make_order(self.customer, status=OrderStatus.CONFIRMED)

        self.run_action("cancel_orders", [order], cancel_reason="Out of dough")

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(order.cancel_reason, "Out of dough")
        self.assertIsNotNone(order.cancelled_at)

    def test_a_delivered_order_cannot_be_cancelled(self):
        order = make_order(self.customer, status=OrderStatus.DELIVERED)

        self.run_action("cancel_orders", [order])

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.DELIVERED)

    def test_detail_button_advances_one_step(self):
        order = make_order(self.customer, status=OrderStatus.CONFIRMED)

        response = self.client.get(
            reverse("admin:orders_order_advance_order", args=[order.pk]), follow=True
        )

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PREPARING)
        self.assertEqual(response.status_code, 200)

    def test_detail_button_leaves_rider_steps_to_the_rider_app(self):
        order = make_order(self.customer, status=OrderStatus.READY)

        self.client.get(reverse("admin:orders_order_advance_order", args=[order.pk]))

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.READY)

    def test_detail_cancel_button(self):
        order = make_order(self.customer, status=OrderStatus.PENDING)

        self.client.get(reverse("admin:orders_order_cancel_order", args=[order.pk]))

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)

    def test_detail_buttons_on_a_missing_order_do_not_crash(self):
        response = self.client.get(
            reverse("admin:orders_order_advance_order", args=[999999]), follow=True
        )
        self.assertEqual(response.status_code, 200)

    def test_actions_need_the_change_permission(self):
        viewer = User.objects.create_user(
            username="viewer", email="v@example.com", password=PASSWORD, is_staff=True
        )
        from django.contrib.auth.models import Permission

        viewer.user_permissions.add(Permission.objects.get(codename="view_order"))
        self.client.force_login(viewer)
        order = make_order(self.customer, status=OrderStatus.PENDING)

        self.run_action("confirm_orders", [order])

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING)


class AssignRiderActionTests(AdminUITestBase):
    def test_assign_creates_delivery_with_snapshotted_earnings(self):
        order = make_order(self.customer, status=OrderStatus.CONFIRMED)

        self.run_action("assign_rider", [order], rider=self.rider.pk)

        delivery = Delivery.objects.get(order=order)
        self.assertEqual(delivery.rider, self.rider)
        self.assertEqual(delivery.status, DeliveryStatus.ASSIGNED)
        self.assertEqual(
            delivery.rider_earnings, Decimal(settings.RIDER_EARNINGS_PER_DELIVERY)
        )

    def test_no_rider_picked_is_an_error_not_a_crash(self):
        order = make_order(self.customer, status=OrderStatus.CONFIRMED)

        response = self.run_action("assign_rider", [order])

        self.assertFalse(Delivery.objects.exists())
        self.assertTrue(any("Pick a rider" in m for m in self.flash(response)))

    def test_unpaid_gcash_order_is_not_assigned(self):
        order = make_order(
            self.customer,
            status=OrderStatus.PENDING_PAYMENT,
            payment_type=PaymentType.GCASH,
        )

        self.run_action("assign_rider", [order], rider=self.rider.pk)

        self.assertFalse(Delivery.objects.exists())

    def test_already_assigned_order_is_skipped(self):
        order = make_order(self.customer, status=OrderStatus.CONFIRMED)
        other = Rider.objects.create(
            user=User.objects.create_user(
                username="pedro",
                email="pedro@example.com",
                password=PASSWORD,
                is_rider=True,
                is_customer=False,
            )
        )
        Delivery.objects.create(order=order, rider=other)

        response = self.run_action("assign_rider", [order], rider=self.rider.pk)

        self.assertEqual(Delivery.objects.get(order=order).rider, other)
        self.assertTrue(any("already assigned" in m for m in self.flash(response)))

    def test_deactivated_rider_is_refused(self):
        self.rider_user.is_active = False
        self.rider_user.save()
        order = make_order(self.customer, status=OrderStatus.CONFIRMED)

        self.run_action("assign_rider", [order], rider=self.rider.pk)

        self.assertFalse(Delivery.objects.exists())


class OtherActionTests(AdminUITestBase):
    def test_rider_duty_toggles(self):
        self.run_action(
            "set_off_duty", [self.rider], url=reverse("admin:accounts_rider_changelist")
        )
        self.rider.refresh_from_db()
        self.assertFalse(self.rider.is_available)

        self.run_action(
            "set_on_duty", [self.rider], url=reverse("admin:accounts_rider_changelist")
        )
        self.rider.refresh_from_db()
        self.assertTrue(self.rider.is_available)

    def test_menu_availability_toggles(self):
        item = Menu.objects.create(name="Garlic Bread", price=Decimal("89.00"))
        url = reverse("admin:menu_menu_changelist")

        self.run_action("mark_unavailable", [item], url=url)
        item.refresh_from_db()
        self.assertFalse(item.is_available)

        self.run_action("mark_available", [item], url=url)
        item.refresh_from_db()
        self.assertTrue(item.is_available)

    def test_deactivate_never_locks_out_the_acting_admin(self):
        url = reverse("admin:accounts_user_changelist")

        self.run_action("deactivate_users", [self.boss, self.cust_user], url=url)

        self.boss.refresh_from_db()
        self.cust_user.refresh_from_db()
        self.assertTrue(self.boss.is_active)
        self.assertFalse(self.cust_user.is_active)

    def test_activate_users(self):
        self.cust_user.is_active = False
        self.cust_user.save()

        self.run_action(
            "activate_users", [self.cust_user], url=reverse("admin:accounts_user_changelist")
        )

        self.cust_user.refresh_from_db()
        self.assertTrue(self.cust_user.is_active)

    def test_user_pages_still_work_with_the_unfold_forms(self):
        self.assertEqual(
            self.client.get(reverse("admin:accounts_user_add")).status_code, 200
        )
        self.assertEqual(
            self.client.get(
                reverse("admin:accounts_user_change", args=[self.cust_user.pk])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse("admin:auth_user_password_change", args=[self.cust_user.pk])
            ).status_code,
            200,
        )

    def test_can_still_create_a_user_through_the_admin(self):
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "username": "newbie",
                "email": "newbie@example.com",
                "password1": "Sup3r!Secret-Pass",
                "password2": "Sup3r!Secret-Pass",
                "is_customer": "on",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(username="newbie").exists())


class CsvExportTests(AdminUITestBase):
    def export(self, orders):
        return self.run_action("export_selected", orders)

    def test_orders_export_has_headers_and_rows(self):
        order = make_order(self.customer)

        response = self.export([order])

        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        text = response.content.decode("utf-8")
        self.assertEqual(text[0], chr(0xFEFF))
        self.assertTrue(text[1:].startswith("Order,Placed,Customer"))
        self.assertIn(order.order_number, text)
        self.assertIn("Maria Santos", text)

    def test_formula_in_a_customer_typed_field_is_defanged(self):
        order = make_order(self.customer, delivery_address='=HYPERLINK("http://evil")')

        text = self.export([order]).content.decode("utf-8")

        self.assertNotIn(",=HYPERLINK", text)
        self.assertIn("'=HYPERLINK", text)

    def test_export_needs_view_permission(self):
        nobody = User.objects.create_user(
            username="nobody", email="n@example.com", password=PASSWORD, is_staff=True
        )
        self.client.force_login(nobody)
        order = make_order(self.customer)

        response = self.client.post(
            self.changelist,
            {"action": "export_selected", "_selected_action": [order.pk], "index": "0"},
        )

        self.assertEqual(response.status_code, 403)
