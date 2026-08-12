"""
PayMongo tests.

Every outbound call is mocked — the suite never touches the real API.
"""

import hashlib
import hmac
import json
import time
from decimal import Decimal
from unittest.mock import patch

import requests
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, Rider, User
from delivery.models import Delivery
from menu.models import Menu
from orders.constants import OrderStatus, PaymentStatus, PaymentType
from orders.models import Order, OrderStatusLog
from payments.gateway import PayMongoError, to_centavos
from payments.models import Invoice
from payments.signature import verify_signature

WEBHOOK_SECRET = "whsk_test_secret"
SECRET_KEY = "sk_test_notreal"

CHECKOUT_URL = "https://test-sources.paymongo.com/sources?id=src_abc123"
SOURCE_ID = "src_abc123"
PAYMENT_ID = "pay_xyz789"


def sign(body_bytes, secret=WEBHOOK_SECRET, timestamp=None, field="te"):
    timestamp = timestamp or str(int(time.time()))
    digest = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body_bytes, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},{field}={digest}"


def event_body(event_type, *, source_id=SOURCE_ID, payment_id=None):
    data = {"id": source_id, "type": "source", "attributes": {"status": "chargeable"}}
    if payment_id:
        data = {
            "id": payment_id,
            "type": "payment",
            "attributes": {
                "status": "paid",
                "source": {"id": source_id, "type": "source"},
            },
        }
    return json.dumps(
        {
            "data": {
                "id": "evt_1",
                "type": "event",
                "attributes": {"type": event_type, "data": data},
            }
        }
    ).encode()


class SignatureTests(APITestCase):
    body = b'{"data":{"id":"evt_1"}}'

    def test_valid_signature_accepted(self):
        header = sign(self.body)
        self.assertTrue(verify_signature(self.body, header, WEBHOOK_SECRET))

    def test_live_mode_field_also_accepted(self):
        header = sign(self.body, field="li")
        self.assertTrue(verify_signature(self.body, header, WEBHOOK_SECRET))

    def test_wrong_secret_rejected(self):
        header = sign(self.body, secret="whsk_someone_elses")
        self.assertFalse(verify_signature(self.body, header, WEBHOOK_SECRET))

    def test_tampered_body_rejected(self):
        header = sign(self.body)
        self.assertFalse(
            verify_signature(b'{"data":{"id":"evt_2"}}', header, WEBHOOK_SECRET)
        )

    def test_missing_header_rejected(self):
        self.assertFalse(verify_signature(self.body, "", WEBHOOK_SECRET))

    def test_missing_timestamp_rejected(self):
        self.assertFalse(verify_signature(self.body, "te=abc", WEBHOOK_SECRET))

    def test_garbage_header_rejected(self):
        for header in ("nonsense", "t=", "t=1,te=", "=,=", "t=1,zz=abc"):
            self.assertFalse(verify_signature(self.body, header, WEBHOOK_SECRET), header)

    def test_unconfigured_secret_rejects_everything(self):
        header = sign(self.body)
        self.assertFalse(verify_signature(self.body, header, ""))

    def test_old_signature_rejected_when_tolerance_is_set(self):
        header = sign(self.body, timestamp=str(int(time.time()) - 3600))
        self.assertFalse(
            verify_signature(self.body, header, WEBHOOK_SECRET, tolerance_seconds=300)
        )

    def test_old_signature_accepted_when_tolerance_is_off(self):
        header = sign(self.body, timestamp=str(int(time.time()) - 3600))
        self.assertTrue(
            verify_signature(self.body, header, WEBHOOK_SECRET, tolerance_seconds=0)
        )

    def test_fresh_signature_passes_the_window(self):
        header = sign(self.body)
        self.assertTrue(
            verify_signature(self.body, header, WEBHOOK_SECRET, tolerance_seconds=300)
        )


class CentavoTests(APITestCase):
    def test_converts_pesos_to_integer_centavos(self):
        self.assertEqual(to_centavos(Decimal("628.00")), 62800)
        self.assertEqual(to_centavos(Decimal("30.50")), 3050)
        self.assertEqual(to_centavos(Decimal("1.00")), 100)

    def test_returns_an_int_not_a_float(self):
        self.assertIsInstance(to_centavos(Decimal("628.00")), int)


