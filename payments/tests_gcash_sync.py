"""
GCash payments that settle without their webhook: the app's sync, the
reaper, and the lock shared with the webhook. Every PayMongo call is mocked.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from orders.constants import OrderStatus, PaymentStatus
from orders.models import Order
from payments.gateway import PayMongoError, find_payment_for_source
from payments.models import Invoice
from payments.services import issue_invoice

from .tests import (
    CHECKOUT_URL,
    PAYMENT_ID,
    SECRET_KEY,
    SOURCE_ID,
    WEBHOOK_SECRET,
    PaymentTestBase,
    event_body,
)

CENTAVOS = 62800  # the base order's 628.00 total


def source(state, amount=CENTAVOS):
    return {"status": state, "amount_centavos": amount}


COLLECTED = {"payment_id": PAYMENT_ID, "status": "paid"}


@override_settings(PAYMONGO_SECRET_KEY=SECRET_KEY, PAYMONGO_WEBHOOK_SECRET=WEBHOOK_SECRET)
class GcashSyncTests(PaymentTestBase):
    def setUp(self):
        super().setUp()
        issue_invoice(self.order, source_id=SOURCE_ID, checkout_url=CHECKOUT_URL)
        self.sync_url = reverse("payments:sync")

    def sync(self):
        self.client.force_authenticate(self.cust_user)
        return self.client.post(self.sync_url, {"order_id": self.order.pk}, format="json")

    def assert_paid(self, payment_id=PAYMENT_ID):
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)
        self.assertEqual(self.order.status, OrderStatus.PENDING)
        self.assertEqual(Invoice.objects.get(order=self.order).paymongo_payment_id, payment_id)

    @patch("payments.gateway.create_payment_from_source", return_value=COLLECTED)
    @patch("payments.gateway.retrieve_source", return_value=source("chargeable"))
    def test_a_chargeable_source_is_collected_and_settled(self, _source, collect):
        response = self.sync()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            (response.data["paid"], response.data["settled"], response.data["gateway_status"]),
            (True, True, "chargeable"),
        )
        self.assertEqual(response.data["order_status"], OrderStatus.PENDING)
        collect.assert_called_once()
        self.assertEqual(collect.call_args.args[0], SOURCE_ID)
        self.assert_paid()

    @patch("payments.gateway.create_payment_from_source", return_value=COLLECTED)
    @patch("payments.gateway.retrieve_source", return_value=source("chargeable"))
    def test_polling_again_collects_nothing_more(self, lookup, collect):
        self.sync()
        response = self.sync()
        self.assertTrue(response.data["paid"])
        self.assertFalse(response.data["settled"])
        collect.assert_called_once()
        lookup.assert_called_once()  # a paid order is not asked about again

    @patch("payments.gateway.create_payment_from_source", return_value=COLLECTED)
    @patch("payments.gateway.retrieve_source", return_value=source("chargeable"))
    def test_the_late_webhook_does_not_collect_twice(self, _source, collect):
        self.sync()
        response = self.post_event(event_body("source.chargeable"))
        self.assertEqual(response.data["status"], "already_paid")
        collect.assert_called_once()

    @patch("payments.gateway.create_payment_from_source", return_value=COLLECTED)
    @patch("payments.gateway.retrieve_source", return_value=source("chargeable"))
    def test_sync_after_the_webhook_collects_nothing(self, lookup, collect):
        self.post_event(event_body("source.chargeable"))
        response = self.sync()
        self.assertTrue(response.data["paid"])
        collect.assert_called_once()
        lookup.assert_not_called()

    @patch("payments.gateway.create_payment_from_source", side_effect=AssertionError("collected twice"))
    def test_the_lock_stops_a_second_collection_that_raced_the_first(self, collect):
        # The webhook settled between this sync reading the source and taking the lock.
        from payments import services

        services.settle_payment(self.order, payment_id="pay_from_webhook")
        _, changed = services.collect_gcash_source(self.order, SOURCE_ID)
        self.assertFalse(changed)
        collect.assert_not_called()

    @patch("payments.gateway.find_payment_for_source", return_value={"payment_id": "pay_found", "status": "paid", "amount_centavos": CENTAVOS})
    @patch("payments.gateway.retrieve_source", return_value=source("paid"))
    def test_an_already_collected_source_settles_with_its_payment(self, _source, find):
        response = self.sync()
        self.assertTrue(response.data["settled"])
        find.assert_called_once_with(SOURCE_ID)
        self.assert_paid("pay_found")

    @patch("payments.gateway.find_payment_for_source", return_value=None)
    @patch("payments.gateway.retrieve_source", return_value=source("consumed"))
    def test_a_collected_source_without_a_found_payment_still_settles(self, _source, _find):
        self.assertTrue(self.sync().data["settled"])
        self.assert_paid(SOURCE_ID)

    @patch("payments.gateway.retrieve_source")
    def test_a_dead_source_marks_the_payment_failed_but_leaves_the_order_open(self, lookup):
        for state in ("cancelled", "expired", "failed"):
            lookup.return_value = source(state)
            response = self.sync()
            self.assertFalse(response.data["paid"], state)
            self.assertEqual(response.data["gateway_status"], state)
            self.order.refresh_from_db()
            self.assertEqual(self.order.payment_status, PaymentStatus.FAILED)
            self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)

    @patch("payments.gateway.create_payment_from_source")
    @patch("payments.gateway.retrieve_source", return_value=source("pending"))
    def test_a_pending_source_changes_nothing(self, _source, collect):
        response = self.sync()
        self.assertEqual((response.data["paid"], response.data["gateway_status"]), (False, "pending"))
        collect.assert_not_called()
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PaymentStatus.UNPAID)

    @patch("payments.gateway.create_payment_from_source")
    @patch("payments.gateway.retrieve_source", return_value=source("chargeable", amount=100))
    def test_a_source_for_a_different_amount_is_never_collected(self, _source, collect):
        response = self.sync()
        self.assertEqual((response.data["paid"], response.data["gateway_status"]), (False, "amount_mismatch"))
        collect.assert_not_called()

    @patch("payments.gateway.retrieve_source", side_effect=PayMongoError("Could not reach PayMongo."))
    def test_paymongo_unreachable_is_a_502(self, _source):
        response = self.sync()
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    @patch("payments.gateway.create_payment_from_source", side_effect=PayMongoError("nope"))
    @patch("payments.gateway.retrieve_source", return_value=source("chargeable"))
    def test_a_failed_collection_writes_nothing(self, _source, _collect):
        self.assertEqual(self.sync().status_code, status.HTTP_502_BAD_GATEWAY)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
        self.assertIsNone(Invoice.objects.get(order=self.order).paid_at)

    @patch("payments.gateway.retrieve_source")
    def test_an_order_without_a_checkout_is_not_looked_up(self, lookup):
        other = self.make_order()
        self.client.force_authenticate(self.cust_user)
        response = self.client.post(self.sync_url, {"order_id": other.pk}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["paid"])
        lookup.assert_not_called()


@override_settings(PAYMONGO_SECRET_KEY=SECRET_KEY)
class GcashReaperTests(PaymentTestBase):
    def setUp(self):
        super().setUp()
        issue_invoice(self.order, source_id=SOURCE_ID, checkout_url=CHECKOUT_URL)
        Order.objects.filter(pk=self.order.pk).update(placed_at=timezone.now() - timedelta(minutes=45))

    def reap(self):
        out = StringIO()
        call_command("cancel_stale_orders", "--minutes", "30", stdout=out)
        self.order.refresh_from_db()
        return out.getvalue()

    @patch("payments.gateway.create_payment_from_source", return_value=COLLECTED)
    @patch("payments.gateway.retrieve_source", return_value=source("chargeable"))
    def test_a_paid_order_whose_webhook_never_came_is_settled_not_cancelled(self, _source, _collect):
        self.reap()
        self.assertEqual(self.order.status, OrderStatus.PENDING)
        self.assertEqual(self.order.payment_status, PaymentStatus.PAID)

    @patch("payments.gateway.retrieve_source", return_value=source("expired"))
    def test_an_unpaid_order_is_cancelled(self, _source):
        self.reap()
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)

    @patch("payments.gateway.retrieve_source", side_effect=PayMongoError("down"))
    def test_when_paymongo_cannot_be_asked_the_order_is_left_alone(self, _source):
        out = self.reap()
        self.assertEqual(self.order.status, OrderStatus.PENDING_PAYMENT)
        self.assertIn("could not check PayMongo", out)


@override_settings(PAYMONGO_SECRET_KEY=SECRET_KEY)
class FindPaymentTests(PaymentTestBase):
    def reply(self, body):
        response = MagicMock(status_code=200)
        response.json.return_value = body
        return patch("payments.gateway.requests.get", return_value=response)

    def test_finds_the_payment_made_from_the_source(self):
        body = {"data": [
            {"id": "pay_other", "attributes": {"status": "paid", "amount": 100, "source": {"id": "src_other"}}},
            {"id": "pay_mine", "attributes": {"status": "paid", "amount": CENTAVOS, "source": {"id": SOURCE_ID}}},
        ]}
        with self.reply(body) as get:
            found = find_payment_for_source(SOURCE_ID)
        self.assertEqual(found, {"payment_id": "pay_mine", "status": "paid", "amount_centavos": CENTAVOS})
        self.assertTrue(get.call_args.args[0].endswith("/payments?limit=100"))

    def test_none_when_no_payment_used_the_source(self):
        with self.reply({"data": []}):
            self.assertIsNone(find_payment_for_source(SOURCE_ID))
