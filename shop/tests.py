"""
The customer web app at /order/ (the iPhone PWA): the session cart, checkout
through the same serializer as the API, the order page, signing in and up, push
registration, and the files that make it installable. Payment gateways are
mocked.
"""

import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.response import Response

from accounts.models import Customer, DeviceToken, Rider, User
from menu.models import Menu
from orders.constants import OrderStatus, PaymentStatus, PaymentType
from orders.models import Order, OrderStatusLog
from orders.tests_admin import make_order
from payments import maya

from . import display
from .cart import MAX_QUANTITY, SESSION_KEY

PASSWORD = "Kidapawan!2026"
INSIDE = {"delivery_latitude": "7.009500", "delivery_longitude": "125.090000"}


class ShopBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("maria", "maria@example.com", PASSWORD, first_name="Maria")
        self.customer = Customer.objects.create(user=self.user)
        self.pizza = Menu.objects.create(name="Pepperoni Pizza", price=Decimal("299.00"))
        self.pasta = Menu.objects.create(name="Carbonara Pasta", price=Decimal("150.50"))
        self.gone = Menu.objects.create(name="Truffle Pizza", price=Decimal("549.00"), is_available=False)

    def sign_in(self, user=None):
        self.client.force_login(user or self.user)

    def add(self, item, times=1):
        for _ in range(times):
            self.client.post(reverse("shop:menu"), {"menu_id": item.pk})

    def cart(self):
        return self.client.session.get(SESSION_KEY, {})


