"""
Maya Checkout: the client, checkout, sync, return pages and the stale-order reaper.

Every outbound call is mocked; the suite never touches Maya.
"""

import base64
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch

import requests
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from menu.models import Menu
from orders.constants import OrderStatus, PaymentStatus, PaymentType
from orders.models import Order, OrderItem
from orders.tests_admin import make_order
from payments import maya
from payments.fees import compute_fee, split
from payments.models import Invoice, Provider
from payments.services import issue_invoice, settle_payment, sync_maya_order

PASSWORD = "Kidapawan!2026"
PK = "pk-test-public"
SK = "sk-test-secret"
MAYA = override_settings(
    MAYA_PUBLIC_KEY=PK,
    MAYA_SECRET_KEY=SK,
    MAYA_BASE_URL="https://pg-sandbox.paymaya.com",
    MAYA_TIMEOUT_SECONDS=5,
)


def maya_payment(state="paid", *, amount="329.00", status_text=None, can_void=True,
                 can_refund=False, payer="Maya wallet ••••0900", pid="pay-uuid-1",
                 created=None):
    """What maya.retrieve_payment returns."""
    return {
        "id": pid,
        "status": status_text or {"paid": "PAYMENT_SUCCESS", "failed": "PAYMENT_FAILED",
                                  "pending": "PENDING_TOKEN"}[state],
        "state": state,
        "amount": Decimal(amount),
        "can_void": can_void,
        "can_refund": can_refund,
        "created_at": created or timezone.now(),
        "payer": payer,
        "raw": {},
    }


