"""
Push diagnostics: the notification channel, the "no token" log line, and the
admin's "Send a test push" actions.

Firebase is never contacted; the messaging module is mocked throughout.
"""

from unittest.mock import MagicMock, patch

from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import DeviceToken, Rider, User
from core import fcm
from core.fcm import ANDROID_CHANNEL_ID, notify_admins, notify_users, reset_app_cache

PASSWORD = "Kidapawan!2026"
CREDENTIALS = "/fake/service-account.json"


class PushBase(TestCase):
    def setUp(self):
        reset_app_cache()
        self.addCleanup(reset_app_cache)
        self.boss = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        self.rider_user = User.objects.create_user(
            "rico", "rico@example.com", PASSWORD, is_rider=True, is_customer=False
        )
        Rider.objects.create(user=self.rider_user)

    def messaging(self):
        m = MagicMock()
        m.UnregisteredError = type("UnregisteredError", (Exception,), {})
        m.SenderIdMismatchError = type("SenderIdMismatchError", (Exception,), {})
        return m


@override_settings(FIREBASE_CREDENTIALS_PATH=CREDENTIALS)
class ChannelTests(PushBase):
    def dispatch(self, messaging):
        messaging.send_each_for_multicast.return_value = MagicMock(
            responses=[MagicMock(success=True)], success_count=1, failure_count=0
        )
        with patch("core.fcm._get_app", return_value=object()):
            with patch.dict("sys.modules", {"firebase_admin.messaging": messaging}):
                fcm._dispatch(["tok"], "Title", "Body", {"order_id": 7})

    def test_the_channel_id_matches_what_the_apps_create(self):
        self.assertEqual(ANDROID_CHANNEL_ID, "yellowribbon_orders")

    def test_every_push_is_filed_under_that_channel_with_high_priority(self):
        messaging = self.messaging()

        self.dispatch(messaging)

        messaging.AndroidNotification.assert_called_once_with(channel_id="yellowribbon_orders")
        messaging.AndroidConfig.assert_called_once_with(
            priority="high", notification=messaging.AndroidNotification.return_value
        )
        sent = messaging.MulticastMessage.call_args.kwargs
        self.assertEqual(sent["android"], messaging.AndroidConfig.return_value)

    def test_the_title_body_and_data_are_unchanged(self):
        messaging = self.messaging()
        self.dispatch(messaging)
        sent = messaging.MulticastMessage.call_args.kwargs
        messaging.Notification.assert_called_once_with(title="Title", body="Body")
        self.assertEqual(sent["data"], {"order_id": "7"})
        self.assertEqual(sent["tokens"], ["tok"])


@override_settings(FIREBASE_CREDENTIALS_PATH=CREDENTIALS)
class NoTokenLoggingTests(PushBase):
    def test_a_user_with_no_token_is_named_in_the_log(self):
        with self.assertLogs("core.fcm", level="INFO") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                notify_users(self.rider_user, "New delivery assigned", "YR-1 to x")

        line = "\n".join(logs.output)
        self.assertIn("rico", line)
        self.assertIn("New delivery assigned", line)
        self.assertIn("No device token", line)

    def test_several_users_are_listed_sorted(self):
        with self.assertLogs("core.fcm", level="INFO") as logs:
            notify_users([self.rider_user, self.boss], "Hi", "There")
        self.assertIn("boss, rico", "\n".join(logs.output))

    def test_tokens_are_never_written_to_the_log(self):
        DeviceToken.objects.create(user=self.boss, fcm_token="SECRET-TOKEN-VALUE", platform="android")
        with self.assertLogs("core.fcm", level="INFO") as logs:
            notify_users(self.rider_user, "Hi", "There")
        self.assertNotIn("SECRET-TOKEN-VALUE", "\n".join(logs.output))

    def test_a_user_who_has_a_token_logs_no_such_line(self):
        DeviceToken.objects.create(user=self.rider_user, fcm_token="tok", platform="android")
        with patch("core.fcm._dispatch"):
            with self.assertNoLogs("core.fcm", level="INFO"):
                with self.captureOnCommitCallbacks(execute=True):
                    notify_users(self.rider_user, "Hi", "There")

    def test_no_active_admins_is_logged_too(self):
        User.objects.filter(pk=self.boss.pk).update(is_active=False)
        with self.assertLogs("core.fcm", level="INFO") as logs:
            notify_admins("New order", "YR-1")
        self.assertIn("No active admin accounts", "\n".join(logs.output))