class MenuAndCartTests(ShopBase):
    def setUp(self):
        super().setUp()
        self.sign_in()

    def test_the_menu_lists_every_dish_and_sold_out_ones_cannot_be_ordered(self):
        response = self.client.get(reverse("shop:menu"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pepperoni Pizza")
        self.assertContains(response, "Truffle Pizza")
        self.assertContains(response, "SOLD OUT", count=1)
        self.assertContains(response, "Freshly baked, made to order.")  # no description yet

    def test_ordering_says_so_and_puts_a_count_on_the_cart_icon(self):
        response = self.client.post(reverse("shop:menu"), {"menu_id": self.pizza.pk}, follow=True)
        self.assertContains(response, "Pepperoni Pizza added to cart")
        self.assertContains(response, 'class="count"')

    def test_adding_keeps_ids_and_quantities_in_the_session(self):
        self.add(self.pizza, 2)
        self.add(self.pasta)
        self.assertEqual(self.cart(), {str(self.pizza.pk): 2, str(self.pasta.pk): 1})

    def test_an_unavailable_or_unknown_dish_is_not_added(self):
        self.add(self.gone)
        self.client.post(reverse("shop:menu"), {"menu_id": 99999})
        self.client.post(reverse("shop:menu"), {"menu_id": "abc"})
        self.assertEqual(self.cart(), {})

    def test_quantity_is_capped(self):
        self.add(self.pizza)
        self.client.post(reverse("shop:cart"), {"menu_id": self.pizza.pk, "quantity": 500})
        self.assertEqual(self.cart(), {str(self.pizza.pk): MAX_QUANTITY})

    def test_zero_or_nonsense_removes_the_line(self):
        self.add(self.pizza)
        self.add(self.pasta)
        self.client.post(reverse("shop:cart"), {"menu_id": self.pizza.pk, "quantity": 0})
        self.client.post(reverse("shop:cart"), {"menu_id": self.pasta.pk, "quantity": "lots"})
        self.assertEqual(self.cart(), {})

    def test_the_cart_shows_database_prices_and_the_badge(self):
        self.add(self.pizza, 2)
        response = self.client.get(reverse("shop:cart"))
        self.assertEqual(response.context["subtotal"], Decimal("598.00"))
        self.assertEqual(response.context["cart_count"], 2)

    def test_a_tampered_session_is_ignored(self):
        session = self.client.session
        session[SESSION_KEY] = {str(self.pizza.pk): 1, "x": 3, str(self.pasta.pk): -4, "7": "9"}
        session.save()
        response = self.client.get(reverse("shop:cart"))
        self.assertEqual([i["menu"] for i in response.context["items"]], [self.pizza])

    def test_a_dish_that_went_off_the_menu_is_left_out_of_the_subtotal(self):
        self.add(self.pizza)
        self.add(self.pasta)
        Menu.objects.filter(pk=self.pasta.pk).update(is_available=False)
        response = self.client.get(reverse("shop:cart"))
        self.assertEqual(response.context["subtotal"], Decimal("299.00"))
        self.assertTrue(response.context["has_unavailable"])


class AccessTests(ShopBase):
    def test_signed_out_customers_are_sent_to_sign_in(self):
        # Even the menu: log in or create an account first, as in the Android app.
        for name in ("shop:menu", "shop:cart", "shop:checkout", "shop:orders", "shop:profile", "shop:profile-edit"):
            response = self.client.get(reverse(name))
            self.assertRedirects(
                response, f"{reverse('shop:sign-in')}?next={reverse(name)}", fetch_redirect_response=False
            )

    def test_adding_to_the_cart_needs_a_customer_too(self):
        self.add(self.pizza)
        self.assertEqual(self.cart(), {})

    def test_a_rider_account_is_turned_away(self):
        rider = User.objects.create_user("rico", "rico@example.com", PASSWORD, is_rider=True, is_customer=False)
        self.sign_in(rider)
        for name in ("shop:menu", "shop:orders"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 403)
            self.assertTemplateUsed(response, "shop/not_customer.html")

    def test_someone_elses_order_is_not_found(self):
        other = Customer.objects.create(user=User.objects.create_user("pedro", "pedro@example.com", PASSWORD))
        order = make_order(other)
        self.sign_in()
        self.assertEqual(self.client.get(reverse("shop:order", args=[order.pk])).status_code, 404)


class CheckoutTests(ShopBase):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.add(self.pizza, 2)

    def place(self, **fields):
        data = {"delivery_address": "Blue gate", "payment_type": "cod", "remember_address": "on", **INSIDE}
        data.update(fields)
        return self.client.post(reverse("shop:checkout"), data)

    def test_an_empty_cart_goes_back_to_the_cart(self):
        self.client.post(reverse("shop:cart"), {"menu_id": self.pizza.pk, "quantity": 0})
        self.assertRedirects(self.client.get(reverse("shop:checkout")), reverse("shop:cart"))

    def test_cash_on_delivery_places_the_order_and_opens_it(self):
        response = self.place(delivery_notes="Call when outside")
        order = Order.objects.get()
        self.assertRedirects(response, reverse("shop:order", args=[order.pk]), fetch_redirect_response=False)
        self.assertEqual(order.customer, self.customer)
        self.assertEqual(order.subtotal, Decimal("598.00"))
        self.assertEqual(order.payment_type, PaymentType.COD)
        self.assertEqual(order.delivery_notes, "Call when outside")
        self.assertEqual(self.cart(), {})

    def test_the_usual_address_is_saved_only_when_ticked(self):
        self.place(delivery_address="Blue gate", remember_address="")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.default_address, "")

        self.add(self.pizza)
        self.place(delivery_address="Green gate")
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.default_address, "Green gate")
        self.assertEqual(self.customer.default_latitude, Decimal("7.009500"))

    def test_no_pin_asks_for_one(self):
        response = self.place(delivery_latitude="", delivery_longitude="")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Drop the pin on the map")
        self.assertFalse(Order.objects.exists())

    def test_the_service_area_rule_comes_from_the_api_serializer(self):
        response = self.place(delivery_latitude="14.599500", delivery_longitude="120.984200")  # Manila
        self.assertContains(response, "We only deliver within")
        self.assertFalse(Order.objects.exists())
        self.assertEqual(self.cart(), {str(self.pizza.pk): 2})

    def test_an_unavailable_dish_in_the_cart_is_left_out_of_the_order(self):
        self.add(self.pasta)
        Menu.objects.filter(pk=self.pasta.pk).update(is_available=False)
        self.place()
        self.assertEqual([i.menu for i in Order.objects.get().items.all()], [self.pizza])

    def test_online_payment_opens_the_payment_page(self):
        reply = Response({"checkout_url": "https://pay.example/abc"}, status=201)
        with patch("shop.views.start_checkout", return_value=reply) as start:
            response = self.place(payment_type="gcash")
        order = Order.objects.get()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertRedirects(response, "https://pay.example/abc", fetch_redirect_response=False)
        self.assertEqual(start.call_args.args[1], order)
        self.assertTrue(start.call_args.kwargs["web"])

    def test_a_payment_page_that_will_not_open_shows_the_reason_on_the_order(self):
        reply = Response({"error": "GCash is not set up."}, status=503)
        with patch("shop.views.start_checkout", return_value=reply):
            response = self.place(payment_type="gcash")
        order = Order.objects.get()
        self.assertRedirects(response, reverse("shop:order", args=[order.pk]), fetch_redirect_response=False)
        page = self.client.get(reverse("shop:order", args=[order.pk]))
        self.assertContains(page, "GCash is not set up.")