def fake_response(status_code=200, body=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body if body is not None else {}
    response.text = str(body)
    return response


def basic(key):
    return "Basic " + base64.b64encode(f"{key}:".encode()).decode()


class MayaBase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("maria", "maria@example.com", PASSWORD)
        self.customer = Customer.objects.create(user=self.user)
        self.other_user = User.objects.create_user("pedro", "pedro@example.com", PASSWORD)
        self.other_customer = Customer.objects.create(user=self.other_user)
        self.admin = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        self.menu = Menu.objects.create(name="Pepperoni", price=Decimal("299.00"))

    def maya_order(self, customer=None, **overrides):
        order = make_order(
            customer or self.customer,
            payment_type=PaymentType.MAYA,
            status=OrderStatus.PENDING_PAYMENT,
            **overrides,
        )
        OrderItem.objects.create(
            order=order, menu=self.menu, quantity=1,
            unit_price=Decimal("299.00"), line_subtotal=Decimal("299.00"),
        )
        return order

    def open_checkout(self, order, rrn="rrn-abc"):
        invoice, _ = issue_invoice(
            order, source_id="checkout-1", checkout_url="https://maya.test/checkout", rrn=rrn
        )
        return invoice


@MAYA
class MayaClientTests(APITestCase):
    def call(self, status_code=200, body=None):
        return patch("payments.maya.requests.request", return_value=fake_response(status_code, body))

    def test_checkout_body_is_what_maya_expects(self):
        user = User.objects.create_user("m", "m@example.com", PASSWORD)
        order = make_order(Customer.objects.create(user=user), payment_type=PaymentType.MAYA)
        menu = Menu.objects.create(name="Pizza", price=Decimal("299.00"))
        OrderItem.objects.create(order=order, menu=menu, quantity=1,
                                 unit_price=Decimal("299.00"), line_subtotal=Decimal("299.00"))

        body = maya.build_checkout_body(order, "rrn1", "https://x/ok", "https://x/no", "https://x/cx")

        self.assertEqual(body["totalAmount"], {"value": "329.00", "currency": "PHP"})
        self.assertEqual(body["requestReferenceNumber"], "rrn1")
        self.assertEqual(body["redirectUrl"],
                         {"success": "https://x/ok", "failure": "https://x/no", "cancel": "https://x/cx"})
        names = [i["name"] for i in body["items"]]
        self.assertEqual(names, ["Pizza", "Delivery fee"])
        self.assertEqual(body["items"][0]["totalAmount"], {"value": "299.00"})
        self.assertEqual(body["items"][1]["totalAmount"], {"value": "30.00"})

    def test_free_delivery_adds_no_fee_line(self):
        user = User.objects.create_user("m", "m@example.com", PASSWORD)
        order = make_order(Customer.objects.create(user=user), payment_type=PaymentType.MAYA,
                           delivery_fee=Decimal("0.00"), total=Decimal("299.00"))
        body = maya.build_checkout_body(order, "r", "a", "b", "c")
        self.assertNotIn("Delivery fee", [i["name"] for i in body["items"]])

    def test_creating_a_checkout_uses_the_public_key(self):
        user = User.objects.create_user("m", "m@example.com", PASSWORD)
        order = make_order(Customer.objects.create(user=user), payment_type=PaymentType.MAYA)
        with self.call(body={"checkoutId": "c-1", "redirectUrl": "https://pay.maya/c-1"}) as mocked:
            result = maya.create_checkout(order, "rrn1", "a", "b", "c")

        self.assertEqual(result, {"checkout_id": "c-1", "checkout_url": "https://pay.maya/c-1"})
        args, kwargs = mocked.call_args
        self.assertEqual(args, ("POST", "https://pg-sandbox.paymaya.com/checkout/v1/checkouts"))
        self.assertEqual(kwargs["headers"]["Authorization"], basic(PK))

    def test_a_checkout_without_a_link_is_an_error(self):
        user = User.objects.create_user("m", "m@example.com", PASSWORD)
        order = make_order(Customer.objects.create(user=user), payment_type=PaymentType.MAYA)
        with self.call(body={"checkoutId": "c-1"}):
            with self.assertRaises(maya.MayaError):
                maya.create_checkout(order, "r", "a", "b", "c")

    def test_lookups_use_the_secret_key_and_read_the_array(self):
        reply = [{"id": "pay-1", "isPaid": True, "status": "PAYMENT_SUCCESS", "amount": "329",
                  "canVoid": True, "canRefund": False, "createdAt": "2026-10-23T04:21:35.452Z"}]
        with self.call(body=reply) as mocked:
            payment = maya.retrieve_payment("rrn1")

        args, kwargs = mocked.call_args
        self.assertEqual(args[1], "https://pg-sandbox.paymaya.com/payments/v1/payment-rrns/rrn1")
        self.assertEqual(kwargs["headers"]["Authorization"], basic(SK))
        self.assertEqual(payment["state"], "paid")
        self.assertEqual(payment["amount"], Decimal("329"))
        self.assertTrue(payment["can_void"])
        self.assertEqual(payment["created_at"].year, 2026)

    def test_an_empty_lookup_is_an_error(self):
        with self.call(body=[]):
            with self.assertRaises(maya.MayaError):
                maya.retrieve_payment("nope")

    def test_states_map_to_paid_failed_pending(self):
        for status_text, expected in [
            ("PAYMENT_SUCCESS", "paid"), ("DONE", "paid"), ("CAPTURED", "paid"),
            ("PAYMENT_FAILED", "failed"), ("PAYMENT_EXPIRED", "failed"),
            ("PAYMENT_CANCELLED", "failed"), ("AUTH_FAILED", "failed"),
            ("VOIDED", "failed"), ("REFUNDED", "failed"),
            ("PENDING_TOKEN", "pending"), ("PENDING_PAYMENT", "pending"),
            ("FOR_AUTHENTICATION", "pending"), ("PAYMENT_PROCESSING", "pending"),
        ]:
            self.assertEqual(maya.classify(status_text), expected, status_text)

    def test_is_paid_flag_counts_even_with_an_unfamiliar_status(self):
        self.assertEqual(maya.classify("SOMETHING_NEW", is_paid=True), "paid")

    def test_payer_is_masked_to_the_last_four_digits(self):
        payment = {"fundSource": {"type": "maya-wallet", "description": "639900100900"}}
        self.assertEqual(maya.mask_payer(payment), "Maya wallet ••••0900")

    def test_payer_without_digits_is_just_the_wallet(self):
        self.assertEqual(maya.mask_payer({"fundSource": {"type": "maya-wallet"}}), "Maya wallet")

    def test_no_fund_source_means_no_payer(self):
        for payment in ({}, {"fundSource": None}, {"fundSource": "oops"}, {"fundSource": {}}):
            self.assertEqual(maya.mask_payer(payment), "")

    def test_the_full_account_number_never_leaks(self):
        label = maya.mask_payer({"fundSource": {"type": "maya-wallet", "description": "639900100900"}})
        self.assertNotIn("63990010", label)

    def test_maya_error_bodies_become_maya_errors(self):
        with self.call(400, {"code": "PY0045", "message": "Payment is not available for void."}):
            with self.assertRaises(maya.MayaError) as caught:
                maya.void_payment("rrn1", "test")
        self.assertEqual(caught.exception.code, "PY0045")
        self.assertIn("not available for void", str(caught.exception))

    def test_timeouts_and_network_errors_are_maya_errors(self):
        for failure in (requests.Timeout(), requests.ConnectionError()):
            with patch("payments.maya.requests.request", side_effect=failure):
                with self.assertRaises(maya.MayaError):
                    maya.retrieve_payment("rrn1")

    @override_settings(MAYA_SECRET_KEY="")
    def test_missing_keys_are_refused_before_any_call(self):
        with patch("payments.maya.requests.request") as mocked:
            with self.assertRaises(maya.MayaError):
                maya.retrieve_payment("rrn1")
        mocked.assert_not_called()

    def test_void_and_refund_calls(self):
        with self.call(body={"id": "void-1", "status": "SUCCESS"}) as mocked:
            result = maya.void_payment("rrn1", "cancelled")
        self.assertEqual(result, {"reference": "void-1", "outcome": "refunded"})
        self.assertTrue(mocked.call_args[0][1].endswith("/payment-rrns/rrn1/voids"))
        self.assertEqual(mocked.call_args[1]["json"], {"reason": "cancelled"})

        with self.call(body={"id": "ref-1", "status": "PENDING"}) as mocked:
            result = maya.refund_payment("rrn1", Decimal("329.00"), "cancelled")
        self.assertEqual(result, {"reference": "ref-1", "outcome": "pending"})
        self.assertTrue(mocked.call_args[0][1].endswith("/payment-rrns/rrn1/refunds"))
        self.assertEqual(mocked.call_args[1]["json"]["totalAmount"], {"amount": 329.0, "currency": "PHP"})

        with self.call(body={"id": "ref-2", "status": "FAILED"}):
            self.assertEqual(maya.refund_payment("rrn1", 1, "x")["outcome"], "failed")


class FeeTests(APITestCase):
    def test_percentage_plus_fixed_rounded_to_centavos(self):
        from core.models import StoreSettings

        store = StoreSettings.load()
        store.paymongo_fee_percent = Decimal("2.50")
        store.paymongo_fee_fixed = Decimal("1.00")
        store.save()
        # 2.5% of 329.00 = 8.225 -> 8.23, plus 1.00
        self.assertEqual(compute_fee(Provider.PAYMONGO, Decimal("329.00")), Decimal("9.23"))

    def test_each_provider_has_its_own_rate(self):
        from core.models import StoreSettings

        store = StoreSettings.load()
        store.maya_fee_percent = Decimal("1.00")
        store.maya_fee_fixed = Decimal("0.00")
        store.save()
        self.assertEqual(compute_fee(Provider.MAYA, Decimal("200.00")), Decimal("2.00"))
        self.assertEqual(compute_fee(Provider.PAYMONGO, Decimal("200.00")), Decimal("5.00"))

    def test_cod_costs_nothing(self):
        self.assertEqual(compute_fee(Provider.COD, Decimal("500.00")), Decimal("0.00"))
        self.assertEqual(compute_fee("", Decimal("500.00")), Decimal("0.00"))

    def test_fee_never_exceeds_the_payment(self):
        from core.models import StoreSettings

        store = StoreSettings.load()
        store.maya_fee_fixed = Decimal("50.00")
        store.save()
        self.assertEqual(compute_fee(Provider.MAYA, Decimal("10.00")), Decimal("10.00"))

    def test_split_adds_up(self):
        fee, net = split(Provider.PAYMONGO, Decimal("329.00"))
        self.assertEqual(fee + net, Decimal("329.00"))


@MAYA
class MayaOrderPlacementTests(MayaBase):
    def test_maya_is_a_payment_type_and_starts_pending_payment(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            reverse("orders:order-list"),
            {
                "items": [{"menu_id": self.menu.pk, "quantity": 1}],
                "delivery_address": "Blue gate",
                "delivery_latitude": "7.008300",
                "delivery_longitude": "125.089400",
                "payment_type": "maya",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get()
        self.assertEqual(order.payment_type, PaymentType.MAYA)
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)

    def test_constants_list_maya(self):
        self.client.force_authenticate(self.user)
        self.assertIn("maya", self.client.get(reverse("constants")).data["payment_type"])


@MAYA
class MayaCheckoutTests(MayaBase):
    url = reverse("payments:checkout")

    def post(self, order, user=None):
        self.client.force_authenticate(user or self.user)
        return self.client.post(self.url, {"order_id": order.pk}, format="json")

    @patch("payments.maya.create_checkout")
    def test_opens_a_checkout_and_records_it(self, create):
        create.return_value = {"checkout_id": "co-1", "checkout_url": "https://pay.maya/co-1"}
        order = self.maya_order()

        response = self.post(order)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            sorted(response.data),
            sorted(["order_id", "order_number", "invoice_number", "amount", "checkout_url", "reused", "provider"]),
        )
        self.assertEqual(response.data["checkout_url"], "https://pay.maya/co-1")
        self.assertEqual(response.data["provider"], "maya")
        self.assertEqual(response.data["amount"], "329.00")
        self.assertFalse(response.data["reused"])

        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.provider, Provider.MAYA)
        self.assertEqual(invoice.paymongo_payment_id, "co-1")
        self.assertEqual(len(invoice.provider_rrn), 32)
        self.assertIsNone(invoice.paid_at)

    @patch("payments.maya.create_checkout")
    def test_return_urls_carry_our_reference_number(self, create):
        create.return_value = {"checkout_id": "co-1", "checkout_url": "https://pay.maya/co-1"}
        order = self.maya_order()

        self.post(order)

        _, rrn, success, failed, cancel = create.call_args[0]
        self.assertEqual(rrn, Invoice.objects.get().provider_rrn)
        self.assertIn(reverse("payments:return-success"), success)
        self.assertIn(f"rrn={rrn}", success)
        self.assertIn(reverse("payments:return-failed"), failed)
        self.assertIn(f"rrn={rrn}", failed)
        self.assertEqual(failed, cancel)

    @patch("payments.maya.create_checkout")
    @patch("payments.maya.retrieve_payment")
    def test_a_live_checkout_is_reused(self, lookup, create):
        order = self.maya_order()
        self.open_checkout(order)
        lookup.return_value = maya_payment("pending")

        response = self.post(order)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["reused"])
        self.assertEqual(response.data["checkout_url"], "https://maya.test/checkout")
        create.assert_not_called()

    @patch("payments.maya.create_checkout")
    @patch("payments.maya.retrieve_payment")
    def test_a_dead_checkout_gets_a_fresh_one(self, lookup, create):
        create.return_value = {"checkout_id": "co-2", "checkout_url": "https://pay.maya/co-2"}
        order = self.maya_order()
        old = self.open_checkout(order, rrn="old-rrn")
        order.payment_status = PaymentStatus.FAILED
        order.save()
        lookup.return_value = maya_payment("failed")

        response = self.post(order)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        old.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(old.paymongo_payment_id, "co-2")
        self.assertNotEqual(old.provider_rrn, "old-rrn")
        self.assertEqual(order.payment_status, PaymentStatus.UNPAID)

    @patch("payments.maya.create_checkout")
    @patch("payments.maya.retrieve_payment")
    def test_a_stale_pending_checkout_is_replaced(self, lookup, create):
        """Maya links die after an hour; do not hand out one that is nearly dead."""
        create.return_value = {"checkout_id": "co-2", "checkout_url": "https://pay.maya/co-2"}
        order = self.maya_order()
        self.open_checkout(order)
        lookup.return_value = maya_payment("pending", created=timezone.now() - timedelta(minutes=55))

        response = self.post(order)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        create.assert_called_once()

    @patch("payments.maya.create_checkout")
    @patch("payments.maya.retrieve_payment")
    def test_already_paid_settles_and_refuses_a_second_checkout(self, lookup, create):
        order = self.maya_order()
        self.open_checkout(order)
        lookup.return_value = maya_payment("paid")

        response = self.post(order)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        create.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        self.assertEqual(order.status, OrderStatus.PENDING)

    @patch("payments.maya.create_checkout", side_effect=maya.MayaError("Maya did not respond."))
    def test_maya_being_down_is_a_502(self, _create):
        response = self.post(self.maya_order())
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertFalse(Invoice.objects.exists())

    @override_settings(MAYA_PUBLIC_KEY="", MAYA_SECRET_KEY="")
    def test_unconfigured_maya_is_a_503(self):
        response = self.post(self.maya_order())
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_someone_elses_order_is_a_404(self):
        order = self.maya_order(customer=self.other_customer)
        self.assertEqual(self.post(order).status_code, status.HTTP_404_NOT_FOUND)

    def test_an_order_not_awaiting_payment_is_refused(self):
        order = self.maya_order()
        order.status = OrderStatus.PENDING
        order.save()
        self.assertEqual(self.post(order).status_code, status.HTTP_400_BAD_REQUEST)

    def test_cod_is_still_refused(self):
        order = make_order(self.customer, payment_type=PaymentType.COD)
        self.assertEqual(self.post(order).status_code, status.HTTP_400_BAD_REQUEST)

    @patch("payments.views.create_gcash_source")
    def test_gcash_checkout_is_unchanged_apart_from_provider(self, source):
        source.return_value = {"source_id": "src_1", "checkout_url": "https://g/1", "amount_centavos": 32900}
        order = make_order(self.customer, payment_type=PaymentType.GCASH, status=OrderStatus.PENDING_PAYMENT)
        with override_settings(PAYMONGO_SECRET_KEY="sk_test_x"):
            response = self.post(order)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["provider"], "paymongo")
        self.assertEqual(Invoice.objects.get().provider, Provider.PAYMONGO)