class PaymentTestBase(APITestCase):
    def setUp(self):
        self.cust_user = User.objects.create_user(
            username="maria", email="maria@example.com", password="Kidapawan!2026"
        )
        self.customer = Customer.objects.create(user=self.cust_user)
        self.other_user = User.objects.create_user(
            username="pedro", email="pedro@example.com", password="Kidapawan!2026"
        )
        Customer.objects.create(user=self.other_user)
        self.admin = User.objects.create_superuser(
            username="boss", email="boss@example.com", password="Kidapawan!2026"
        )
        self.menu = Menu.objects.create(name="Pepperoni", price=Decimal("299.00"))

        self.order = self.make_order()
        self.checkout_url = reverse("payments:checkout")
        self.webhook_url = reverse("payments:webhook")

    def make_order(self, **overrides):
        fields = {
            "order_number": f"YR-P{Order.objects.count() + 1:04d}",
            "customer": self.customer,
            "status": OrderStatus.PENDING_PAYMENT,
            "payment_type": PaymentType.GCASH,
            "payment_status": PaymentStatus.UNPAID,
            "subtotal": Decimal("598.00"),
            "delivery_fee": Decimal("30.00"),
            "total": Decimal("628.00"),
            "delivery_address": "Blue gate, Quezon Blvd",
            "delivery_latitude": Decimal("7.008300"),
            "delivery_longitude": Decimal("125.089400"),
        }
        fields.update(overrides)
        return Order.objects.create(**fields)

    def post_event(self, body, header=None):
        return self.client.post(
            self.webhook_url,
            data=body,
            content_type="application/json",
            HTTP_PAYMONGO_SIGNATURE=header if header is not None else sign(body),
        )