class OrderPageTests(ShopBase):
    def setUp(self):
        super().setUp()
        self.sign_in()

    def url(self, order):
        return reverse("shop:order", args=[order.pk])

    def test_an_order_awaiting_gcash_or_maya_polls_the_payment_sync(self):
        for payment_type in (PaymentType.GCASH, PaymentType.MAYA):
            order = make_order(self.customer, payment_type=payment_type, status=OrderStatus.PENDING_PAYMENT)
            config = self.client.get(self.url(order)).context["track_config"]
            self.assertEqual(config["syncUrl"], reverse("payments:sync"), payment_type)
            self.assertEqual(config["orderId"], order.pk)

    def test_a_cash_or_paid_order_does_not_poll_the_sync(self):
        cash = make_order(self.customer)
        paid = make_order(self.customer, payment_type=PaymentType.GCASH, payment_status=PaymentStatus.PAID)
        for order in (cash, paid):
            self.assertIsNone(self.client.get(self.url(order)).context["track_config"]["syncUrl"])

    def test_the_sync_accepts_the_web_apps_session_with_its_csrf_token(self):
        from django.test import Client

        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        order = make_order(self.customer)
        page = browser.get(self.url(order))
        token = page.context["csrf_token"]
        url = reverse("payments:sync")
        body = json.dumps({"order_id": order.pk})
        self.assertEqual(browser.post(url, body, content_type="application/json").status_code, 403)
        response = browser.post(url, body, content_type="application/json", HTTP_X_CSRFTOKEN=str(token))
        self.assertEqual(response.status_code, 200)

    def test_the_page_shows_progress_and_polls_the_api(self):
        order = make_order(self.customer, status=OrderStatus.PREPARING)
        response = self.client.get(self.url(order))
        progress = response.context["p"]
        self.assertEqual((progress["done"], progress["total"], progress["step_label"]), (3, 7, "Preparing"))
        self.assertContains(response, "Step 3 of 7")
        self.assertContains(response, "Your order is being prepared.")
        self.assertTrue(response.context["live"])
        self.assertEqual(response.context["track_config"]["orderUrl"], f"/api/orders/{order.pk}/")

    def test_progress_follows_the_app_through_every_status(self):
        expected = {
            OrderStatus.PENDING_PAYMENT: (0, "Awaiting payment"),
            OrderStatus.PENDING: (1, "Order placed"),
            OrderStatus.CONFIRMED: (2, "Confirmed"),
            OrderStatus.READY: (4, "Ready for pickup"),
            OrderStatus.OUT_FOR_DELIVERY: (5, "Picked up"),
            OrderStatus.DELIVERED: (7, "Delivered"),
            OrderStatus.CANCELLED: (None, "Cancelled"),
        }
        for status, (done, label) in expected.items():
            progress = display.progress(make_order(self.customer, status=status))
            self.assertEqual((progress["done"], progress["step_label"]), (done, label), status)

    def test_a_rider_on_the_road_is_step_six(self):
        from delivery.models import Delivery

        order = make_order(self.customer, status=OrderStatus.OUT_FOR_DELIVERY)
        rider_user = User.objects.create_user("rico", "rico@example.com", PASSWORD, is_rider=True, is_customer=False)
        rider = Rider.objects.create(user=rider_user)
        Delivery.objects.create(order=order, rider=rider, status="in_transit")
        progress = display.progress(Order.objects.get(pk=order.pk))
        self.assertEqual(
            (progress["done"], progress["step_label"], progress["style"]["label"]), (6, "On the way", "In transit")
        )

    def test_the_payment_card_shows_a_pay_button_while_unpaid(self):
        order = make_order(self.customer, payment_type=PaymentType.GCASH, status=OrderStatus.PENDING_PAYMENT)
        response = self.client.get(self.url(order))
        self.assertContains(response, "Awaiting payment")
        self.assertContains(response, "PAY WITH GCASH")
        self.assertContains(response, "Waiting for your GCash payment to be confirmed.")

    def test_the_progress_timeline_lists_each_change(self):
        order = make_order(self.customer)
        OrderStatusLog.objects.filter(order=order).delete()
        OrderStatusLog.objects.create(order=order, status=OrderStatus.PENDING)
        OrderStatusLog.objects.create(order=order, status=OrderStatus.CONFIRMED)
        timeline = self.client.get(self.url(order)).context["timeline"]
        self.assertEqual([t["label"] for t in timeline], ["Pending", "Confirmed"])

    def test_the_order_api_accepts_the_session(self):
        order = make_order(self.customer)
        response = self.client.get(f"/api/orders/{order.pk}/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["order_number"], order.order_number)

    def test_a_pending_order_can_be_cancelled(self):
        order = make_order(self.customer)
        self.client.post(self.url(order), {"action": "cancel"})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)

    def test_an_order_in_the_oven_cannot_be_cancelled(self):
        order = make_order(self.customer, status=OrderStatus.PREPARING)
        response = self.client.post(self.url(order), {"action": "cancel"}, follow=True)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PREPARING)
        self.assertEqual(len(list(response.context["messages"])), 1)

    def test_pay_reopens_the_payment_page_while_unpaid(self):
        order = make_order(self.customer, payment_type=PaymentType.GCASH, status=OrderStatus.PENDING_PAYMENT)
        reply = Response({"checkout_url": "https://pay.example/again"}, status=201)
        with patch("shop.views.start_checkout", return_value=reply):
            response = self.client.post(self.url(order), {"action": "pay"})
        self.assertRedirects(response, "https://pay.example/again", fetch_redirect_response=False)

    def test_pay_does_nothing_for_cash_on_delivery(self):
        order = make_order(self.customer)
        with patch("shop.views.start_checkout") as start:
            response = self.client.post(self.url(order), {"action": "pay"})
        start.assert_not_called()
        self.assertRedirects(response, self.url(order), fetch_redirect_response=False)

    def test_the_orders_list_shows_only_mine(self):
        mine = make_order(self.customer)
        other = Customer.objects.create(user=User.objects.create_user("pedro", "pedro@example.com", PASSWORD))
        make_order(other)
        response = self.client.get(reverse("shop:orders"))
        self.assertEqual(list(response.context["orders"]), [mine])
        self.assertContains(response, "Order info")
        self.assertContains(response, "1 of 7")