@MAYA
class SyncTests(MayaBase):
    url = reverse("payments:sync")

    def sync(self, order, user=None):
        self.client.force_authenticate(user or self.user)
        return self.client.post(self.url, {"order_id": order.pk}, format="json")

    @patch("payments.maya.retrieve_payment")
    def test_a_paid_maya_order_is_settled(self, lookup):
        order = self.maya_order()
        self.open_checkout(order)
        lookup.return_value = maya_payment("paid", payer="Maya wallet ••••0900")

        response = self.sync(order)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "order_id": order.pk,
                "order_number": order.order_number,
                "provider": "maya",
                "payment_status": "paid",
                "order_status": "pending",
                "paid": True,
                "settled": True,
                "gateway_status": "PAYMENT_SUCCESS",
            },
        )
        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.provider, Provider.MAYA)
        self.assertEqual(invoice.paymongo_payment_id, "pay-uuid-1")
        self.assertEqual(invoice.payer, "Maya wallet ••••0900")
        self.assertEqual(invoice.amount_paid, Decimal("329.00"))
        self.assertIsNotNone(invoice.paid_at)

    @patch("payments.maya.retrieve_payment")
    def test_settlement_snapshots_fee_and_net(self, lookup):
        order = self.maya_order()
        self.open_checkout(order)
        lookup.return_value = maya_payment("paid")

        self.sync(order)

        invoice = Invoice.objects.get(order=order)
        # Default Maya rate is 1.5%: 329.00 * 0.015 = 4.935 -> 4.94
        self.assertEqual(invoice.fee, Decimal("4.94"))
        self.assertEqual(invoice.net_amount, Decimal("324.06"))

    @patch("payments.maya.retrieve_payment")
    def test_a_later_rate_change_does_not_rewrite_old_earnings(self, lookup):
        from core.models import StoreSettings

        order = self.maya_order()
        self.open_checkout(order)
        lookup.return_value = maya_payment("paid")
        self.sync(order)

        store = StoreSettings.load()
        store.maya_fee_percent = Decimal("10.00")
        store.save()

        self.assertEqual(Invoice.objects.get(order=order).fee, Decimal("4.94"))

    @patch("payments.maya.retrieve_payment")
    def test_syncing_twice_settles_once(self, lookup):
        order = self.maya_order()
        self.open_checkout(order)
        lookup.return_value = maya_payment("paid")

        first = self.sync(order)
        second = self.sync(order)

        self.assertTrue(first.data["settled"])
        self.assertFalse(second.data["settled"])
        self.assertTrue(second.data["paid"])
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(order.status_logs.filter(status="pending").count(), 1)

    @patch("payments.maya.retrieve_payment")
    def test_a_pending_payment_changes_nothing(self, lookup):
        order = self.maya_order()
        self.open_checkout(order)
        lookup.return_value = maya_payment("pending")

        response = self.sync(order)

        self.assertFalse(response.data["paid"])
        self.assertEqual(response.data["payment_status"], "unpaid")
        self.assertEqual(response.data["order_status"], "pending_payment")
        self.assertIsNone(Invoice.objects.get(order=order).paid_at)

    @patch("payments.maya.retrieve_payment")
    def test_a_failed_payment_is_marked_failed_but_the_order_stays_open(self, lookup):
        order = self.maya_order()
        self.open_checkout(order)
        lookup.return_value = maya_payment("failed")

        response = self.sync(order)

        self.assertEqual(response.data["payment_status"], "failed")
        self.assertEqual(response.data["order_status"], "pending_payment")

    @patch("payments.maya.retrieve_payment")
    def test_a_paid_amount_that_differs_from_the_total_is_never_settled(self, lookup):
        order = self.maya_order()
        self.open_checkout(order)
        lookup.return_value = maya_payment("paid", amount="1.00")

        response = self.sync(order)

        self.assertFalse(response.data["paid"])
        self.assertEqual(response.data["gateway_status"], "AMOUNT_MISMATCH")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(order.payment_status, PaymentStatus.UNPAID)

    @patch("payments.maya.retrieve_payment", side_effect=maya.MayaError("Could not reach Maya."))
    def test_maya_being_unreachable_is_a_502_and_changes_nothing(self, _lookup):
        order = self.maya_order()
        self.open_checkout(order)
        response = self.sync(order)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)

    def test_no_checkout_yet_is_just_unpaid(self):
        order = self.maya_order()
        response = self.sync(order)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["paid"])

    def test_someone_elses_order_is_a_404(self):
        order = self.maya_order(customer=self.other_customer)
        self.assertEqual(self.sync(order).status_code, status.HTTP_404_NOT_FOUND)

    def test_order_id_is_required(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, status.HTTP_400_BAD_REQUEST)

    def test_customers_only(self):
        order = self.maya_order()
        self.assertEqual(self.client.post(self.url, {"order_id": order.pk}).status_code, status.HTTP_401_UNAUTHORIZED)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(self.url, {"order_id": order.pk}).status_code, status.HTTP_403_FORBIDDEN)

    @patch("payments.maya.retrieve_payment")
    def test_non_maya_orders_report_state_without_calling_maya(self, lookup):
        order = make_order(self.customer, payment_type=PaymentType.COD)
        response = self.sync(order)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["provider"], "cod")
        lookup.assert_not_called()

    @patch("payments.maya.retrieve_payment")
    def test_an_already_paid_order_is_not_looked_up_again(self, lookup):
        order = self.maya_order()
        self.open_checkout(order)
        lookup.return_value = maya_payment("paid")
        self.sync(order)
        lookup.reset_mock()

        self.sync(order)

        lookup.assert_not_called()

    @patch("payments.maya.retrieve_payment")
    def test_payment_after_cancel_is_refunded_not_kept(self, lookup):
        """The customer finished paying after the order was cancelled."""
        order = self.maya_order()
        self.open_checkout(order)
        order.status = OrderStatus.CANCELLED
        order.save()
        lookup.return_value = maya_payment("paid")

        with patch("payments.maya.void_payment", return_value={"reference": "v-1", "outcome": "refunded"}) as void:
            with self.captureOnCommitCallbacks(execute=True):
                sync_maya_order(order)

        void.assert_called_once()
        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.refund_status, "refunded")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        self.assertEqual(order.status, OrderStatus.CANCELLED)


