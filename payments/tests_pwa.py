"""
The customer web app (iPhone PWA): returning to it after paying, web push, and
web device tokens. Every gateway and Firebase call is mocked.
"""

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status

from accounts.models import DeviceToken
from core import fcm
from core.fcm import reset_app_cache
from orders.constants import OrderStatus, PaymentStatus, PaymentType
from orders.tests_admin import make_order
from payments.models import Invoice

from .tests_maya import MAYA, MayaBase, maya_payment

PWA = "https://yellow-ribbon-backend.onrender.com/order"
WITH_PWA = override_settings(PWA_BASE_URL=PWA)
GCASH_SOURCE = {"source_id": "src_1", "checkout_url": "https://g/1", "amount_centavos": 32900}


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


@WITH_PWA
@override_settings(PAYMONGO_SECRET_KEY="sk_test_x")
class GcashWebCheckoutTests(MayaBase):
    url = reverse("payments:checkout")

    def checkout(self, **extra):
        order = make_order(self.customer, payment_type=PaymentType.GCASH, status=OrderStatus.PENDING_PAYMENT)
        self.client.force_authenticate(self.user)
        with patch("payments.views.create_gcash_source", return_value=GCASH_SOURCE) as source:
            response = self.client.post(self.url, {"order_id": order.pk, **extra}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return order, source.call_args.args[1], source.call_args.args[2]

    def test_web_checkout_marks_both_return_urls(self):
        order, success, failed = self.checkout(client="web")
        self.assertTrue(success.split("?")[0].endswith("/api/payments/return/success/"))
        self.assertEqual(query(success), {"client": "web", "order": str(order.pk)})
        self.assertEqual(query(failed), {"client": "web", "order": str(order.pk)})

    def test_the_android_app_is_unchanged(self):
        _, success, failed = self.checkout()
        self.assertTrue(success.endswith("/api/payments/return/success/"))
        self.assertTrue(failed.endswith("/api/payments/return/failed/"))

    def test_another_client_value_is_ignored(self):
        _, success, _ = self.checkout(client="ios")
        self.assertNotIn("client=", success)

    @override_settings(
        PAYMONGO_SUCCESS_REDIRECT_URL="https://api.example.com/api/payments/return/success/?src=gcash",
        PAYMONGO_FAILED_REDIRECT_URL="https://api.example.com/api/payments/return/failed/",
    )
    def test_configured_redirect_urls_get_the_web_params_too(self):
        order, success, failed = self.checkout(client="web")
        self.assertTrue(success.startswith("https://api.example.com/api/payments/return/success/"))
        self.assertEqual(query(success), {"src": "gcash", "client": "web", "order": str(order.pk)})
        self.assertEqual(query(failed), {"client": "web", "order": str(order.pk)})

    @override_settings(PWA_BASE_URL="")
    def test_without_a_pwa_configured_web_is_ignored(self):
        _, success, _ = self.checkout(client="web")
        self.assertNotIn("client=", success)


@MAYA
@WITH_PWA
class MayaWebCheckoutTests(MayaBase):
    @patch("payments.maya.create_checkout")
    def test_web_checkout_keeps_the_rrn_and_adds_the_web_params(self, create):
        create.return_value = {"checkout_id": "co-1", "checkout_url": "https://pay.maya/co-1"}
        order = self.maya_order()
        self.client.force_authenticate(self.user)

        self.client.post(reverse("payments:checkout"), {"order_id": order.pk, "client": "web"}, format="json")

        _, rrn, success, failed, cancel = create.call_args[0]
        expected = {"rrn": rrn, "client": "web", "order": str(order.pk)}
        self.assertEqual(query(success), expected)
        self.assertEqual(query(failed), expected)
        self.assertEqual(failed, cancel)
        self.assertEqual(Invoice.objects.get().provider_rrn, rrn)


@MAYA
@WITH_PWA
class WebReturnTests(MayaBase):
    def land(self, name, **params):
        return self.client.get(reverse(f"payments:{name}"), params)

    def test_gcash_success_goes_back_to_the_pwa(self):
        response = self.land("return-success", client="web", order="42")
        self.assertRedirects(response, f"{PWA}/payment/success?order=42", fetch_redirect_response=False)

    def test_gcash_failure_goes_back_to_the_pwa(self):
        response = self.land("return-failed", client="web", order="42")
        self.assertRedirects(response, f"{PWA}/payment/failed?order=42", fetch_redirect_response=False)

    @patch("payments.maya.retrieve_payment")
    def test_maya_is_still_verified_before_the_redirect(self, lookup):
        order = self.maya_order()
        self.open_checkout(order, rrn="r1")
        lookup.return_value = maya_payment("paid")

        response = self.land("return-success", rrn="r1", client="web", order=str(order.pk))

        self.assertRedirects(response, f"{PWA}/payment/success?order={order.pk}", fetch_redirect_response=False)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.PAID)

    @patch("payments.maya.retrieve_payment")
    def test_a_maya_failure_on_the_success_url_lands_on_failed(self, lookup):
        order = self.maya_order()
        self.open_checkout(order, rrn="r1")
        lookup.return_value = maya_payment("failed")
        response = self.land("return-success", rrn="r1", client="web", order=str(order.pk))
        self.assertRedirects(response, f"{PWA}/payment/failed?order={order.pk}", fetch_redirect_response=False)

    @patch("payments.maya.retrieve_payment")
    def test_paid_in_another_tab_then_cancelled_lands_on_success(self, lookup):
        order = self.maya_order()
        self.open_checkout(order, rrn="r1")
        lookup.return_value = maya_payment("paid")
        response = self.land("return-failed", rrn="r1", client="web", order=str(order.pk))
        self.assertRedirects(response, f"{PWA}/payment/success?order={order.pk}", fetch_redirect_response=False)

    def test_the_redirect_cannot_be_pointed_elsewhere(self):
        for order in ["https://evil.example.com", "42/../../x", "-1", "", "４２"]:
            response = self.land("return-success", client="web", order=order)
            self.assertEqual(response.status_code, 200, order)
            self.assertContains(response, "yellowribbon://payment/success")

    def test_the_android_pages_are_unchanged(self):
        response = self.land("return-success")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "yellowribbon://payment/success")

    @override_settings(PWA_BASE_URL="")
    def test_no_redirect_without_a_pwa_configured(self):
        response = self.land("return-success", client="web", order="42")
        self.assertEqual(response.status_code, 200)