class PaymentReturnTests(ShopBase):
    def land(self, outcome, order=""):
        return self.client.get(reverse("shop:payment-return", args=[outcome]), {"order": order})

    def test_only_success_and_failed_exist(self):
        self.assertEqual(self.land("maybe").status_code, 404)

    def test_signed_out_it_just_says_how_it_went(self):
        order = make_order(self.customer, payment_type=PaymentType.GCASH, status=OrderStatus.PENDING_PAYMENT)
        response = self.land("success", str(order.pk))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "shop/payment_return.html")

    def test_signed_in_it_goes_back_to_the_order(self):
        self.sign_in()
        order = make_order(self.customer, payment_type=PaymentType.GCASH, status=OrderStatus.PENDING_PAYMENT)
        response = self.land("failed", str(order.pk))
        self.assertRedirects(response, reverse("shop:order", args=[order.pk]), fetch_redirect_response=False)

    def test_someone_elses_order_number_is_not_followed(self):
        other = Customer.objects.create(user=User.objects.create_user("pedro", "pedro@example.com", PASSWORD))
        order = make_order(other, payment_type=PaymentType.GCASH, status=OrderStatus.PENDING_PAYMENT)
        self.sign_in()
        for value in (str(order.pk), "abc", "４２"):
            self.assertEqual(self.land("success", value).status_code, 200, value)

    def test_an_unpaid_maya_order_is_checked_with_maya(self):
        self.sign_in()
        order = make_order(self.customer, payment_type=PaymentType.MAYA, status=OrderStatus.PENDING_PAYMENT)

        def mark_paid(o):
            Order.objects.filter(pk=o.pk).update(payment_status=PaymentStatus.PAID, status=OrderStatus.PENDING)

        with patch("shop.views.sync_order_payment", side_effect=mark_paid) as sync:
            response = self.land("success", str(order.pk))
        sync.assert_called_once()
        page = self.client.get(response.url)
        self.assertContains(page, "Payment received")

    def test_maya_being_down_does_not_break_the_return(self):
        self.sign_in()
        order = make_order(self.customer, payment_type=PaymentType.MAYA, status=OrderStatus.PENDING_PAYMENT)
        with patch("shop.views.sync_order_payment", side_effect=maya.MayaError("down")):
            response = self.land("success", str(order.pk))
        self.assertRedirects(response, reverse("shop:order", args=[order.pk]), fetch_redirect_response=False)