@MAYA
class ReturnPageTests(MayaBase):
    def land(self, name, rrn=None):
        url = reverse(f"payments:{name}")
        return self.client.get(url, {"rrn": rrn} if rrn else {})

    @patch("payments.maya.retrieve_payment")
    def test_success_page_settles_a_paid_maya_order(self, lookup):
        order = self.maya_order()
        self.open_checkout(order, rrn="r1")
        lookup.return_value = maya_payment("paid")

        response = self.land("return-success", "r1")

        self.assertContains(response, "yellowribbon://payment/success")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        self.assertEqual(order.status, OrderStatus.PENDING)

    @patch("payments.maya.retrieve_payment")
    def test_visiting_success_does_not_settle_what_maya_says_failed(self, lookup):
        order = self.maya_order()
        self.open_checkout(order, rrn="r1")
        lookup.return_value = maya_payment("failed")

        response = self.land("return-success", "r1")

        self.assertContains(response, "yellowribbon://payment/failed")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertNotEqual(order.payment_status, PaymentStatus.PAID)

    @patch("payments.maya.retrieve_payment")
    def test_visiting_success_does_not_settle_what_maya_says_pending(self, lookup):
        order = self.maya_order()
        self.open_checkout(order, rrn="r1")
        lookup.return_value = maya_payment("pending")

        self.land("return-success", "r1")

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)

    @patch("payments.maya.retrieve_payment")
    def test_failure_page_is_failed_but_still_honours_a_real_payment(self, lookup):
        order = self.maya_order()
        self.open_checkout(order, rrn="r1")
        lookup.return_value = maya_payment("paid")

        response = self.land("return-failed", "r1")

        self.assertContains(response, "yellowribbon://payment/success")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.PAID)

    @patch("payments.maya.retrieve_payment")
    def test_failure_page_for_an_unpaid_order(self, lookup):
        order = self.maya_order()
        self.open_checkout(order, rrn="r1")
        lookup.return_value = maya_payment("failed")
        self.assertContains(self.land("return-failed", "r1"), "yellowribbon://payment/failed")

    @patch("payments.maya.retrieve_payment")
    def test_an_unknown_reference_does_nothing_and_never_crashes(self, lookup):
        self.assertContains(self.land("return-success", "nope"), "yellowribbon://payment/success")
        self.assertContains(self.land("return-failed", "nope"), "yellowribbon://payment/failed")
        lookup.assert_not_called()

    @patch("payments.maya.retrieve_payment", side_effect=maya.MayaError("down"))
    def test_maya_being_down_does_not_break_the_page(self, _lookup):
        order = self.maya_order()
        self.open_checkout(order, rrn="r1")
        self.assertEqual(self.land("return-success", "r1").status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)

    def test_pages_without_a_reference_are_the_old_cosmetic_pages(self):
        self.assertContains(self.land("return-success"), "yellowribbon://payment/success")
        self.assertContains(self.land("return-failed"), "yellowribbon://payment/failed")

    @patch("payments.maya.retrieve_payment")
    def test_a_gcash_invoice_is_never_looked_up_with_maya(self, lookup):
        order = make_order(self.customer, payment_type=PaymentType.GCASH, status=OrderStatus.PENDING_PAYMENT)
        issue_invoice(order, source_id="src_1", checkout_url="https://g", rrn="same-rrn")
        self.land("return-success", "same-rrn")
        lookup.assert_not_called()