@override_settings(
    PAYMONGO_SECRET_KEY=SECRET_KEY, PAYMONGO_WEBHOOK_SECRET=WEBHOOK_SECRET
)
class CheckoutTests(PaymentTestBase):
    def source_ok(self):
        return {
            "source_id": SOURCE_ID,
            "checkout_url": CHECKOUT_URL,
            "amount_centavos": 62800,
        }

    def test_requires_authentication(self):
        response = self.client.post(
            self.checkout_url, {"order_id": self.order.pk}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("payments.views.create_gcash_source")
    def test_creates_a_source_and_returns_the_checkout_url(self, source):
        source.return_value = self.source_ok()

        self.client.force_authenticate(self.cust_user)
        response = self.client.post(
            self.checkout_url, {"order_id": self.order.pk}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["checkout_url"], CHECKOUT_URL)
        self.assertEqual(response.data["amount"], "628.00")

        invoice = Invoice.objects.get(order=self.order)
        self.assertEqual(invoice.paymongo_payment_id, SOURCE_ID)
        self.assertEqual(invoice.paymongo_checkout_url, CHECKOUT_URL)
        self.assertIsNone(invoice.paid_at)
        self.assertEqual(invoice.amount_paid, Decimal("0"))

    @patch("payments.views.create_gcash_source")
    def test_checkout_does_not_move_the_order(self, source):
        source.return_value = self.source_ok()

        self.client.force_authenticate(self.cust_user)
        self.client.post(self.checkout_url, {"order_id": self.order.pk}, format="json")

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(self.order.payment_status, PaymentStatus.UNPAID)

    @patch("payments.views.create_gcash_source")
    def test_second_tap_reuses_the_same_source(self, source):
        source.return_value = self.source_ok()

        self.client.force_authenticate(self.cust_user)
        self.client.post(self.checkout_url, {"order_id": self.order.pk}, format="json")
        response = self.client.post(
            self.checkout_url, {"order_id": self.order.pk}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["reused"])
        self.assertEqual(source.call_count, 1)
        self.assertEqual(Invoice.objects.count(), 1)

    @patch("payments.views.create_gcash_source")
    def test_cod_order_rejected(self, source):
        cod = self.make_order(
            payment_type=PaymentType.COD, status=OrderStatus.PENDING
        )

        self.client.force_authenticate(self.cust_user)
        response = self.client.post(
            self.checkout_url, {"order_id": cod.pk}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        source.assert_not_called()

    @patch("payments.views.create_gcash_source")
    def test_order_not_awaiting_payment_rejected(self, source):
        self.order.status = OrderStatus.CONFIRMED
        self.order.save()

        self.client.force_authenticate(self.cust_user)
        response = self.client.post(
            self.checkout_url, {"order_id": self.order.pk}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        source.assert_not_called()

    def test_cannot_pay_for_someone_elses_order(self):
        self.client.force_authenticate(self.other_user)
        response = self.client.post(
            self.checkout_url, {"order_id": self.order.pk}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_missing_order_id_rejected(self):
        self.client.force_authenticate(self.cust_user)
        response = self.client.post(self.checkout_url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("payments.views.create_gcash_source", side_effect=PayMongoError("PayMongo is down."))
    def test_gateway_failure_returns_502_and_writes_nothing(self, _source):
        self.client.force_authenticate(self.cust_user)
        response = self.client.post(
            self.checkout_url, {"order_id": self.order.pk}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(Invoice.objects.count(), 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)


@override_settings(
    PAYMONGO_SECRET_KEY=SECRET_KEY, PAYMONGO_WEBHOOK_SECRET=WEBHOOK_SECRET
)
class WebhookAuthTests(PaymentTestBase):
    def test_unsigned_request_rejected(self):
        body = event_body("payment.paid", payment_id=PAYMENT_ID)
        response = self.post_event(body, header="")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_bad_signature_rejected(self):
        body = event_body("payment.paid", payment_id=PAYMENT_ID)
        response = self.post_event(body, header=sign(body, secret="whsk_wrong"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_tampered_amount_rejected(self):
        body = event_body("payment.paid", payment_id=PAYMENT_ID)
        header = sign(body)
        tampered = body.replace(b"paid", b"pa1d")
        response = self.post_event(tampered, header=header)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_no_jwt_required(self):
        """The signature is the authentication — no Authorization header."""
        Invoice.objects.create(
            order=self.order,
            invoice_number="INV-T001",
            paymongo_payment_id=SOURCE_ID,
        )
        body = event_body("payment.paid", payment_id=PAYMENT_ID)

        self.client.credentials()  # explicitly no auth
        response = self.post_event(body)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_logged_in_customer_gains_nothing(self):
        self.client.force_authenticate(self.cust_user)
        body = event_body("payment.paid", payment_id=PAYMENT_ID)
        response = self.post_event(body, header="t=1,te=deadbeef")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_malformed_json_with_valid_signature(self):
        body = b"{not json"
        response = self.post_event(body)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@override_settings(
    PAYMONGO_SECRET_KEY=SECRET_KEY, PAYMONGO_WEBHOOK_SECRET=WEBHOOK_SECRET
)
class WebhookSettlementTests(PaymentTestBase):
    def setUp(self):
        super().setUp()
        self.invoice = Invoice.objects.create(
            order=self.order,
            invoice_number="INV-T001",
            paymongo_payment_id=SOURCE_ID,
            paymongo_checkout_url=CHECKOUT_URL,
        )

    def test_payment_paid_settles_the_order(self):
        body = event_body("payment.paid", payment_id=PAYMENT_ID)
        response = self.post_event(body)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "paid")

        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)
        self.assertEqual(self.order.status, OrderStatus.PENDING)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.amount_paid, Decimal("628.00"))
        self.assertEqual(self.invoice.paymongo_payment_id, PAYMENT_ID)
        self.assertIsNotNone(self.invoice.paid_at)

    def test_settlement_writes_a_status_log(self):
        self.post_event(event_body("payment.paid", payment_id=PAYMENT_ID))

        log = OrderStatusLog.objects.filter(order=self.order).latest("changed_at")
        self.assertEqual(log.status, OrderStatus.PENDING)
        self.assertIsNone(log.changed_by)

    @patch("payments.views.create_payment_from_source")
    def test_source_chargeable_collects_then_settles(self, create_payment):
        create_payment.return_value = {"payment_id": PAYMENT_ID, "status": "paid"}

        response = self.post_event(event_body("source.chargeable"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        create_payment.assert_called_once()
        self.assertEqual(create_payment.call_args.args[0], SOURCE_ID)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING)
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)

    @patch(
        "payments.views.create_payment_from_source",
        side_effect=PayMongoError("declined"),
    )
    def test_failed_collection_leaves_the_order_unpaid_and_asks_for_a_retry(self, _p):
        response = self.post_event(event_body("source.chargeable"))

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(self.order.payment_status, PaymentStatus.UNPAID)
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.paid_at)

    def test_payment_failed_marks_the_order_failed(self):
        response = self.post_event(
            event_body("payment.failed", payment_id=PAYMENT_ID)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.FAILED)
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)

    def test_failure_after_success_does_not_unpay_the_order(self):
        self.post_event(event_body("payment.paid", payment_id=PAYMENT_ID))
        self.post_event(event_body("payment.failed", payment_id=PAYMENT_ID))

        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)

    def test_unhandled_event_acknowledged(self):
        response = self.post_event(event_body("source.expired"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "ignored")

    def test_unmatched_event_acknowledged(self):
        body = event_body("payment.paid", source_id="src_unknown", payment_id="pay_x")
        response = self.post_event(body)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "unmatched")


@override_settings(
    PAYMONGO_SECRET_KEY=SECRET_KEY, PAYMONGO_WEBHOOK_SECRET=WEBHOOK_SECRET
)
class WebhookIdempotencyTests(PaymentTestBase):
    """PayMongo retries. A duplicate must never produce a second invoice."""

    def setUp(self):
        super().setUp()
        self.invoice = Invoice.objects.create(
            order=self.order,
            invoice_number="INV-T001",
            paymongo_payment_id=SOURCE_ID,
            paymongo_checkout_url=CHECKOUT_URL,
        )

    def test_replaying_the_same_event_changes_nothing(self):
        body = event_body("payment.paid", payment_id=PAYMENT_ID)

        first = self.post_event(body)
        paid_at = Invoice.objects.get(order=self.order).paid_at

        responses = [self.post_event(body) for _ in range(4)]

        self.assertEqual(first.data["status"], "paid")
        for response in responses:
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data["status"], "already_paid")

        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(Invoice.objects.get(order=self.order).paid_at, paid_at)

    def test_duplicate_does_not_duplicate_the_status_log(self):
        body = event_body("payment.paid", payment_id=PAYMENT_ID)
        for _ in range(3):
            self.post_event(body)

        pending_logs = OrderStatusLog.objects.filter(
            order=self.order, status=OrderStatus.PENDING
        )
        self.assertEqual(pending_logs.count(), 1)

    @patch("payments.views.create_payment_from_source")
    def test_chargeable_then_paid_settles_once(self, create_payment):
        """Both events arrive in the real flow; only the first may act."""
        create_payment.return_value = {"payment_id": PAYMENT_ID, "status": "paid"}

        self.post_event(event_body("source.chargeable"))
        second = self.post_event(event_body("payment.paid", payment_id=PAYMENT_ID))

        self.assertEqual(second.data["status"], "already_paid")
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(create_payment.call_count, 1)

    @patch("payments.views.create_payment_from_source")
    def test_repeated_chargeable_collects_only_once(self, create_payment):
        create_payment.return_value = {"payment_id": PAYMENT_ID, "status": "paid"}

        responses = [self.post_event(event_body("source.chargeable")) for _ in range(3)]

        self.assertEqual(create_payment.call_count, 1)
        self.assertEqual(Invoice.objects.count(), 1)

        # The retries must land on the explicit idempotency branch, not fall
        # through as "unmatched" — that would be safe only by accident.
        self.assertEqual(responses[0].data["status"], "paid")
        self.assertEqual(responses[1].data["status"], "already_paid")
        self.assertEqual(responses[2].data["status"], "already_paid")

    @patch("payments.views.create_payment_from_source")
    def test_settled_order_is_still_matchable_by_its_source_id(self, create_payment):
        """Settlement overwrites the source id with the payment id."""
        create_payment.return_value = {"payment_id": PAYMENT_ID, "status": "paid"}
        self.invoice.paymongo_checkout_url = CHECKOUT_URL
        self.invoice.save()

        self.post_event(event_body("source.chargeable"))

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.paymongo_payment_id, PAYMENT_ID)

        response = self.post_event(event_body("source.chargeable"))
        self.assertEqual(response.data["status"], "already_paid")
        self.assertEqual(response.data["order_number"], self.order.order_number)

    def test_amount_is_not_double_counted(self):
        body = event_body("payment.paid", payment_id=PAYMENT_ID)
        for _ in range(5):
            self.post_event(body)

        self.assertEqual(
            Invoice.objects.get(order=self.order).amount_paid, Decimal("628.00")
        )

    def test_one_invoice_per_order_is_enforced_by_the_database(self):
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Invoice.objects.create(
                    order=self.order, invoice_number="INV-T002"
                )