@override_settings(FIREBASE_CREDENTIALS_PATH=CREDENTIALS)
class SendTestPushTests(PushBase):
    def run_with(self, messaging, targets):
        with patch("core.fcm._get_app", return_value=object()):
            with patch.dict("sys.modules", {"firebase_admin.messaging": messaging}):
                return fcm.send_test_push(targets)

    def test_a_good_token_is_reported_sent(self):
        messaging = self.messaging()
        error, results = self.run_with(messaging, [("rico", "tok")])

        self.assertIsNone(error)
        self.assertEqual(results, [("rico", True, "sent")])
        sent = messaging.Message.call_args.kwargs
        self.assertEqual(sent["token"], "tok")
        self.assertEqual(sent["data"], {"type": "test", "event": "test"})
        messaging.AndroidNotification.assert_called_with(channel_id="yellowribbon_orders")
        messaging.send.assert_called_once()
        self.assertNotIn("dry_run", messaging.send.call_args.kwargs)  # a real send

    def test_each_failure_names_its_error_class(self):
        messaging = self.messaging()
        messaging.send.side_effect = [
            None,
            messaging.UnregisteredError("Requested entity was not found."),
            RuntimeError("quota"),
        ]
        error, results = self.run_with(messaging, [("a", "1"), ("b", "2"), ("c", "3")])

        self.assertIsNone(error)
        self.assertEqual(results[0], ("a", True, "sent"))
        self.assertFalse(results[1][1])
        self.assertTrue(results[1][2].startswith("UnregisteredError: Requested entity"))
        self.assertEqual(results[2][2], "RuntimeError: quota")

    def test_a_dead_token_is_reported_not_deleted(self):
        DeviceToken.objects.create(user=self.rider_user, fcm_token="dead", platform="android")
        messaging = self.messaging()
        messaging.send.side_effect = messaging.UnregisteredError("gone")

        self.run_with(messaging, [("rico", "dead")])

        self.assertTrue(DeviceToken.objects.filter(fcm_token="dead").exists())

    @override_settings(FIREBASE_CREDENTIALS_JSON="", FIREBASE_CREDENTIALS_PATH="")
    def test_without_firebase_it_says_why_instead_of_sending(self):
        error, results = fcm.send_test_push([("rico", "tok")])
        self.assertIn("Neither FIREBASE_CREDENTIALS_JSON nor FIREBASE_CREDENTIALS_PATH", error)
        self.assertEqual(results, [])

    @override_settings(FIREBASE_CREDENTIALS_JSON="not json")
    def test_bad_credentials_are_explained(self):
        error, _ = fcm.send_test_push([("rico", "tok")])
        self.assertIn("not valid JSON", error)

    def test_it_never_raises(self):
        with patch("core.fcm._get_app", side_effect=RuntimeError("boom")):
            error, results = fcm.send_test_push([("rico", "tok")])
        self.assertIn("Unexpected error", error)
        self.assertEqual(results, [])