@MAYA
class StaleOrderReaperTests(MayaBase):
    def reap(self):
        out = StringIO()
        call_command("cancel_stale_orders", "--minutes", "30", stdout=out)
        return out.getvalue()

    def age(self, order, minutes=45):
        Order.objects.filter(pk=order.pk).update(placed_at=timezone.now() - timedelta(minutes=minutes))

    @patch("payments.maya.retrieve_payment")
    def test_a_paid_but_unsynced_maya_order_is_settled_not_cancelled(self, lookup):
        order = self.maya_order()
        self.open_checkout(order)
        self.age(order)
        lookup.return_value = maya_payment("paid")

        self.reap()

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING)
        self.assertEqual(order.payment_status, PaymentStatus.PAID)

    @patch("payments.maya.retrieve_payment")
    def test_an_unpaid_maya_order_is_cancelled(self, lookup):
        order = self.maya_order()
        self.open_checkout(order)
        self.age(order)
        lookup.return_value = maya_payment("pending")

        self.reap()

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)

    @patch("payments.maya.retrieve_payment", side_effect=maya.MayaError("Could not reach Maya."))
    def test_when_maya_cannot_be_asked_the_order_is_left_alone(self, _lookup):
        order = self.maya_order()
        self.open_checkout(order)
        self.age(order)

        output = self.reap()

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertIn("skipped", output)

    def test_a_maya_order_that_never_started_checkout_is_cancelled(self):
        order = self.maya_order()
        self.age(order)
        self.reap()
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)

    def test_recent_orders_are_untouched(self):
        order = self.maya_order()
        self.reap()
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)

    @patch("payments.maya.retrieve_payment")
    def test_gcash_orders_do_not_call_maya(self, lookup):
        order = make_order(self.customer, payment_type=PaymentType.GCASH, status=OrderStatus.PENDING_PAYMENT)
        self.age(order)
        self.reap()
        lookup.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
