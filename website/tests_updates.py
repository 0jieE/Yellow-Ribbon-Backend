"""App updates: the publish API GitHub calls, the version API the apps call."""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .models import AppRelease
from .tests import APK_BYTES, make_users, release

GH = "https://github.com/0jieE/yellow-Ribbon-Mobile/releases/download/v1.0.1/"
TOKEN = "test-publish-token-0123456789"


@patch("django.conf.settings.APP_RELEASE_PUBLISH_TOKEN", TOKEN)
class PublishApiTests(TestCase):
    url = reverse("app-release-publish")

    def setUp(self):
        self.api = APIClient()

    def publish(self, token=TOKEN, **overrides):
        body = {
            "app": "customer",
            "version": "1.0.1",
            "version_code": 2,
            "download_url": GH + "YellowRibbon-1.0.1.apk",
            "size": 18_000_000,
            "notes": "Faster checkout.",
            **overrides,
        }
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.api.post(self.url, body, format="json", **headers)

    def test_publishing_creates_the_release(self):
        response = self.publish()
        self.assertEqual(response.status_code, 200, response.data)
        obj = AppRelease.objects.get(app="customer")
        self.assertEqual((obj.version, obj.version_code), ("1.0.1", 2))
        self.assertEqual(obj.download_url, GH + "YellowRibbon-1.0.1.apk")
        self.assertEqual(obj.notes, "Faster checkout.")
        self.assertEqual(obj.uploaded_by, "github-actions")

    def test_publishing_replaces_an_uploaded_file(self):
        release()
        self.publish()
        obj = AppRelease.objects.get(app="customer")
        self.assertEqual(obj.chunks.count(), 0)
        self.assertEqual(obj.size, 18_000_000)

    def test_the_same_version_twice_is_fine_an_older_one_is_refused(self):
        self.publish(version_code=5)
        self.assertEqual(self.publish(version_code=5).status_code, 200)
        response = self.publish(version_code=4)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Bump the build number", response.data["error"])
        self.assertEqual(AppRelease.objects.get().version_code, 5)

    def test_wrong_or_missing_token(self):
        self.assertEqual(self.publish(token="nope").status_code, 403)
        self.assertEqual(self.publish(token=None).status_code, 403)
        self.assertFalse(AppRelease.objects.exists())

    def test_a_link_outside_the_mobile_repo_is_refused(self):
        for url in [
            "https://evil.example.com/YellowRibbon.apk",
            "https://github.com/someone-else/repo/releases/download/v1/x.apk",
            GH + "notes.txt",
        ]:
            self.assertEqual(self.publish(download_url=url).status_code, 400, url)
        self.assertFalse(AppRelease.objects.exists())

    def test_bad_fields(self):
        self.assertEqual(self.publish(app="hacker").status_code, 400)
        self.assertEqual(self.publish(version_code=0).status_code, 400)
        self.assertEqual(self.publish(version="1.0 <b>").status_code, 400)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.api.get(self.url).status_code, 405)


class PublishTurnedOffTests(TestCase):
    @patch("django.conf.settings.APP_RELEASE_PUBLISH_TOKEN", "")
    def test_publishing_is_off_without_a_token_setting(self):
        response = APIClient().post(
            reverse("app-release-publish"), {}, format="json", HTTP_AUTHORIZATION="Bearer "
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("not set", response.data["error"])


class AppVersionApiTests(TestCase):
    def get(self, app, client=None):
        return (client or APIClient()).get(reverse("app-version", args=[app]))

    def test_a_github_release(self):
        AppRelease.objects.create(
            app="rider", version="1.0.1", version_code=2,
            download_url=GH + "RibbonRider-1.0.1.apk", notes="Bigger map.",
        )
        response = self.get("rider")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["version"], "1.0.1")
        self.assertEqual(response.data["version_code"], 2)
        self.assertEqual(response.data["download_url"], GH + "RibbonRider-1.0.1.apk")
        self.assertEqual(response.data["notes"], "Bigger map.")
        self.assertIsNone(response.data["size"])

    def test_an_uploaded_file_points_at_the_site(self):
        release()
        response = self.get("customer")
        self.assertEqual(response.data["download_url"], "http://testserver/download/customer.apk")
        self.assertEqual(response.data["size"], len(APK_BYTES))

    def test_nothing_published_yet(self):
        response = self.get("customer")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.data)

    def test_unknown_app(self):
        self.assertEqual(self.get("x").status_code, 404)

    def test_an_expired_login_does_not_break_the_check(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer expired.junk.token")
        release()
        self.assertEqual(self.get("customer", client).status_code, 200)


class GitHubHostedDownloadTests(TestCase):
    def setUp(self):
        self.users = make_users()
        for app, name in [("customer", "YellowRibbon"), ("rider", "RibbonRider")]:
            AppRelease.objects.create(
                app=app, version="1.0.1", version_code=2, download_url=f"{GH}{name}-1.0.1.apk"
            )

    def test_the_website_sends_downloads_to_github(self):
        response = self.client.get("/download/customer.apk")
        self.assertRedirects(response, GH + "YellowRibbon-1.0.1.apk", fetch_redirect_response=False)

    def test_the_home_page_offers_it(self):
        self.assertContains(self.client.get("/"), "version 1.0.1.")

    def test_the_rider_link_still_needs_a_sign_in(self):
        self.assertRedirects(self.client.get("/download/rider.apk"), reverse("website:staff-apps"))
        self.client.force_login(self.users["rider"])
        self.assertRedirects(
            self.client.get("/download/rider.apk"),
            GH + "RibbonRider-1.0.1.apk",
            fetch_redirect_response=False,
        )

    def test_admin_can_enter_a_link_by_hand(self):
        self.client.force_login(self.users["boss"])
        response = self.client.post(
            reverse("admin:website_apprelease_add"),
            {
                "app": "admin",
                "version": "1.0.0",
                "version_code": "1",
                "download_url": GH + "RibbonAdmin-1.0.0.apk",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AppRelease.objects.get(app="admin").uploaded_by, "boss")

    def test_switching_an_uploaded_app_to_a_link_drops_the_file(self):
        AppRelease.objects.filter(app="customer").delete()
        obj = release()
        self.client.force_login(self.users["boss"])
        self.client.post(
            reverse("admin:website_apprelease_change", args=[obj.pk]),
            {"version": "1.0.1", "version_code": "2", "download_url": GH + "YellowRibbon-1.0.1.apk", "notes": ""},
        )
        obj.refresh_from_db()
        self.assertEqual(obj.chunks.count(), 0)
        self.assertEqual(obj.download_url, GH + "YellowRibbon-1.0.1.apk")
