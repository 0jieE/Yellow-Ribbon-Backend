"""
Refunds for cancelled paid orders, the payments report, and the order `payment` block.

Every gateway call is mocked. Refunds run on `transaction.on_commit`, so tests
that cancel an order wrap the call in captureOnCommitCallbacks(execute=True),
which is exactly what happens in production once the cancel commits.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import transaction
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, User
from orders.admin_api import apply_status_change
from orders.constants import OrderStatus, PaymentStatus, PaymentType
from orders.tests_admin import make_order
from payments import gateway, maya
from payments.models import Invoice, Provider, RefundStatus
from payments.refunds import RefundError, process_refund, retry_refund, schedule_refund
from payments.services import issue_invoice, settle_payment

PASSWORD = "Kidapawan!2026"
MAYA = override_settings(
    MAYA_PUBLIC_KEY="pk-test", MAYA_SECRET_KEY="sk-test",
    MAYA_BASE_URL="https://pg-sandbox.paymaya.com", PAYMONGO_SECRET_KEY="sk_test_x",
)

REFUNDED = {"reference": "ref-1", "outcome": "refunded"}
PENDING = {"reference": "ref-1", "outcome": "pending"}


def maya_state(can_void=True, can_refund=False, status_text="PAYMENT_SUCCESS"):
    return {
        "id": "pay-1", "status": status_text, "state": "paid", "amount": Decimal("329.00"),
        "can_void": can_void, "can_refund": can_refund, "created_at": timezone.now(),
        "payer": "", "raw": {},
    }


class RefundBase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("maria", "maria@example.com", PASSWORD)
        self.customer = Customer.objects.create(user=self.user)
        self.admin = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)

    def paid_order(self, provider="maya", **overrides):
        """An order that was paid online and is now in the kitchen (status pending)."""
        payment_type = {"maya": PaymentType.MAYA, "paymongo": PaymentType.GCASH}[provider]
        order = make_order(self.customer, payment_type=payment_type,
                           status=OrderStatus.PENDING_PAYMENT, **overrides)
        if provider == "maya":
            issue_invoice(order, source_id="co-1", checkout_url="https://m/1", rrn="rrn-1")
            settle_payment(order, amount=order.total, payment_id="pay-1", payer="Maya wallet ••••0900")
        else:
            issue_invoice(order, source_id="src_1", checkout_url="https://g/1")
            settle_payment(order, amount=order.total, payment_id="pay_1")
        order.refresh_from_db()
        return order

    def cancel_as_admin(self, order, reason="Out of dough"):
        self.client.force_authenticate(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.patch(
                reverse("admin-order-status", args=[order.pk]),
                {"status": "cancelled", "cancel_reason": reason},
                format="json",
            )


@MAYA
class MayaRefundTests(RefundBase):
    @patch("payments.refunds.notify_refund")
    @patch("payments.maya.void_payment", return_value=REFUNDED)
    @patch("payments.maya.retrieve_payment", return_value=maya_state(can_void=True))
    def test_cancelling_a_paid_maya_order_voids_it_the_same_day(self, _lookup, void, notify):
        order = self.paid_order()

        response = self.cancel_as_admin(order)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        void.assert_called_once()
        self.assertEqual(void.call_args[0][0], "rrn-1")
        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.refund_status, RefundStatus.REFUNDED)
        self.assertEqual(invoice.refund_amount, Decimal("329.00"))
        self.assertEqual(invoice.refund_reference, "ref-1")
        self.assertIsNotNone(invoice.refunded_at)
        self.assertEqual(invoice.refund_error, "")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        notify.assert_called_once()

    @patch("payments.maya.refund_payment", return_value=REFUNDED)
    @patch("payments.maya.void_payment")
    @patch("payments.maya.retrieve_payment", return_value=maya_state(can_void=False, can_refund=True))
    def test_an_older_payment_is_refunded_not_voided(self, _lookup, void, refund):
        order = self.paid_order()
        self.cancel_as_admin(order)

        void.assert_not_called()
        refund.assert_called_once()
        self.assertEqual(refund.call_args[0][1], Decimal("329.00"))
        self.assertEqual(Invoice.objects.get(order=order).refund_status, RefundStatus.REFUNDED)

    @patch("payments.refunds.notify_admins")
    @patch("payments.maya.retrieve_payment", return_value=maya_state(can_void=False, can_refund=False))
    def test_a_payment_maya_will_not_reverse_yet_is_a_recorded_failure(self, _lookup, notify_admins):
        order = self.paid_order()
        self.cancel_as_admin(order)

        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.refund_status, RefundStatus.FAILED)
        self.assertIn("cannot void or refund", invoice.refund_error)
        notify_admins.assert_called_once()

    @patch("payments.refunds.notify_refund")
    @patch("payments.maya.refund_payment", return_value=PENDING)
    @patch("payments.maya.retrieve_payment", return_value=maya_state(can_void=False, can_refund=True))
    def test_a_pending_refund_keeps_payment_status_paid_until_confirmed(self, _lookup, _refund, notify):
        order = self.paid_order()
        self.cancel_as_admin(order)

        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.refund_status, RefundStatus.PENDING)
        self.assertEqual(invoice.refund_reference, "ref-1")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        notify.assert_called_once()  # "refund on the way"


@MAYA
class PayMongoRefundTests(RefundBase):
    @patch("payments.refunds.notify_refund")
    @patch("payments.gateway.create_refund", return_value=REFUNDED)
    def test_cancelling_a_paid_gcash_order_refunds_the_payment(self, create, notify):
        order = self.paid_order("paymongo")

        self.cancel_as_admin(order)

        create.assert_called_once()
        self.assertEqual(create.call_args[0][0], "pay_1")
        self.assertEqual(create.call_args[0][1], Decimal("329.00"))
        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.refund_status, RefundStatus.REFUNDED)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        notify.assert_called_once()

    @patch("payments.gateway.create_refund")
    def test_an_invoice_with_only_a_source_id_cannot_be_refunded_yet(self, create):
        order = make_order(self.customer, payment_type=PaymentType.GCASH, status=OrderStatus.PENDING)
        invoice, _ = issue_invoice(order, source_id="src_1", checkout_url="https://g/1")
        Invoice.objects.filter(pk=invoice.pk).update(paid_at=timezone.now(), provider="paymongo",
                                                     amount_paid=order.total)

        self.cancel_as_admin(order)

        create.assert_not_called()
        invoice.refresh_from_db()
        self.assertEqual(invoice.refund_status, RefundStatus.FAILED)
        self.assertIn("no PayMongo payment id", invoice.refund_error)

    @patch("payments.gateway.retrieve_refund", return_value=REFUNDED)
    @patch("payments.gateway.create_refund", return_value=PENDING)
    def test_a_pending_gcash_refund_is_confirmed_by_a_retry(self, create, retrieve):
        order = self.paid_order("paymongo")
        self.cancel_as_admin(order)
        self.assertEqual(Invoice.objects.get(order=order).refund_status, RefundStatus.PENDING)

        order.refresh_from_db()
        invoice = retry_refund(order)

        self.assertEqual(invoice.refund_status, RefundStatus.REFUNDED)
        create.assert_called_once()  # not asked twice
        retrieve.assert_called_once_with("ref-1")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)


@MAYA
class RefundSafetyTests(RefundBase):
    @patch("payments.refunds.notify_admins")
    @patch("payments.gateway.create_refund", side_effect=gateway.PayMongoError("PayMongo did not respond."))
    def test_a_failed_refund_never_undoes_the_cancel(self, _create, notify_admins):
        order = self.paid_order("paymongo")

        response = self.cancel_as_admin(order)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertEqual(order.payment_status, PaymentStatus.PAID)
        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.refund_status, RefundStatus.FAILED)
        self.assertEqual(invoice.refund_error, "PayMongo did not respond.")
        notify_admins.assert_called_once()
        self.assertIn(order.order_number, notify_admins.call_args[0][1])

    @patch("payments.refunds.notify_admins")
    @patch("payments.gateway.create_refund", side_effect=RuntimeError("boom"))
    def test_even_a_bug_in_the_refund_is_recorded_not_raised(self, _create, _notify):
        order = self.paid_order("paymongo")
        self.cancel_as_admin(order)
        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.refund_status, RefundStatus.FAILED)
        self.assertIn("boom", invoice.refund_error)

    @patch("payments.gateway.create_refund", return_value=REFUNDED)
    def test_no_refund_is_made_if_the_cancel_rolls_back(self, create):
        order = self.paid_order("paymongo")

        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    apply_status_change(order, OrderStatus.CANCELLED, changed_by=self.admin)
                    raise RuntimeError("something later in the request failed")
            except RuntimeError:
                pass

        create.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING)
        self.assertEqual(Invoice.objects.get(order=order).refund_status, RefundStatus.NONE)

    @patch("payments.gateway.create_refund", return_value=REFUNDED)
    def test_the_gateway_is_not_called_inside_the_cancel_transaction(self, create):
        order = self.paid_order("paymongo")
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            apply_status_change(order, OrderStatus.CANCELLED, changed_by=self.admin)
        create.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(Invoice.objects.get(order=order).refund_status, RefundStatus.PENDING)

    @patch("payments.gateway.create_refund", return_value=REFUNDED)
    def test_a_refund_is_never_made_twice(self, create):
        order = self.paid_order("paymongo")
        self.cancel_as_admin(order)
        invoice = Invoice.objects.get(order=order)

        process_refund(invoice.pk)
        self.assertFalse(schedule_refund(order))

        create.assert_called_once()

    @patch("payments.gateway.create_refund")
    def test_unpaid_orders_have_nothing_to_refund(self, create):
        order = make_order(self.customer, payment_type=PaymentType.GCASH, status=OrderStatus.PENDING_PAYMENT)
        issue_invoice(order, source_id="src_1", checkout_url="https://g/1")

        self.cancel_as_admin(order)

        create.assert_not_called()
        self.assertEqual(Invoice.objects.get(order=order).refund_status, RefundStatus.NONE)

    @patch("payments.gateway.create_refund")
    @patch("payments.maya.void_payment")
    def test_cod_orders_are_never_refunded_online(self, void, create):
        order = make_order(self.customer, payment_type=PaymentType.COD, status=OrderStatus.PENDING)
        self.cancel_as_admin(order)
        void.assert_not_called()
        create.assert_not_called()
        self.assertFalse(Invoice.objects.exists())

    @patch("payments.gateway.create_refund", return_value=REFUNDED)
    def test_the_customer_cancelling_a_paid_order_refunds_it_too(self, create):
        order = self.paid_order("paymongo")
        self.client.force_authenticate(self.user)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("orders:order-cancel", args=[order.pk]), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        create.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.REFUNDED)
        self.assertEqual(response.data["payment"]["refund"]["status"], "pending")

    @patch("payments.gateway.create_refund", return_value=REFUNDED)
    def test_the_stale_order_reaper_refunds_nothing_for_unpaid_orders(self, create):
        order = make_order(self.customer, payment_type=PaymentType.GCASH, status=OrderStatus.PENDING_PAYMENT)
        from orders.models import Order

        Order.objects.filter(pk=order.pk).update(placed_at=timezone.now() - timedelta(hours=2))
        from django.core.management import call_command
        from io import StringIO

        with self.captureOnCommitCallbacks(execute=True):
            call_command("cancel_stale_orders", "--minutes", "30", stdout=StringIO())
        create.assert_not_called()


@MAYA
class RetryRefundTests(RefundBase):
    def url(self, order):
        return reverse("admin-order-refund", args=[order.pk])

    def failed_order(self):
        order = self.paid_order("paymongo")
        with patch("payments.gateway.create_refund", side_effect=gateway.PayMongoError("down")):
            with patch("payments.refunds.notify_admins"):
                self.cancel_as_admin(order)
        self.assertEqual(Invoice.objects.get(order=order).refund_status, RefundStatus.FAILED)
        return order

    @patch("payments.refunds.notify_refund")
    @patch("payments.gateway.create_refund", return_value=REFUNDED)
    def test_the_admin_can_retry_a_failed_refund(self, create, _notify):
        order = self.failed_order()
        self.client.force_authenticate(self.admin)

        response = self.client.post(self.url(order))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["payment"]["refund"]["status"], "refunded")
        self.assertEqual(response.data["payment_status"], "refunded")
        create.assert_called_once()
        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.refund_error, "")

    @patch("payments.refunds.notify_admins")
    @patch("payments.gateway.create_refund", side_effect=gateway.PayMongoError("still down"))
    def test_a_retry_that_fails_again_says_why(self, _create, _notify):
        order = self.failed_order()
        self.client.force_authenticate(self.admin)

        response = self.client.post(self.url(order))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["payment"]["refund"]["status"], "failed")
        self.assertEqual(response.data["payment"]["refund"]["error"], "still down")

    @patch("payments.gateway.create_refund", return_value=REFUNDED)
    def test_retry_starts_a_new_refund_when_the_gateway_called_the_old_one_failed(self, create):
        order = self.paid_order("paymongo")
        Invoice.objects.filter(order=order).update(refund_status="failed", refund_reference="ref-dead",
                                                   refund_amount=order.total)
        order.status = OrderStatus.CANCELLED
        order.save()

        retry_refund(order)

        create.assert_called_once()

    def test_an_already_refunded_order_is_refused(self):
        order = self.paid_order("paymongo")
        Invoice.objects.filter(order=order).update(refund_status="refunded")
        order.status = OrderStatus.CANCELLED
        order.save()
        self.client.force_authenticate(self.admin)
        response = self.client.post(self.url(order))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already been refunded", response.data["error"])

    def test_an_order_that_is_not_cancelled_is_refused(self):
        order = self.paid_order("paymongo")
        self.client.force_authenticate(self.admin)
        response = self.client.post(self.url(order))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Cancel it first", response.data["error"])

    def test_an_unpaid_order_is_refused(self):
        order = make_order(self.customer, payment_type=PaymentType.GCASH, status=OrderStatus.CANCELLED)
        self.client.force_authenticate(self.admin)
        response = self.client.post(self.url(order))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("never paid", response.data["error"])

    def test_cod_is_refused(self):
        order = make_order(self.customer, payment_type=PaymentType.COD, status=OrderStatus.CANCELLED)
        Invoice.objects.create(order=order, invoice_number="INV-9001", provider="cod",
                               amount_paid=order.total, paid_at=timezone.now())
        self.client.force_authenticate(self.admin)
        response = self.client.post(self.url(order))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Cash on delivery", response.data["error"])

    def test_unknown_order_is_a_404(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(reverse("admin-order-refund", args=[999999])).status_code,
                         status.HTTP_404_NOT_FOUND)

    def test_only_admins_may_retry(self):
        order = self.failed_order()
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post(self.url(order)).status_code, status.HTTP_401_UNAUTHORIZED)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post(self.url(order)).status_code, status.HTTP_403_FORBIDDEN)

    def test_retry_refund_function_raises_refund_error(self):
        order = self.paid_order("paymongo")
        with self.assertRaises(RefundError):
            retry_refund(order)  # not cancelled

    @patch("payments.refunds.notify_refund")
    @patch("payments.gateway.create_refund", return_value=REFUNDED)
    def test_the_django_admin_action_retries_too(self, create, _notify):
        order = self.failed_order()
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("admin:orders_order_changelist"),
            {"action": "retry_refunds", "_selected_action": [order.pk], "index": "0"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Invoice.objects.get(order=order).refund_status, RefundStatus.REFUNDED)


@MAYA
class PaymentBlockTests(RefundBase):
    def get(self, order, admin=False):
        self.client.force_authenticate(self.admin if admin else self.user)
        name = "admin-order-detail" if admin else "orders:order-detail"
        return self.client.get(reverse(name, args=[order.pk])).data["payment"]

    def test_no_invoice_means_no_payment_block(self):
        order = make_order(self.customer, payment_type=PaymentType.COD)
        self.assertIsNone(self.get(order))
        self.assertIsNone(self.get(order, admin=True))

    def test_customer_block_for_a_paid_maya_order(self):
        order = self.paid_order()
        block = self.get(order)
        self.assertEqual(
            sorted(block),
            sorted(["invoice_number", "paymongo_reference", "amount_paid", "paid_at", "test_mode",
                    "provider", "payer", "refund"]),
        )
        self.assertEqual(block["provider"], "maya")
        self.assertEqual(block["payer"], "Maya wallet ••••0900")
        self.assertEqual(block["paymongo_reference"], "pay-1")
        self.assertEqual(block["amount_paid"], "329.00")
        self.assertTrue(block["test_mode"])
        self.assertIsNone(block["refund"])

    def test_the_stores_fee_and_net_are_not_shown_to_the_customer(self):
        block = self.get(self.paid_order())
        self.assertNotIn("fee", block)
        self.assertNotIn("net_amount", block)

    def test_admin_block_adds_fee_and_net(self):
        block = self.get(self.paid_order(), admin=True)
        self.assertEqual(block["fee"], "4.94")
        self.assertEqual(block["net_amount"], "324.06")
        self.assertEqual(block["provider"], "maya")

    def test_gcash_block(self):
        block = self.get(self.paid_order("paymongo"), admin=True)
        self.assertEqual(block["provider"], "paymongo")
        self.assertIsNone(block["payer"])
        self.assertEqual(block["paymongo_reference"], "pay_1")
        self.assertEqual(block["fee"], "8.23")  # 2.5% of 329.00

    def test_unpaid_checkout_block(self):
        order = make_order(self.customer, payment_type=PaymentType.MAYA, status=OrderStatus.PENDING_PAYMENT)
        issue_invoice(order, source_id="co-1", checkout_url="https://m/1", rrn="r")
        block = self.get(order)
        self.assertIsNone(block["paid_at"])
        self.assertEqual(block["amount_paid"], "0.00")
        self.assertEqual(block["provider"], "maya")

    @patch("payments.refunds.notify_refund")
    @patch("payments.maya.void_payment", return_value=REFUNDED)
    @patch("payments.maya.retrieve_payment", return_value=maya_state())
    def test_refund_block_after_a_refund(self, _lookup, _void, _notify):
        order = self.paid_order()
        self.cancel_as_admin(order)

        customer_view = self.get(order)["refund"]
        admin_view = self.get(order, admin=True)["refund"]

        self.assertEqual(sorted(customer_view), ["amount", "reference", "refunded_at", "status"])
        self.assertEqual(customer_view["status"], "refunded")
        self.assertEqual(customer_view["amount"], "329.00")
        self.assertEqual(customer_view["reference"], "ref-1")
        self.assertIsNotNone(customer_view["refunded_at"])
        self.assertIn("error", admin_view)

    def test_a_failed_refunds_reason_is_for_the_admin_only(self):
        order = self.paid_order("paymongo")
        Invoice.objects.filter(order=order).update(refund_status="failed", refund_error="Card network said no",
                                                   refund_amount=order.total)
        self.assertNotIn("error", self.get(order)["refund"])
        self.assertEqual(self.get(order, admin=True)["refund"]["error"], "Card network said no")

    def test_the_order_list_carries_the_block_without_extra_queries(self):
        for _ in range(4):
            self.paid_order("paymongo")
        self.client.force_authenticate(self.user)
        # One query per relation however many orders: orders (with customer,
        # delivery, rider and invoice joined), items, status logs.
        with self.assertNumQueries(3):
            self.client.get(reverse("orders:order-list"))


@MAYA
class PaymentsReportTests(RefundBase):
    url = reverse("admin-report-payments")

    def settle_at(self, order, when):
        Invoice.objects.filter(order=order).update(paid_at=when)

    def test_admins_only(self):
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)

    def test_empty_report_has_every_provider_with_zeros(self):
        self.client.force_authenticate(self.admin)
        data = self.client.get(self.url).data
        self.assertEqual([p["provider"] for p in data["providers"]], ["paymongo", "maya", "cod"])
        for row in data["providers"]:
            self.assertEqual(
                sorted(row),
                sorted(["provider", "label", "count", "gross", "fees", "refund_count", "refunds", "net"]),
            )
            self.assertEqual((row["count"], row["gross"], row["fees"], row["refunds"], row["net"]),
                             (0, "0.00", "0.00", "0.00", "0.00"))
        self.assertEqual(data["totals"]["net"], "0.00")
        self.assertEqual(data["refunds_outstanding"], {"count": 0, "amount": "0.00"})

    def test_per_provider_gross_fees_and_net(self):
        self.paid_order("maya")
        self.paid_order("maya")
        self.paid_order("paymongo")
        cod = make_order(self.customer, payment_type=PaymentType.COD, status=OrderStatus.DELIVERED)
        settle_payment(cod)
        self.client.force_authenticate(self.admin)

        data = self.client.get(self.url).data
        rows = {p["provider"]: p for p in data["providers"]}

        self.assertEqual((rows["maya"]["count"], rows["maya"]["gross"], rows["maya"]["fees"], rows["maya"]["net"]),
                         (2, "658.00", "9.88", "648.12"))
        self.assertEqual((rows["paymongo"]["count"], rows["paymongo"]["gross"], rows["paymongo"]["fees"], rows["paymongo"]["net"]),
                         (1, "329.00", "8.23", "320.77"))
        self.assertEqual((rows["cod"]["count"], rows["cod"]["gross"], rows["cod"]["fees"], rows["cod"]["net"]),
                         (1, "329.00", "0.00", "329.00"))
        self.assertEqual(data["totals"], {"count": 4, "gross": "1316.00", "fees": "18.11",
                                          "refund_count": 0, "refunds": "0.00", "net": "1297.89"})

    def test_refunds_come_off_the_net(self):
        order = self.paid_order("maya")
        with patch("payments.refunds.notify_refund"), patch(
            "payments.maya.retrieve_payment", return_value=maya_state()
        ), patch("payments.maya.void_payment", return_value=REFUNDED):
            self.cancel_as_admin(order)
        self.client.force_authenticate(self.admin)

        row = {p["provider"]: p for p in self.client.get(self.url).data["providers"]}["maya"]

        self.assertEqual((row["gross"], row["fees"], row["refund_count"], row["refunds"], row["net"]),
                         ("329.00", "4.94", 1, "329.00", "-4.94"))

    def test_date_range_filters_by_paid_date_and_refund_date(self):
        old = self.paid_order("maya")
        self.settle_at(old, timezone.now() - timedelta(days=40))
        self.paid_order("maya")
        self.client.force_authenticate(self.admin)
        today = timezone.localdate()

        last_week = self.client.get(self.url, {"start": (today - timedelta(days=7)).isoformat(),
                                               "end": today.isoformat()}).data
        everything = self.client.get(self.url, {"start": (today - timedelta(days=60)).isoformat(),
                                                "end": today.isoformat()}).data

        self.assertEqual(last_week["totals"]["count"], 1)
        self.assertEqual(everything["totals"]["count"], 2)
        self.assertEqual(last_week["start"], (today - timedelta(days=7)).isoformat())

    def test_outstanding_refunds_are_counted_whatever_their_date(self):
        order = self.paid_order("paymongo")
        Invoice.objects.filter(order=order).update(refund_status="failed", refund_amount=order.total)
        failed_too = self.paid_order("maya")
        Invoice.objects.filter(order=failed_too).update(refund_status="pending", refund_amount=failed_too.total)
        self.client.force_authenticate(self.admin)

        data = self.client.get(self.url, {"start": "2020-01-01", "end": "2020-01-02"}).data

        self.assertEqual(data["refunds_outstanding"], {"count": 2, "amount": "658.00"})

    def test_bad_dates_are_a_400(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(self.url, {"start": "soon"}).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.get(self.url, {"start": "2026-02-01", "end": "2026-01-01"}).status_code,
                         status.HTTP_400_BAD_REQUEST)


@MAYA
class PaymentTransactionsTests(RefundBase):
    url = reverse("admin-report-payment-transactions")

    def test_admins_only(self):
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)

    def test_row_shape_and_newest_first(self):
        first = self.paid_order("maya")
        Invoice.objects.filter(order=first).update(paid_at=timezone.now() - timedelta(hours=2))
        second = self.paid_order("paymongo")
        self.client.force_authenticate(self.admin)

        data = self.client.get(self.url).data

        self.assertEqual(sorted(data), ["count", "next", "previous", "results"])
        self.assertEqual(data["count"], 2)
        self.assertEqual([r["order_number"] for r in data["results"]],
                         [second.order_number, first.order_number])
        row = data["results"][1]
        self.assertEqual(
            sorted(row),
            sorted(["order_id", "order_number", "customer", "invoice_number", "provider", "reference",
                    "payer", "amount", "fee", "net", "paid_at", "refund"]),
        )
        self.assertEqual(row["provider"], "maya")
        self.assertEqual(row["reference"], "pay-1")
        self.assertEqual(row["payer"], "Maya wallet ••••0900")
        self.assertEqual((row["amount"], row["fee"], row["net"]), ("329.00", "4.94", "324.06"))
        self.assertIsNone(row["refund"])

    def test_pagination(self):
        for _ in range(3):
            self.paid_order("maya")
        self.client.force_authenticate(self.admin)

        page = self.client.get(self.url, {"page_size": 2}).data
        self.assertEqual((page["count"], len(page["results"])), (3, 2))
        self.assertIsNotNone(page["next"])
        self.assertEqual(len(self.client.get(self.url, {"page_size": 2, "page": 2}).data["results"]), 1)

    def test_provider_filter(self):
        self.paid_order("maya")
        self.paid_order("paymongo")
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(self.url, {"provider": "maya"}).data["count"], 1)
        self.assertEqual(self.client.get(self.url, {"provider": "paymongo"}).data["count"], 1)
        self.assertEqual(self.client.get(self.url, {"provider": "cod"}).data["count"], 0)

    def test_refund_filter(self):
        refunded = self.paid_order("maya")
        Invoice.objects.filter(order=refunded).update(refund_status="refunded", refund_amount=refunded.total)
        self.paid_order("maya")
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(self.url, {"refund": "refunded"}).data["count"], 1)
        self.assertEqual(self.client.get(self.url, {"refund": "none"}).data["count"], 1)
        self.assertEqual(self.client.get(self.url, {"refund": "failed"}).data["count"], 0)

    def test_unknown_filters_are_a_400(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(self.url, {"provider": "venmo"}).status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.get(self.url, {"refund": "maybe"}).status_code, status.HTTP_400_BAD_REQUEST)

    def test_unsettled_invoices_are_not_transactions(self):
        order = make_order(self.customer, payment_type=PaymentType.MAYA, status=OrderStatus.PENDING_PAYMENT)
        issue_invoice(order, source_id="co-1", checkout_url="https://m/1", rrn="r")
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(self.url).data["count"], 0)

    def test_the_list_does_not_query_per_row(self):
        for _ in range(5):
            self.paid_order("maya")
        self.client.force_authenticate(self.admin)
        # The count, and one joined query for the rows.
        with self.assertNumQueries(2):
            self.client.get(self.url)


class MigrationBackfillTests(APITestCase):
    def test_legacy_invoices_get_a_provider_and_a_net(self):
        from importlib import import_module

        from django.apps import apps

        user = User.objects.create_user("m", "m@example.com", PASSWORD)
        customer = Customer.objects.create(user=user)
        gcash = make_order(customer, payment_type=PaymentType.GCASH)
        cod = make_order(customer, payment_type=PaymentType.COD)
        a = Invoice.objects.create(order=gcash, invoice_number="INV-8001", amount_paid=Decimal("329.00"),
                                   paid_at=timezone.now())
        b = Invoice.objects.create(order=cod, invoice_number="INV-8002", amount_paid=Decimal("100.00"),
                                   paid_at=timezone.now())

        class Schema:  # the data migration never touches the schema editor
            pass

        import_module("payments.migrations.0004_backfill_invoice_provider").backfill(apps, Schema())

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.provider, a.fee, a.net_amount), ("paymongo", Decimal("0.00"), Decimal("329.00")))
        self.assertEqual((b.provider, b.net_amount), ("cod", Decimal("100.00")))