@override_settings(
    PAYMONGO_SECRET_KEY=SECRET_KEY, PAYMONGO_WEBHOOK_SECRET=WEBHOOK_SECRET
)
class CodInvoiceTests(PaymentTestBase):
    """COD settles at the door, written by the rider completing the delivery."""

    def setUp(self):
        super().setUp()
        self.rider_user = User.objects.create_user(
            username="rico",
            email="rico@example.com",
            password="Kidapawan!2026",
            is_rider=True,
            is_customer=False,
        )
        self.rider = Rider.objects.create(user=self.rider_user, is_available=True)

        self.cod_order = self.make_order(
            payment_type=PaymentType.COD, status=OrderStatus.READY
        )
        self.delivery = Delivery.objects.create(
            order=self.cod_order, rider=self.rider, rider_earnings=Decimal("30.00")
        )

    def advance(self, *values):
        self.client.force_authenticate(self.rider_user)
        for value in values:
            response = self.client.patch(
                reverse("delivery:rider-delivery-status", args=[self.delivery.pk]),
                {"status": value},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, value)

    def test_invoice_written_on_completion(self):
        self.advance("picked_up", "in_transit", "delivered")

        invoice = Invoice.objects.get(order=self.cod_order)
        self.assertEqual(invoice.amount_paid, Decimal("628.00"))
        self.assertIsNotNone(invoice.paid_at)
        self.assertTrue(invoice.invoice_number.startswith("INV-"))

    def test_cod_invoice_has_no_paymongo_reference(self):
        self.advance("picked_up", "in_transit", "delivered")

        invoice = Invoice.objects.get(order=self.cod_order)
        self.assertEqual(invoice.paymongo_payment_id, "")
        self.assertEqual(invoice.paymongo_checkout_url, "")

    def test_order_is_marked_paid(self):
        self.advance("picked_up", "in_transit", "delivered")

        self.cod_order.refresh_from_db()
        self.assertEqual(self.cod_order.payment_status, PaymentStatus.PAID)
        self.assertEqual(self.cod_order.status, OrderStatus.DELIVERED)

    def test_completion_does_not_reopen_the_order_to_pending(self):
        """settle_payment only opens the gate for pending_payment orders."""
        self.advance("picked_up", "in_transit", "delivered")

        self.cod_order.refresh_from_db()
        self.assertEqual(self.cod_order.status, OrderStatus.DELIVERED)

    def test_no_invoice_before_delivery(self):
        self.advance("picked_up", "in_transit")
        self.assertFalse(Invoice.objects.filter(order=self.cod_order).exists())

    def test_invoice_numbers_are_unique_across_orders(self):
        self.advance("picked_up", "in_transit", "delivered")

        second = self.make_order(
            payment_type=PaymentType.COD, status=OrderStatus.READY
        )
        delivery = Delivery.objects.create(order=second, rider=self.rider)
        self.client.force_authenticate(self.rider_user)
        for value in ("picked_up", "in_transit", "delivered"):
            self.client.patch(
                reverse("delivery:rider-delivery-status", args=[delivery.pk]),
                {"status": value},
                format="json",
            )

        numbers = list(Invoice.objects.values_list("invoice_number", flat=True))
        self.assertEqual(len(numbers), len(set(numbers)))


