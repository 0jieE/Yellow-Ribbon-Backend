"""The public website: landing page, rider sign-in, APK storage and downloads."""

import gzip
import hashlib
import json
from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from core import backup
from core.models import StoreSettings
from core.tests_backup import make_data
from menu.models import Menu

from .models import ApkError, AppKind, AppRelease, AppReleaseChunk, can_download

PASSWORD = "Kidapawan!2026"
APK_BYTES = b"PK\x03\x04" + bytes(range(256)) * 40  # 10,244 bytes


def apk(data=APK_BYTES, name="app-release.apk"):
    return SimpleUploadedFile(name, data, content_type="application/vnd.android.package-archive")


def release(app=AppKind.CUSTOMER, data=APK_BYTES, version="1.0.0"):
    obj = AppRelease.objects.create(app=app, version=version)
    obj.replace_file(apk(data))
    return obj


def body(response):
    return b"".join(response.streaming_content)


def make_users():
    return {
        "rider": User.objects.create_user(
            "rider1", password=PASSWORD, is_rider=True, is_customer=False
        ),
        "customer": User.objects.create_user("maria", password=PASSWORD),
        "store_admin": User.objects.create_user(
            "manager", password=PASSWORD, is_admin=True, is_customer=False
        ),
        "boss": User.objects.create_superuser("boss", "boss@example.com", PASSWORD),
    }


class AppReleaseStorageTests(TestCase):
    @patch("website.models.CHUNK_SIZE", 1000)
    def test_a_file_is_stored_in_chunks_and_read_back_whole(self):
        obj = release()
        self.assertEqual(obj.chunks.count(), 11)
        self.assertEqual(obj.size, len(APK_BYTES))
        self.assertEqual(obj.sha256, hashlib.sha256(APK_BYTES).hexdigest())
        self.assertEqual(b"".join(obj.iter_bytes()), APK_BYTES)

    @patch("website.models.CHUNK_SIZE", 1000)
    def test_a_range_across_chunk_boundaries(self):
        obj = release()
        for start, end in [(0, 0), (999, 1000), (1500, 4321), (10_000, len(APK_BYTES) - 1)]:
            self.assertEqual(
                b"".join(obj.iter_bytes(start, end)), APK_BYTES[start : end + 1], (start, end)
            )

    def test_uploading_again_replaces_the_old_file(self):
        obj = release()
        new = b"PK\x03\x04 version two"
        obj.replace_file(apk(new))
        self.assertEqual(b"".join(obj.iter_bytes()), new)
        self.assertEqual(AppReleaseChunk.objects.count(), 1)

    def test_a_file_that_is_not_an_apk_is_refused_and_the_old_one_kept(self):
        obj = release()
        with self.assertRaises(ApkError):
            obj.replace_file(apk(b"<html>not an app</html>"))
        obj.refresh_from_db()
        self.assertEqual(b"".join(obj.iter_bytes()), APK_BYTES)

    def test_download_name(self):
        self.assertEqual(release(AppKind.RIDER, version="1.2.0").download_name, "RibbonRider-1.2.0.apk")


class CanDownloadTests(TestCase):
    def test_who_gets_which_app(self):
        users = make_users()
        expected = {
            None: {AppKind.CUSTOMER},
            "customer": {AppKind.CUSTOMER},
            "rider": {AppKind.CUSTOMER, AppKind.RIDER},
            "store_admin": {AppKind.CUSTOMER, AppKind.RIDER, AppKind.ADMIN},
            "boss": {AppKind.CUSTOMER, AppKind.RIDER, AppKind.ADMIN},
        }
        for who, apps in expected.items():
            user = users.get(who)
            self.assertEqual({a for a in AppKind if can_download(user, a)}, apps, who)

    def test_a_deactivated_rider_gets_nothing_gated(self):
        rider = make_users()["rider"]
        rider.is_active = False
        self.assertFalse(can_download(rider, AppKind.RIDER))