@override_settings(FIREBASE_CREDENTIALS_PATH="/fake/service-account.json")
class WebPushTests(TestCase):
    def setUp(self):
        reset_app_cache()
        self.addCleanup(reset_app_cache)

    def send(self, data):
        messaging = MagicMock()
        messaging.UnregisteredError = type("UnregisteredError", (Exception,), {})
        messaging.SenderIdMismatchError = type("SenderIdMismatchError", (Exception,), {})
        messaging.send_each_for_multicast.return_value = MagicMock(
            responses=[MagicMock(success=True)], success_count=1, failure_count=0
        )
        with patch("core.fcm._get_app", return_value=object()):
            with patch.dict("sys.modules", {"firebase_admin.messaging": messaging}):
                fcm._dispatch(["tok"], "On the way", "Your rider has picked up YR-0001.", data)
        return messaging

    @WITH_PWA
    def test_an_order_push_opens_that_order_in_the_pwa(self):
        messaging = self.send({"type": "order", "order_id": 17})
        messaging.WebpushFCMOptions.assert_called_once_with(link=f"{PWA}/orders/17")
        messaging.WebpushNotification.assert_called_once_with(
            icon=f"{PWA}/icons/icon-192.png", badge=f"{PWA}/icons/badge-72.png"
        )
        messaging.WebpushConfig.assert_called_once_with(
            headers={"Urgency": "high"},
            notification=messaging.WebpushNotification.return_value,
            fcm_options=messaging.WebpushFCMOptions.return_value,
        )
        sent = messaging.MulticastMessage.call_args.kwargs
        self.assertEqual(sent["webpush"], messaging.WebpushConfig.return_value)
        # iOS: never data-only. The visible notification and Android config stay.
        messaging.Notification.assert_called_once()
        self.assertEqual(sent["android"], messaging.AndroidConfig.return_value)

    @WITH_PWA
    def test_a_push_without_an_order_opens_the_home_page(self):
        messaging = self.send({"type": "test"})
        messaging.WebpushFCMOptions.assert_called_once_with(link=f"{PWA}/")

    @override_settings(PWA_BASE_URL="")
    def test_no_webpush_config_without_a_pwa(self):
        messaging = self.send({"order_id": 17})
        self.assertIsNone(messaging.MulticastMessage.call_args.kwargs["webpush"])
        messaging.WebpushConfig.assert_not_called()

    @override_settings(PWA_BASE_URL="http://127.0.0.1:8000/order")
    def test_a_non_https_pwa_gets_no_webpush_config(self):
        # Firebase refuses an http:// link and would fail the Android tokens too.
        messaging = self.send({"order_id": 17})
        self.assertIsNone(messaging.MulticastMessage.call_args.kwargs["webpush"])
        messaging.WebpushFCMOptions.assert_not_called()

    @WITH_PWA
    def test_the_admin_test_push_works_for_web_tokens(self):
        messaging = MagicMock()
        with patch("core.fcm._get_app", return_value=object()):
            with patch.dict("sys.modules", {"firebase_admin.messaging": messaging}):
                error, results = fcm.send_test_push([("maria (web)", "tok")])
        self.assertIsNone(error)
        self.assertEqual(results, [("maria (web)", True, "sent")])
        sent = messaging.Message.call_args.kwargs
        self.assertEqual(sent["webpush"], messaging.WebpushConfig.return_value)
        messaging.WebpushFCMOptions.assert_called_once_with(link=f"{PWA}/")


class WebDeviceTokenTests(MayaBase):
    def test_the_pwa_registers_a_web_token(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(
            reverse("device-tokens"), {"fcm_token": "web-tok", "platform": "web"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(DeviceToken.objects.get(fcm_token="web-tok").platform, "web")

    def test_constants_list_the_web_platform(self):
        self.client.force_authenticate(self.user)
        self.assertEqual(
            self.client.get(reverse("constants")).data["device_platform"], ["android", "ios", "web"]
        )