@override_settings(
    PAYMONGO_SECRET_KEY=SECRET_KEY,
    PAYMONGO_SUCCESS_REDIRECT_URL="https://example.test/paid",
    PAYMONGO_FAILED_REDIRECT_URL="https://example.test/failed",
)
class GatewayTests(APITestCase):
    class FakeOrder:
        total = Decimal("628.00")
        order_number = "YR-0001"

    def source_response(self, status_code=200, body=None):
        class FakeResponse:
            def __init__(self):
                self.status_code = status_code
                self.text = json.dumps(body or {})

            def json(self):
                if body is None:
                    raise ValueError("no json")
                return body

        return FakeResponse()

    @patch("payments.gateway.requests.post")
    def test_source_request_shape(self, post):
        post.return_value = self.source_response(
            body={
                "data": {
                    "id": SOURCE_ID,
                    "attributes": {"redirect": {"checkout_url": CHECKOUT_URL}},
                }
            }
        )

        from payments.gateway import create_gcash_source

        result = create_gcash_source(self.FakeOrder())

        self.assertEqual(result["source_id"], SOURCE_ID)
        self.assertEqual(result["checkout_url"], CHECKOUT_URL)

        attributes = post.call_args.kwargs["json"]["data"]["attributes"]
        self.assertEqual(attributes["amount"], 62800)
        self.assertEqual(attributes["currency"], "PHP")
        self.assertEqual(attributes["type"], "gcash")

    @patch("payments.gateway.requests.post")
    def test_secret_key_is_sent_as_basic_auth_and_never_logged(self, post):
        post.return_value = self.source_response(
            body={
                "data": {
                    "id": SOURCE_ID,
                    "attributes": {"redirect": {"checkout_url": CHECKOUT_URL}},
                }
            }
        )

        from payments.gateway import create_gcash_source

        create_gcash_source(self.FakeOrder())
        header = post.call_args.kwargs["headers"]["Authorization"]
        self.assertTrue(header.startswith("Basic "))
        self.assertNotIn(SECRET_KEY, header)  # base64, not plaintext

    @patch("payments.gateway.requests.post", side_effect=requests.Timeout())
    def test_timeout_becomes_a_paymongo_error(self, _post):
        from payments.gateway import create_gcash_source

        with self.assertRaises(PayMongoError):
            create_gcash_source(self.FakeOrder())

    @patch("payments.gateway.requests.post")
    def test_api_error_becomes_a_paymongo_error(self, post):
        post.return_value = self.source_response(
            status_code=400, body={"errors": [{"detail": "amount is invalid"}]}
        )
        from payments.gateway import create_gcash_source

        with self.assertRaises(PayMongoError) as caught:
            create_gcash_source(self.FakeOrder())
        self.assertIn("amount is invalid", str(caught.exception))

    @override_settings(PAYMONGO_SECRET_KEY="")
    def test_unconfigured_key_raises_rather_than_calling_out(self):
        from payments.gateway import create_gcash_source

        with self.assertRaises(PayMongoError):
            create_gcash_source(self.FakeOrder())

    @override_settings(
        PAYMONGO_SUCCESS_REDIRECT_URL="yellowribbon://payment/success",
        PAYMONGO_FAILED_REDIRECT_URL="yellowribbon://payment/failed",
    )
    @patch("payments.gateway.requests.post")
    def test_custom_app_scheme_redirects_are_refused_before_calling_out(self, post):
        """PayMongo answers 'redirect.failed format is invalid' for these."""
        from payments.gateway import create_gcash_source

        with self.assertRaises(PayMongoError) as caught:
            create_gcash_source(self.FakeOrder())

        self.assertIn("http(s) URL", str(caught.exception))
        post.assert_not_called()

    @override_settings(
        PAYMONGO_SUCCESS_REDIRECT_URL="", PAYMONGO_FAILED_REDIRECT_URL=""
    )
    @patch("payments.gateway.requests.post")
    def test_explicit_urls_are_used_when_settings_are_blank(self, post):
        post.return_value = self.source_response(
            body={
                "data": {
                    "id": SOURCE_ID,
                    "attributes": {"redirect": {"checkout_url": CHECKOUT_URL}},
                }
            }
        )
        from payments.gateway import create_gcash_source

        create_gcash_source(
            self.FakeOrder(),
            "https://example.test/ok",
            "https://example.test/no",
        )

        redirect = post.call_args.kwargs["json"]["data"]["attributes"]["redirect"]
        self.assertEqual(redirect["success"], "https://example.test/ok")
        self.assertEqual(redirect["failed"], "https://example.test/no")

    @override_settings(
        PAYMONGO_SUCCESS_REDIRECT_URL="", PAYMONGO_FAILED_REDIRECT_URL=""
    )
    @patch("payments.gateway.requests.post")
    def test_no_redirect_at_all_is_refused(self, post):
        from payments.gateway import create_gcash_source

        with self.assertRaises(PayMongoError):
            create_gcash_source(self.FakeOrder())
        post.assert_not_called()