class HomePageTests(TestCase):
    def test_the_page_shows_the_menu_and_delivery_terms(self):
        store = StoreSettings.load()
        store.delivery_fee = Decimal("20.00")
        store.service_area_radius_km = Decimal("6.00")
        store.save()
        Menu.objects.create(name="Pepperoni Pizza", price=Decimal("299.00"))
        Menu.objects.create(name="Truffle Pizza", price=Decimal("549.50"), is_available=False)

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pepperoni Pizza")
        self.assertContains(response, "₱299")
        self.assertContains(response, "₱549.50")
        self.assertContains(response, "Not available right now", count=1)
        self.assertContains(response, "within 6 km")
        self.assertContains(response, "Delivery is ₱20.")

    def test_without_an_uploaded_app_it_says_soon(self):
        response = self.client.get("/")
        self.assertContains(response, "available to download here soon")
        self.assertNotContains(response, reverse("website:download", args=["customer"]))

    def test_with_an_uploaded_app_it_offers_the_download(self):
        release(version="1.0.3")
        response = self.client.get("/")
        self.assertContains(response, reverse("website:download", args=["customer"]))
        self.assertContains(response, "version 1.0.3")

    def test_the_rider_app_is_never_offered_on_the_home_page(self):
        release(AppKind.RIDER)
        self.assertNotContains(self.client.get("/"), "rider.apk")

    def test_about_and_contact_details_come_from_store_settings(self):
        store = StoreSettings.load()
        store.about = "Since 2019.\n\nFamily run."
        store.address = "Quezon Blvd, Kidapawan"
        store.phone = "0917 123 4567"
        store.save()

        response = self.client.get("/")

        self.assertContains(response, "<p>Since 2019.</p>", html=True)
        self.assertContains(response, "<p>Family run.</p>", html=True)
        self.assertContains(response, "Quezon Blvd, Kidapawan")
        self.assertContains(response, 'href="tel:09171234567"')
        self.assertNotContains(response, "Facebook</dt>")

    def test_menu_text_is_escaped(self):
        Menu.objects.create(name="<script>x</script>", price=Decimal("1"))
        self.assertNotContains(self.client.get("/"), "<script>x</script>")