class AdminActionTests(PushBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.boss)
        self.token = DeviceToken.objects.create(
            user=self.rider_user, fcm_token="rider-token-abcdef", platform="android"
        )

    def flash(self, response):
        return [(m.level_tag, str(m)) for m in get_messages(response.wsgi_request)]

    def run_action(self, name, url_name, selected):
        return self.client.post(
            reverse(url_name),
            {"action": name, "_selected_action": [o.pk for o in selected], "index": "0"},
            follow=True,
        )

    @patch("accounts.admin.fcm.send_test_push", return_value=(None, [("x", True, "sent")]))
    def test_device_token_action_sends_to_the_selected_tokens(self, send):
        response = self.run_action("send_test_push", "admin:accounts_devicetoken_changelist", [self.token])

        targets = send.call_args[0][0]
        self.assertEqual(len(targets), 1)
        self.assertIn("rico (android)", targets[0][0])
        self.assertEqual(targets[0][1], "rider-token-abcdef")
        levels = dict(self.flash(response))
        self.assertIn("accepted by Firebase", levels["success"])

    @patch("accounts.admin.fcm.send_test_push")
    def test_failures_are_listed_with_their_error_class(self, send):
        send.return_value = (None, [("rico (android) rider-to...(18)", False, "UnregisteredError: gone")])
        response = self.run_action("send_test_push", "admin:accounts_devicetoken_changelist", [self.token])

        warnings = [text for level, text in self.flash(response) if level == "warning"]
        self.assertTrue(any("UnregisteredError: gone" in w for w in warnings))

    @patch("accounts.admin.fcm.send_test_push", return_value=("Neither FIREBASE_CREDENTIALS_JSON set.", []))
    def test_unconfigured_firebase_is_an_error_message(self, _send):
        response = self.run_action("send_test_push", "admin:accounts_devicetoken_changelist", [self.token])
        errors = [text for level, text in self.flash(response) if level == "error"]
        self.assertTrue(any("No push was sent" in e and "FIREBASE_CREDENTIALS_JSON" in e for e in errors))

    @patch("accounts.admin.fcm.send_test_push", return_value=(None, [("x", True, "sent")]))
    def test_user_action_sends_to_every_token_the_user_has(self, send):
        DeviceToken.objects.create(user=self.rider_user, fcm_token="second-device-token", platform="ios")

        self.run_action("send_test_push", "admin:accounts_user_changelist", [self.rider_user])

        tokens = sorted(t for _, t in send.call_args[0][0])
        self.assertEqual(tokens, ["rider-token-abcdef", "second-device-token"])

    @patch("accounts.admin.fcm.send_test_push", return_value=(None, []))
    def test_a_user_with_no_token_is_called_out_by_name(self, send):
        response = self.run_action("send_test_push", "admin:accounts_user_changelist", [self.boss])

        warnings = [text for level, text in self.flash(response) if level == "warning"]
        self.assertTrue(any("boss has no device token registered" in w for w in warnings))
        send.assert_not_called()  # nothing to send, so Firebase is not even asked

    @patch("accounts.admin.fcm.send_test_push", return_value=(None, [("x", True, "sent")]))
    def test_mixed_selection_reports_the_missing_one_and_sends_the_rest(self, send):
        response = self.run_action(
            "send_test_push", "admin:accounts_user_changelist", [self.boss, self.rider_user]
        )

        self.assertEqual(len(send.call_args[0][0]), 1)
        warnings = [text for level, text in self.flash(response) if level == "warning"]
        self.assertTrue(any("boss has no device token" in w for w in warnings))

    @patch("accounts.admin.fcm.send_test_push")
    def test_a_long_failure_list_is_capped(self, send):
        send.return_value = (None, [(f"t{i}", False, "X: y") for i in range(40)])
        response = self.run_action("send_test_push", "admin:accounts_devicetoken_changelist", [self.token])
        warnings = [text for level, text in self.flash(response) if level == "warning"]
        self.assertEqual(len(warnings), 16)  # 15 lines + "and 25 more"
        self.assertTrue(any("25 more" in w for w in warnings))

    @patch("accounts.admin.fcm.send_test_push")
    def test_the_action_needs_the_change_permission(self, send):
        from django.contrib.auth.models import Permission

        viewer = User.objects.create_user("viewer", "v@example.com", PASSWORD, is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(codename="view_devicetoken"))
        self.client.force_login(viewer)

        self.run_action("send_test_push", "admin:accounts_devicetoken_changelist", [self.token])

        send.assert_not_called()