@override_settings(
    PAYMONGO_SECRET_KEY=SECRET_KEY,
    PAYMONGO_SUCCESS_REDIRECT_URL="",
    PAYMONGO_FAILED_REDIRECT_URL="",
)
class RedirectPageTests(PaymentTestBase):
    """The pages PayMongo bounces the customer back to."""

    @patch("payments.views.create_gcash_source")
    def test_checkout_derives_redirects_from_the_request(self, source):
        source.return_value = {
            "source_id": SOURCE_ID,
            "checkout_url": CHECKOUT_URL,
            "amount_centavos": 62800,
        }

        self.client.force_authenticate(self.cust_user)
        self.client.post(self.checkout_url, {"order_id": self.order.pk}, format="json")

        success, failed = source.call_args.args[1], source.call_args.args[2]
        self.assertTrue(success.startswith("http"))
        self.assertTrue(success.endswith("/api/payments/return/success/"))
        self.assertTrue(failed.endswith("/api/payments/return/failed/"))

    def test_success_page_renders_publicly(self):
        response = self.client.get(reverse("payments:return-success"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(b"Payment received", response.content)
        self.assertIn(b"yellowribbon://payment/success", response.content)

    def test_failed_page_renders_publicly(self):
        response = self.client.get(reverse("payments:return-failed"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(b"not completed", response.content)