class DownloadTests(TestCase):
    def setUp(self):
        self.users = make_users()
        self.customer_app = release()
        self.rider_app = release(AppKind.RIDER, data=b"PK\x03\x04rider")

    def test_anyone_can_download_the_customer_app(self):
        response = self.client.get("/download/customer.apk")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body(response), APK_BYTES)
        self.assertEqual(response["Content-Type"], "application/vnd.android.package-archive")
        self.assertEqual(response["Content-Length"], str(len(APK_BYTES)))
        self.assertIn('filename="YellowRibbon-1.0.0.apk"', response["Content-Disposition"])

    def test_a_resumed_download_gets_the_rest(self):
        response = self.client.get("/download/customer.apk", HTTP_RANGE="bytes=100-")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(body(response), APK_BYTES[100:])
        self.assertEqual(response["Content-Range"], f"bytes 100-{len(APK_BYTES) - 1}/{len(APK_BYTES)}")

    def test_suffix_and_bounded_ranges(self):
        self.assertEqual(body(self.client.get("/download/customer.apk", HTTP_RANGE="bytes=-4")), APK_BYTES[-4:])
        self.assertEqual(body(self.client.get("/download/customer.apk", HTTP_RANGE="bytes=2-5")), APK_BYTES[2:6])

    def test_an_impossible_range(self):
        response = self.client.get("/download/customer.apk", HTTP_RANGE=f"bytes={len(APK_BYTES)}-")
        self.assertEqual(response.status_code, 416)
        self.assertEqual(response["Content-Range"], f"bytes */{len(APK_BYTES)}")

    def test_a_garbled_range_sends_the_whole_file(self):
        response = self.client.get("/download/customer.apk", HTTP_RANGE="pages=1-2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body(response), APK_BYTES)

    def test_the_rider_app_needs_a_signed_in_rider(self):
        self.assertRedirects(
            self.client.get("/download/rider.apk"), reverse("website:staff-apps")
        )
        self.client.force_login(self.users["customer"])
        self.assertRedirects(
            self.client.get("/download/rider.apk"), reverse("website:staff-apps")
        )
        self.client.force_login(self.users["rider"])
        response = self.client.get("/download/rider.apk")
        self.assertEqual(body(response), b"PK\x03\x04rider")

    def test_the_admin_app_needs_a_store_admin(self):
        release(AppKind.ADMIN, data=b"PK\x03\x04admin")
        self.client.force_login(self.users["rider"])
        self.assertEqual(self.client.get("/download/admin.apk").status_code, 302)
        self.client.force_login(self.users["store_admin"])
        self.assertEqual(body(self.client.get("/download/admin.apk")), b"PK\x03\x04admin")

    def test_an_app_with_no_upload_yet_is_not_found(self):
        self.client.force_login(self.users["boss"])
        self.assertEqual(self.client.get("/download/admin.apk").status_code, 404)

    def test_an_unknown_app_is_not_found(self):
        self.assertEqual(self.client.get("/download/secret.apk").status_code, 404)


class StaffSignInTests(TestCase):
    url = reverse("website:staff-apps")

    def setUp(self):
        self.users = make_users()
        release(AppKind.RIDER, version="2.0.0")

    def sign_in(self, username, password=PASSWORD):
        return self.client.post(self.url, {"username": username, "password": password})

    def test_the_page_asks_for_a_sign_in(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Rider sign-in")
        self.assertContains(response, 'name="password"')

    def test_a_rider_signs_in_and_sees_the_rider_app(self):
        self.assertRedirects(self.sign_in("rider1"), self.url)
        response = self.client.get(self.url)
        self.assertContains(response, "Signed in as <strong>rider1</strong>", html=False)
        self.assertContains(response, reverse("website:download", args=["rider"]))
        self.assertContains(response, "Version 2.0.0")
        self.assertNotContains(response, "admin.apk")

    def test_a_store_admin_also_sees_the_admin_app(self):
        self.sign_in("manager")
        response = self.client.get(self.url)
        self.assertContains(response, "Ribbon Rider")
        self.assertContains(response, "Ribbon Admin")
        self.assertContains(response, "Not uploaded yet")

    def test_a_customer_account_is_not_signed_in(self):
        response = self.sign_in("maria")
        self.assertContains(response, "That is a customer account")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_wrong_password(self):
        response = self.sign_in("rider1", "nope")
        self.assertContains(response, "Wrong username or password")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_sign_out(self):
        self.sign_in("rider1")
        self.assertRedirects(self.client.post(reverse("website:sign-out")), self.url)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_sign_out_is_post_only(self):
        self.sign_in("rider1")
        self.assertEqual(self.client.get(reverse("website:sign-out")).status_code, 405)

    def test_csrf_is_enforced_on_sign_in(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(self.url, {"username": "rider1", "password": PASSWORD})
        self.assertEqual(response.status_code, 403)

    def test_a_rider_cannot_reach_the_admin(self):
        self.sign_in("rider1")
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)


class AppReleaseAdminTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        self.client.force_login(self.boss)

    def test_upload_through_the_admin(self):
        response = self.client.post(
            reverse("admin:website_apprelease_add"),
            {"app": "customer", "version": "1.0.4", "apk": apk()},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        obj = AppRelease.objects.get(app="customer")
        self.assertEqual(obj.version, "1.0.4")
        self.assertEqual(obj.size, len(APK_BYTES))
        self.assertEqual(obj.uploaded_by, "boss")
        self.assertContains(response, "It is on the website now")

    def test_a_non_apk_is_refused_with_a_message(self):
        response = self.client.post(
            reverse("admin:website_apprelease_add"),
            {"app": "customer", "version": "1", "apk": apk(b"hello")},
        )
        self.assertContains(response, "That is not an APK file")
        self.assertFalse(AppRelease.objects.exists())

    def test_a_new_release_needs_a_file(self):
        response = self.client.post(
            reverse("admin:website_apprelease_add"), {"app": "customer", "version": "1"}
        )
        self.assertContains(response, "Choose the APK file to upload")

    def test_changing_only_the_version_keeps_the_file(self):
        obj = release()
        self.client.post(
            reverse("admin:website_apprelease_change", args=[obj.pk]), {"version": "1.0.1"}
        )
        obj.refresh_from_db()
        self.assertEqual(obj.version, "1.0.1")
        self.assertEqual(b"".join(obj.iter_bytes()), APK_BYTES)

    def test_list_and_change_pages_load(self):
        obj = release()
        self.assertContains(self.client.get(reverse("admin:website_apprelease_changelist")), "1.0.0")
        self.assertEqual(
            self.client.get(reverse("admin:website_apprelease_change", args=[obj.pk])).status_code, 200
        )


class StoreSettingsWebsiteFieldsTests(TestCase):
    def test_the_admin_app_api_reads_and_writes_them(self):
        boss = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        client = APIClient()
        client.force_authenticate(boss)
        response = client.patch(
            reverse("admin-store-settings"),
            {"about": "Hello", "opening_hours": "Daily, 10 AM to 9 PM", "facebook_url": "not a url"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        response = client.patch(
            reverse("admin-store-settings"),
            {"about": "Hello", "opening_hours": "Daily, 10 AM to 9 PM"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["opening_hours"], "Daily, 10 AM to 9 PM")
        self.assertEqual(response.data["phone"], "")


class BackupLeavesAppsAloneTests(TransactionTestCase):
    """APKs are too big for the backup, so a restore must not delete them."""

    def test_apks_are_not_in_the_backup_and_survive_a_restore(self):
        data = make_data()
        release()
        raw = backup.build_backup()
        rows = json.loads(gzip.decompress(raw))
        self.assertFalse([r for r in rows if r["model"].startswith("website.")])
        self.assertNotIn("App downloads", [name for name, _ in backup.record_counts()])

        client = Client()
        client.force_login(data["boss"])
        response = client.post(
            reverse("backup-restore"),
            {"backup": SimpleUploadedFile("b.json.gz", raw), "confirm": "RESTORE"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(AppRelease.objects.get().iter_bytes()), APK_BYTES)

    def test_an_uploaded_apk_does_not_make_a_new_database_count_as_used(self):
        release()
        self.assertTrue(backup.database_is_empty())
