"""
FCM and device-token tests.

Firebase is never contacted — the messaging module is mocked throughout.
"""

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, DeviceToken, Rider, User
from core import fcm
from core.fcm import notify_admins, notify_users, reset_app_cache
from delivery.models import Delivery
from menu.models import Menu
from orders.constants import DeliveryStatus, OrderStatus, PaymentType
from orders.models import Order, OrderStatusLog

CREDENTIALS = "/fake/service-account.json"


class FakeResult:
    def __init__(self, success=True, exception=None):
        self.success = success
        self.exception = exception


class FakeBatch:
    def __init__(self, responses):
        self.responses = responses
        self.success_count = sum(1 for r in responses if r.success)
        self.failure_count = len(responses) - self.success_count


class FcmTestBase(APITestCase):
    def setUp(self):
        reset_app_cache()
        self.addCleanup(reset_app_cache)

        self.cust_user = User.objects.create_user(
            username="maria", email="maria@example.com", password="Kidapawan!2026"
        )
        self.customer = Customer.objects.create(user=self.cust_user)
        self.admin = User.objects.create_superuser(
            username="boss", email="boss@example.com", password="Kidapawan!2026"
        )
        self.rider_user = User.objects.create_user(
            username="rico",
            email="rico@example.com",
            password="Kidapawan!2026",
            is_rider=True,
            is_customer=False,
        )
        self.rider = Rider.objects.create(user=self.rider_user, is_available=True)
        self.menu = Menu.objects.create(name="Pepperoni", price=Decimal("299.00"))

    def token(self, user, value, platform="android"):
        return DeviceToken.objects.create(
            user=user, fcm_token=value, platform=platform
        )

    def make_order(self, **overrides):
        fields = {
            "order_number": f"YR-N{Order.objects.count() + 1:04d}",
            "customer": self.customer,
            "status": OrderStatus.PENDING,
            "payment_type": PaymentType.COD,
            "subtotal": Decimal("598.00"),
            "delivery_fee": Decimal("30.00"),
            "total": Decimal("628.00"),
            "delivery_address": "Blue gate, Quezon Blvd",
            "delivery_latitude": Decimal("7.008300"),
            "delivery_longitude": Decimal("125.089400"),
        }
        fields.update(overrides)
        return Order.objects.create(**fields)