class AccountTests(ShopBase):
    def test_sign_up_makes_a_signed_in_customer(self):
        response = self.client.post(reverse("shop:sign-up"), {
            "name": "Ana Dela Cruz", "username": "ana", "phone": "09171234567",
            "password": PASSWORD, "password_confirm": PASSWORD,
        })
        self.assertRedirects(response, reverse("shop:menu"), fetch_redirect_response=False)
        user = User.objects.get(username="ana")
        # One Name box, split on the first space as the app does.
        self.assertEqual((user.first_name, user.last_name), ("Ana", "Dela Cruz"))
        self.assertTrue(Customer.objects.filter(user=user).exists())
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_sign_up_shows_the_api_rules(self):
        response = self.client.post(reverse("shop:sign-up"), {
            "name": "Ana", "username": "maria", "phone": "09171234567",
            "password": PASSWORD, "password_confirm": "different",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(first_name="Ana").exists())
        self.assertTrue(response.context["form"].errors)

    def test_sign_in_follows_next_only_within_the_app(self):
        data = {"username": "maria", "password": PASSWORD}
        response = self.client.post(reverse("shop:sign-in"), {**data, "next": "/order/orders/"})
        self.assertRedirects(response, "/order/orders/", fetch_redirect_response=False)
        self.client.logout()
        response = self.client.post(reverse("shop:sign-in"), {**data, "next": "https://evil.example.com/"})
        self.assertRedirects(response, reverse("shop:menu"), fetch_redirect_response=False)

    def test_a_rider_cannot_sign_in_here(self):
        User.objects.create_user("rico", "rico@example.com", PASSWORD, is_rider=True, is_customer=False)
        response = self.client.post(reverse("shop:sign-in"), {"username": "rico", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ribbon Rider app")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signing_out_goes_back_to_login(self):
        self.sign_in()
        response = self.client.post(reverse("shop:sign-out"))
        self.assertRedirects(response, reverse("shop:sign-in"), fetch_redirect_response=False)

    def test_the_profile_shows_the_details_read_only(self):
        self.sign_in()
        response = self.client.get(reverse("shop:profile"))
        self.assertContains(response, "@maria")
        self.assertContains(response, "EDIT PROFILE")
        self.assertContains(response, "Not set")

    def test_edit_profile_saves_details_and_the_default_address(self):
        self.sign_in()
        data = {"first_name": "Maria", "last_name": "Santos", "phone": "0917", "default_address": "Blue gate"}
        response = self.client.post(reverse("shop:profile-edit"), data)
        self.assertRedirects(response, reverse("shop:profile"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(
            (self.user.last_name, self.user.phone, self.user.email), ("Santos", "0917", "maria@example.com")
        )
        self.assertEqual(self.customer.default_address, "Blue gate")

    def test_edit_profile_needs_a_first_name_and_phone(self):
        self.sign_in()
        response = self.client.post(reverse("shop:profile-edit"), {"first_name": "", "phone": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.context["form"].errors), {"first_name", "phone"})


class PushTests(ShopBase):
    def register(self, token="web-tok-1"):
        return self.client.post(
            reverse("shop:push-register"), data=json.dumps({"token": token}), content_type="application/json"
        )

    def test_a_customer_registers_a_web_token(self):
        self.sign_in()
        self.assertEqual(self.register().status_code, 200)
        token = DeviceToken.objects.get()
        self.assertEqual((token.user, token.platform, token.fcm_token), (self.user, "web", "web-tok-1"))

    def test_signed_out_or_empty_is_refused(self):
        self.assertEqual(self.register().status_code, 403)
        self.sign_in()
        self.assertEqual(self.register("").status_code, 400)
        self.assertEqual(self.register("x" * 300).status_code, 400)
        self.assertFalse(DeviceToken.objects.exists())

    def test_a_shared_phone_moves_to_whoever_signed_in_last(self):
        self.sign_in()
        self.register("shared")
        other = User.objects.create_user("pedro", "pedro@example.com", PASSWORD)
        Customer.objects.create(user=other)
        self.sign_in(other)
        self.register("shared")
        self.assertEqual(DeviceToken.objects.get().user, other)

    def test_signing_out_stops_this_phones_notifications(self):
        self.sign_in()
        self.register()
        self.client.post(reverse("shop:sign-out"))
        self.assertFalse(DeviceToken.objects.exists())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unregister_removes_only_this_phone(self):
        DeviceToken.objects.create(user=self.user, fcm_token="android-tok", platform="android")
        self.sign_in()
        self.register()
        self.client.post(reverse("shop:push-unregister"))
        self.assertEqual(list(DeviceToken.objects.values_list("fcm_token", flat=True)), ["android-tok"])


class InstallableAppTests(TestCase):
    def test_the_manifest_makes_a_standalone_app_scoped_to_order(self):
        response = self.client.get(reverse("shop:manifest"))
        self.assertEqual(response["Content-Type"], "application/manifest+json")
        data = response.json()
        self.assertEqual((data["start_url"], data["scope"], data["display"]), ("/order/", "/order/", "standalone"))
        self.assertIn("maskable", [i.get("purpose") for i in data["icons"]])

    @override_settings(FIREBASE_WEB_CONFIG={"projectId": "delivery-ab221", "messagingSenderId": "1"})
    def test_the_service_worker_covers_order_and_carries_the_firebase_config(self):
        response = self.client.get(reverse("shop:sw"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Service-Worker-Allowed"], "/order/")
        self.assertEqual(response["Cache-Control"], "no-cache")
        self.assertIn("application/javascript", response["Content-Type"])
        body = response.content.decode()
        self.assertIn('"projectId": "delivery-ab221"', body)
        self.assertIn("firebase-messaging-compat.js", body)

    def test_icons_are_allowlisted(self):
        response = self.client.get(reverse("shop:icon", args=["icon-192.png"]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("shop/icons/icon-192", response["Location"])
        self.assertEqual(self.client.get(reverse("shop:icon", args=["secret.png"])).status_code, 404)

    def test_every_page_links_the_manifest_and_push_config(self):
        response = self.client.get(reverse("shop:sign-in"))
        self.assertContains(response, reverse("shop:manifest"))
        self.assertContains(response, 'id="push-config"')
        self.assertContains(response, "apple-mobile-web-app-capable")