class DeviceTokenEndpointTests(FcmTestBase):
    url = reverse("device-tokens")

    def test_requires_authentication(self):
        response = self.client.post(
            self.url, {"fcm_token": "abc", "platform": "android"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_register_a_token(self):
        self.client.force_authenticate(self.cust_user)
        response = self.client.post(
            self.url, {"fcm_token": "tok-1", "platform": "android"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        token = DeviceToken.objects.get(fcm_token="tok-1")
        self.assertEqual(token.user, self.cust_user)
        self.assertEqual(token.platform, "android")

    def test_registering_twice_is_not_a_duplicate(self):
        self.client.force_authenticate(self.cust_user)
        for _ in range(3):
            self.client.post(
                self.url, {"fcm_token": "tok-1", "platform": "android"}, format="json"
            )
        self.assertEqual(DeviceToken.objects.filter(fcm_token="tok-1").count(), 1)

    def test_one_user_may_have_several_devices(self):
        self.client.force_authenticate(self.cust_user)
        self.client.post(self.url, {"fcm_token": "phone", "platform": "android"}, format="json")
        self.client.post(self.url, {"fcm_token": "tablet", "platform": "ios"}, format="json")

        self.assertEqual(DeviceToken.objects.filter(user=self.cust_user).count(), 2)

    def test_a_handset_reassigns_to_whoever_logs_in(self):
        """Same device, different user — the old owner must stop receiving."""
        self.client.force_authenticate(self.cust_user)
        self.client.post(self.url, {"fcm_token": "shared", "platform": "android"}, format="json")

        self.client.force_authenticate(self.rider_user)
        self.client.post(self.url, {"fcm_token": "shared", "platform": "android"}, format="json")

        self.assertEqual(DeviceToken.objects.filter(fcm_token="shared").count(), 1)
        self.assertEqual(DeviceToken.objects.get(fcm_token="shared").user, self.rider_user)

    def test_blank_token_rejected(self):
        self.client.force_authenticate(self.cust_user)
        response = self.client.post(
            self.url, {"fcm_token": "   "}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bad_platform_rejected(self):
        self.client.force_authenticate(self.cust_user)
        response = self.client.post(
            self.url, {"fcm_token": "tok", "platform": "symbian"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_one_token(self):
        self.token(self.cust_user, "phone")
        self.token(self.cust_user, "tablet")

        self.client.force_authenticate(self.cust_user)
        response = self.client.delete(self.url, {"fcm_token": "phone"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(DeviceToken.objects.filter(fcm_token="phone").exists())
        self.assertTrue(DeviceToken.objects.filter(fcm_token="tablet").exists())

    def test_delete_without_a_token_clears_the_account(self):
        self.token(self.cust_user, "phone")
        self.token(self.cust_user, "tablet")

        self.client.force_authenticate(self.cust_user)
        self.client.delete(self.url, {}, format="json")

        self.assertEqual(DeviceToken.objects.filter(user=self.cust_user).count(), 0)

    def test_cannot_delete_another_users_token(self):
        self.token(self.rider_user, "theirs")

        self.client.force_authenticate(self.cust_user)
        self.client.delete(self.url, {"fcm_token": "theirs"}, format="json")

        self.assertTrue(DeviceToken.objects.filter(fcm_token="theirs").exists())

    def test_logout_removes_the_callers_tokens(self):
        self.token(self.cust_user, "phone")
        self.token(self.rider_user, "theirs")

        self.client.force_authenticate(self.cust_user)
        response = self.client.post(reverse("accounts:logout"), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(DeviceToken.objects.filter(user=self.cust_user).exists())
        self.assertTrue(DeviceToken.objects.filter(user=self.rider_user).exists())

    def test_logout_can_target_one_device(self):
        self.token(self.cust_user, "phone")
        self.token(self.cust_user, "tablet")

        self.client.force_authenticate(self.cust_user)
        self.client.post(reverse("accounts:logout"), {"fcm_token": "phone"}, format="json")

        self.assertEqual(
            list(DeviceToken.objects.filter(user=self.cust_user).values_list("fcm_token", flat=True)),
            ["tablet"],
        )

    def test_constants_exposes_the_platform_enum(self):
        self.client.force_authenticate(self.cust_user)
        data = self.client.get(reverse("constants")).data
        self.assertEqual(data["device_platform"], ["android", "ios"])


@override_settings(FIREBASE_CREDENTIALS_PATH=CREDENTIALS)
class SendingTests(FcmTestBase):
    def test_nothing_is_sent_without_tokens(self):
        with patch("core.fcm._dispatch") as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                notify_users(self.cust_user, "Hi", "There")
        dispatch.assert_not_called()

    def test_send_targets_every_token_the_user_owns(self):
        self.token(self.cust_user, "phone")
        self.token(self.cust_user, "tablet")

        with patch("core.fcm._dispatch") as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                notify_users(self.cust_user, "Hi", "There")

        dispatch.assert_called_once()
        self.assertCountEqual(dispatch.call_args.args[0], ["phone", "tablet"])

    def test_send_skips_other_users_tokens(self):
        self.token(self.cust_user, "mine")
        self.token(self.rider_user, "theirs")

        with patch("core.fcm._dispatch") as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                notify_users(self.cust_user, "Hi", "There")

        self.assertEqual(dispatch.call_args.args[0], ["mine"])

    def test_deactivated_accounts_are_not_pushed(self):
        self.token(self.cust_user, "phone")
        self.cust_user.is_active = False
        self.cust_user.save()

        with patch("core.fcm._dispatch") as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                notify_users(self.cust_user, "Hi", "There")

        dispatch.assert_not_called()

    def test_notify_admins_reaches_every_admin(self):
        second_admin = User.objects.create_superuser(
            username="boss2", email="boss2@example.com", password="Kidapawan!2026"
        )
        self.token(self.admin, "admin-1")
        self.token(second_admin, "admin-2")
        self.token(self.cust_user, "customer")

        with patch("core.fcm._dispatch") as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                notify_admins("New order", "YR-0001")

        self.assertCountEqual(dispatch.call_args.args[0], ["admin-1", "admin-2"])

    def test_nothing_is_sent_if_the_transaction_rolls_back(self):
        from django.db import transaction

        self.token(self.cust_user, "phone")

        with patch("core.fcm._dispatch") as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        notify_users(self.cust_user, "Hi", "There")
                        raise RuntimeError("rolled back")
                except RuntimeError:
                    pass

        dispatch.assert_not_called()


@override_settings(FIREBASE_CREDENTIALS_PATH=CREDENTIALS)
class DispatchTests(FcmTestBase):
    """The layer that actually talks to Firebase."""

    def setUp(self):
        super().setUp()
        self.messaging = MagicMock()
        self.messaging.UnregisteredError = type("UnregisteredError", (Exception,), {})
        self.messaging.SenderIdMismatchError = type(
            "SenderIdMismatchError", (Exception,), {}
        )
        self.module_patch = patch.dict(
            "sys.modules", {"firebase_admin.messaging": self.messaging}
        )

    def dispatch(self, tokens, responses):
        self.messaging.send_each_for_multicast.return_value = FakeBatch(responses)
        with patch("core.fcm._get_app", return_value=object()):
            with self.module_patch:
                fcm._dispatch(tokens, "Title", "Body", {"order_id": 7})

    def test_successful_send_keeps_tokens(self):
        self.token(self.cust_user, "good")
        self.dispatch(["good"], [FakeResult(True)])

        self.assertTrue(DeviceToken.objects.filter(fcm_token="good").exists())

    def test_unregistered_tokens_are_deleted(self):
        self.token(self.cust_user, "dead")
        self.token(self.cust_user, "alive")

        self.dispatch(
            ["dead", "alive"],
            [
                FakeResult(False, self.messaging.UnregisteredError("gone")),
                FakeResult(True),
            ],
        )

        self.assertFalse(DeviceToken.objects.filter(fcm_token="dead").exists())
        self.assertTrue(DeviceToken.objects.filter(fcm_token="alive").exists())

    def test_sender_id_mismatch_also_prunes(self):
        self.token(self.cust_user, "wrong-project")
        self.dispatch(
            ["wrong-project"],
            [FakeResult(False, self.messaging.SenderIdMismatchError("nope"))],
        )
        self.assertFalse(DeviceToken.objects.filter(fcm_token="wrong-project").exists())

    def test_transient_failures_do_not_delete_the_token(self):
        self.token(self.cust_user, "flaky")
        self.dispatch(["flaky"], [FakeResult(False, RuntimeError("timeout"))])

        self.assertTrue(DeviceToken.objects.filter(fcm_token="flaky").exists())

    def test_firebase_blowing_up_never_raises(self):
        self.token(self.cust_user, "phone")
        self.messaging.send_each_for_multicast.side_effect = RuntimeError("FCM down")

        with patch("core.fcm._get_app", return_value=object()):
            with self.module_patch:
                fcm._dispatch(["phone"], "Title", "Body", None)  # must not raise

        self.assertTrue(DeviceToken.objects.filter(fcm_token="phone").exists())

    def test_data_values_are_stringified(self):
        self.token(self.cust_user, "phone")
        self.dispatch(["phone"], [FakeResult(True)])

        kwargs = self.messaging.MulticastMessage.call_args.kwargs
        self.assertEqual(kwargs["data"], {"order_id": "7"})


@override_settings(FIREBASE_CREDENTIALS_JSON="", FIREBASE_CREDENTIALS_PATH="")
class ConfigurationTests(FcmTestBase):
    def test_unconfigured_credentials_disable_pushes_quietly(self):
        self.assertIsNone(fcm._get_app())

    @override_settings(FIREBASE_CREDENTIALS_PATH="/does/not/exist.json")
    def test_a_missing_credentials_file_is_logged_not_raised(self):
        with self.assertLogs("core.fcm", level="INFO"):
            self.assertIsNone(fcm._get_app())

    @override_settings(FIREBASE_CREDENTIALS_PATH="/does/not/exist.json")
    def test_initialisation_is_attempted_only_once(self):
        with self.assertLogs("core.fcm", level="INFO"):
            fcm._get_app()
        # Second call is memoised: no further attempt, no further logging.
        self.assertIsNone(fcm._get_app())

    def test_a_push_with_no_firebase_configured_is_harmless(self):
        self.token(self.cust_user, "phone")
        with self.captureOnCommitCallbacks(execute=True):
            notify_users(self.cust_user, "Hi", "There")  # must not raise


# A structurally complete service account. The private key is not a real one;
# firebase-admin is mocked, so it is never parsed.
FAKE_SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "yellow-ribbon-test",
    "private_key_id": "abc123",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnotarealkey\n-----END PRIVATE KEY-----\n",
    "client_email": "fcm@yellow-ribbon-test.iam.gserviceaccount.com",
    "client_id": "1234567890",
    "token_uri": "https://oauth2.googleapis.com/token",
}


@override_settings(FIREBASE_CREDENTIALS_JSON="", FIREBASE_CREDENTIALS_PATH="")
class CredentialSourceTests(FcmTestBase):
    """
    FIREBASE_CREDENTIALS_JSON for hosted platforms, which hand you env vars
    rather than a filesystem; FIREBASE_CREDENTIALS_PATH for local dev.
    """

    def setUp(self):
        super().setUp()
        # Patch the attribute on the real module: `from firebase_admin import
        # credentials` resolves it there, so a sys.modules swap would not take.
        patcher = patch("firebase_admin.credentials.Certificate")
        self.certificate = patcher.start()
        self.addCleanup(patcher.stop)

    def load(self):
        return fcm._load_credential()

    @override_settings(FIREBASE_CREDENTIALS_JSON=json.dumps(FAKE_SERVICE_ACCOUNT))
    def test_json_env_var_is_parsed_into_a_certificate(self):
        credential, reason = self.load()

        self.assertIsNone(reason)
        self.assertIsNotNone(credential)
        # Passed as a dict, not a path.
        self.certificate.assert_called_once_with(FAKE_SERVICE_ACCOUNT)

    @override_settings(FIREBASE_CREDENTIALS_JSON=f"  {json.dumps(FAKE_SERVICE_ACCOUNT)}  ")
    def test_surrounding_whitespace_is_tolerated(self):
        credential, reason = self.load()
        self.assertIsNone(reason)
        self.assertIsNotNone(credential)

    @override_settings(FIREBASE_CREDENTIALS_PATH="/etc/secrets/sa.json")
    def test_falls_back_to_the_path(self):
        credential, reason = self.load()

        self.assertIsNone(reason)
        self.certificate.assert_called_once_with("/etc/secrets/sa.json")

    @override_settings(
        FIREBASE_CREDENTIALS_JSON=json.dumps(FAKE_SERVICE_ACCOUNT),
        FIREBASE_CREDENTIALS_PATH="/etc/secrets/sa.json",
    )
    def test_json_wins_when_both_are_set(self):
        self.load()
        self.certificate.assert_called_once_with(FAKE_SERVICE_ACCOUNT)

    @override_settings(FIREBASE_CREDENTIALS_JSON="   ")
    def test_a_blank_json_var_falls_through_rather_than_erroring(self):
        credential, reason = self.load()
        self.assertIsNone(credential)
        self.assertIn("Neither", reason)

    @override_settings(FIREBASE_CREDENTIALS_JSON="{not valid json")
    def test_malformed_json_reports_a_useful_reason(self):
        credential, reason = self.load()

        self.assertIsNone(credential)
        self.assertIn("not valid JSON", reason)
        self.certificate.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON='["a", "list"]')
    def test_json_that_is_not_an_object_is_rejected(self):
        credential, reason = self.load()

        self.assertIsNone(credential)
        self.assertIn("must be a JSON object", reason)

    def test_neither_source_set(self):
        credential, reason = self.load()
        self.assertIsNone(credential)
        self.assertIn("push notifications are off", reason)

    @override_settings(FIREBASE_CREDENTIALS_JSON=json.dumps(FAKE_SERVICE_ACCOUNT))
    def test_the_private_key_is_never_logged(self):
        self.certificate.side_effect = ValueError("bad certificate")

        with self.assertLogs("core.fcm", level="INFO") as logs:
            fcm._get_app()

        output = "\n".join(logs.output)
        self.assertNotIn("notarealkey", output)
        self.assertNotIn("PRIVATE KEY", output)

    @override_settings(FIREBASE_CREDENTIALS_JSON="{not valid json")
    def test_bad_json_does_not_break_a_push(self):
        self.token(self.cust_user, "phone")
        with self.captureOnCommitCallbacks(execute=True):
            notify_users(self.cust_user, "Hi", "There")  # must not raise


@override_settings(FIREBASE_CREDENTIALS_PATH=CREDENTIALS)
class NotificationEventTests(FcmTestBase):
    """The four events from spec section 9."""

    def setUp(self):
        super().setUp()
        self.token(self.admin, "admin-device")
        self.token(self.cust_user, "customer-device")
        self.token(self.rider_user, "rider-device")

        patcher = patch("core.fcm._dispatch")
        self.dispatch = patcher.start()
        self.addCleanup(patcher.stop)

    def sent(self):
        """[(tokens, title), ...] for everything dispatched."""
        return [
            (tuple(call.args[0]), call.args[1]) for call in self.dispatch.call_args_list
        ]

    def titles_for(self, token):
        return [title for tokens, title in self.sent() if token in tokens]

    # -- event 1: new order -> admin -----------------------------------

    def test_cod_placement_notifies_the_admin(self):
        self.client.force_authenticate(self.cust_user)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("orders:order-list"),
                {
                    "items": [{"menu_id": self.menu.pk, "quantity": 1}],
                    "delivery_address": "Blue gate",
                    "delivery_latitude": "7.008300",
                    "delivery_longitude": "125.089400",
                    "payment_type": "cod",
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("New order", self.titles_for("admin-device"))

    def test_gcash_placement_does_not_notify_the_admin(self):
        """An unpaid order must not reach the kitchen — or the admin's phone."""
        self.client.force_authenticate(self.cust_user)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("orders:order-list"),
                {
                    "items": [{"menu_id": self.menu.pk, "quantity": 1}],
                    "delivery_address": "Blue gate",
                    "delivery_latitude": "7.008300",
                    "delivery_longitude": "125.089400",
                    "payment_type": "gcash",
                },
                format="json",
            )

        self.assertNotIn("New order", self.titles_for("admin-device"))

    def test_paid_gcash_notifies_the_admin(self):
        from payments.services import settle_payment

        order = self.make_order(
            status=OrderStatus.PENDING_PAYMENT, payment_type=PaymentType.GCASH
        )
        with self.captureOnCommitCallbacks(execute=True):
            settle_payment(order, payment_id="pay_1")

        self.assertIn("New order", self.titles_for("admin-device"))

    # -- event 2: assigned -> rider ------------------------------------

    def test_assignment_notifies_the_rider(self):
        order = self.make_order(status=OrderStatus.CONFIRMED)

        self.client.force_authenticate(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("admin-order-assign", args=[order.pk]),
                {"rider_id": self.rider.pk},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("New delivery assigned", self.titles_for("rider-device"))
        self.assertNotIn("New delivery assigned", self.titles_for("customer-device"))

    # -- event 3: status change -> customer ----------------------------

    def test_admin_status_change_notifies_the_customer(self):
        order = self.make_order()

        self.client.force_authenticate(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(
                reverse("admin-order-status", args=[order.pk]),
                {"status": "confirmed"},
                format="json",
            )

        self.assertIn("Order confirmed", self.titles_for("customer-device"))

    def test_rider_pickup_notifies_the_customer(self):
        order = self.make_order(status=OrderStatus.READY)
        delivery = Delivery.objects.create(order=order, rider=self.rider)

        self.client.force_authenticate(self.rider_user)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(
                reverse("delivery:rider-delivery-status", args=[delivery.pk]),
                {"status": "picked_up"},
                format="json",
            )

        self.assertIn("On the way", self.titles_for("customer-device"))

    def test_status_push_does_not_go_to_the_rider(self):
        order = self.make_order()

        self.client.force_authenticate(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(
                reverse("admin-order-status", args=[order.pk]),
                {"status": "confirmed"},
                format="json",
            )

        self.assertNotIn("Order confirmed", self.titles_for("rider-device"))

    # -- event 4: delivered -> customer + admin ------------------------

    def test_completion_notifies_customer_and_admin(self):
        order = self.make_order(status=OrderStatus.READY)
        delivery = Delivery.objects.create(order=order, rider=self.rider)

        self.client.force_authenticate(self.rider_user)
        for value in ("picked_up", "in_transit"):
            self.client.patch(
                reverse("delivery:rider-delivery-status", args=[delivery.pk]),
                {"status": value},
                format="json",
            )

        self.dispatch.reset_mock()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(
                reverse("delivery:rider-delivery-status", args=[delivery.pk]),
                {"status": "delivered"},
                format="json",
            )

        self.assertIn("Delivered", self.titles_for("customer-device"))
        self.assertIn("Delivery completed", self.titles_for("admin-device"))

    # -- customer cancels their own order -> admin ---------------------

    def cancel_as_customer(self, order, reason=""):
        self.client.force_authenticate(self.cust_user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse("orders:order-cancel", args=[order.pk]),
                {"cancel_reason": reason} if reason else {},
                format="json",
            )

    def test_customer_cancellation_notifies_the_admin(self):
        order = self.make_order()
        response = self.cancel_as_customer(order, "Changed my mind")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(
            "Order cancelled by customer", self.titles_for("admin-device")
        )

    def test_customer_is_not_pushed_about_their_own_cancellation(self):
        order = self.make_order()
        self.cancel_as_customer(order)

        titles = self.titles_for("customer-device")
        self.assertNotIn("Order cancelled", titles)
        self.assertNotIn("Order cancelled by customer", titles)

    def test_cancellation_push_carries_the_order_number(self):
        order = self.make_order()
        self.cancel_as_customer(order)

        bodies = [
            call.args[2]
            for call in self.dispatch.call_args_list
            if call.args[1] == "Order cancelled by customer"
        ]
        self.assertTrue(bodies)
        self.assertIn(order.order_number, bodies[0])
        self.assertIn("maria", bodies[0])

    def test_cancellation_still_records_the_status_log(self):
        order = self.make_order()
        self.cancel_as_customer(order, "Too slow")

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertIsNotNone(order.cancelled_at)
        self.assertEqual(order.cancel_reason, "Too slow")

        log = OrderStatusLog.objects.filter(order=order).latest("changed_at")
        self.assertEqual(log.status, OrderStatus.CANCELLED)
        self.assertEqual(log.changed_by, self.cust_user)

    def test_a_failed_admin_push_does_not_block_the_cancellation(self):
        self.dispatch.side_effect = RuntimeError("FCM exploded")
        order = self.make_order()

        response = self.cancel_as_customer(order)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)

    def test_a_rejected_cancellation_pushes_nothing(self):
        order = self.make_order(status=OrderStatus.CONFIRMED)
        response = self.cancel_as_customer(order)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn(
            "Order cancelled by customer", self.titles_for("admin-device")
        )

    # -- rider stand-down on cancellation ------------------------------

    def assign_rider(self, order, delivery_status=DeliveryStatus.ASSIGNED):
        return Delivery.objects.create(
            order=order, rider=self.rider, status=delivery_status
        )

    def test_customer_cancellation_tells_an_assigned_rider_to_stop(self):
        order = self.make_order()
        self.assign_rider(order)

        self.cancel_as_customer(order)
        self.assertIn("Order cancelled", self.titles_for("rider-device"))

    def test_admin_cancellation_tells_an_assigned_rider_to_stop(self):
        order = self.make_order()
        self.assign_rider(order)

        self.client.force_authenticate(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(
                reverse("admin-order-status", args=[order.pk]),
                {"status": "cancelled"},
                format="json",
            )

        self.assertIn("Order cancelled", self.titles_for("rider-device"))

    def test_no_rider_push_when_nobody_is_assigned(self):
        order = self.make_order()
        self.cancel_as_customer(order)

        self.assertNotIn("Order cancelled", self.titles_for("rider-device"))

    def test_no_rider_push_when_the_delivery_already_finished(self):
        order = self.make_order()
        self.assign_rider(order, DeliveryStatus.FAILED)

        self.cancel_as_customer(order)
        self.assertNotIn("Order cancelled", self.titles_for("rider-device"))

    # -- rider marks a delivery failed -> admin ------------------------

    def test_failed_delivery_notifies_the_admin(self):
        order = self.make_order(status=OrderStatus.READY)
        delivery = Delivery.objects.create(order=order, rider=self.rider)

        self.client.force_authenticate(self.rider_user)
        self.client.patch(
            reverse("delivery:rider-delivery-status", args=[delivery.pk]),
            {"status": "picked_up"},
            format="json",
        )
        self.dispatch.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                reverse("delivery:rider-delivery-status", args=[delivery.pk]),
                {"status": "failed", "failure_reason": "Nobody at the gate"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Delivery failed", self.titles_for("admin-device"))

    def test_failed_delivery_push_includes_the_reason(self):
        order = self.make_order(status=OrderStatus.READY)
        delivery = Delivery.objects.create(order=order, rider=self.rider)

        self.client.force_authenticate(self.rider_user)
        self.client.patch(
            reverse("delivery:rider-delivery-status", args=[delivery.pk]),
            {"status": "picked_up"},
            format="json",
        )
        self.dispatch.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(
                reverse("delivery:rider-delivery-status", args=[delivery.pk]),
                {"status": "failed", "failure_reason": "Wrong address"},
                format="json",
            )

        bodies = [
            call.args[2]
            for call in self.dispatch.call_args_list
            if call.args[1] == "Delivery failed"
        ]
        self.assertTrue(bodies)
        self.assertIn(order.order_number, bodies[0])
        self.assertIn("Wrong address", bodies[0])
        self.assertIn("Reassign or cancel", bodies[0])

    def test_failed_delivery_does_not_push_the_customer(self):
        """The order has not moved, so there is nothing to tell them yet."""
        order = self.make_order(status=OrderStatus.READY)
        delivery = Delivery.objects.create(order=order, rider=self.rider)

        self.client.force_authenticate(self.rider_user)
        self.client.patch(
            reverse("delivery:rider-delivery-status", args=[delivery.pk]),
            {"status": "picked_up"},
            format="json",
        )
        self.dispatch.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(
                reverse("delivery:rider-delivery-status", args=[delivery.pk]),
                {"status": "failed"},
                format="json",
            )

        self.assertEqual(self.titles_for("customer-device"), [])

    def test_a_failed_push_does_not_block_marking_a_delivery_failed(self):
        order = self.make_order(status=OrderStatus.READY)
        delivery = Delivery.objects.create(order=order, rider=self.rider)

        self.client.force_authenticate(self.rider_user)
        self.client.patch(
            reverse("delivery:rider-delivery-status", args=[delivery.pk]),
            {"status": "picked_up"},
            format="json",
        )
        self.dispatch.side_effect = RuntimeError("FCM exploded")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                reverse("delivery:rider-delivery-status", args=[delivery.pk]),
                {"status": "failed"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, DeliveryStatus.FAILED)

    def test_a_push_failure_does_not_break_the_order(self):
        """The whole point: a dead phone must not roll anything back."""
        self.dispatch.side_effect = RuntimeError("FCM exploded")
        order = self.make_order()

        self.client.force_authenticate(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                reverse("admin-order-status", args=[order.pk]),
                {"status": "confirmed"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CONFIRMED)
