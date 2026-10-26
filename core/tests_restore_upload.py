"""
Restoring a backup by uploading it on the admin's Backup page.

This replaces the whole database from a web form, so the tests are mostly about
what must NOT happen: a bad file changing anything, a backup that would lock
everyone out, a half-finished restore, an unauthorised caller.

TransactionTestCase because a restore truncates tables.
"""

import gzip
import json
import tempfile
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import User
from core import backup
from core.backup import build_backup
from core.tests_backup import make_data, table_counts
from menu.models import Menu
from orders.models import Order, next_order_number
from orders.tests_admin import make_order

PASSWORD = "Kidapawan!2026"
BOSS_PASSWORD = "BossPass!2026"


def upload(raw, name="backup.json.gz"):
    return SimpleUploadedFile(name, raw, content_type="application/gzip")


def as_gz(rows):
    return gzip.compress(json.dumps(rows).encode("utf-8"))


class RestoreBase(TransactionTestCase):
    url = None

    def setUp(self):
        self.url = reverse("backup-restore")
        self.data = make_data()
        self.boss = self.data["boss"]
        self.client.force_login(self.boss)

    def restore(self, raw, confirm="RESTORE", name="backup.json.gz", follow=False):
        return self.client.post(
            self.url, {"backup": upload(raw, name), "confirm": confirm}, follow=follow
        )

    def flash(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def messages_after(self, response):
        """Messages queued by a refusal, read from the page it redirects to."""
        followed = self.client.get(response["Location"])
        return [str(m) for m in followed.context["messages"]]


class SuccessfulRestoreTests(RestoreBase):
    def test_the_backup_replaces_everything(self):
        raw = build_backup()
        before = table_counts()
        # Change the live data after the backup was taken.
        User.objects.create_user("intruder", "x@example.com", PASSWORD)
        Menu.objects.update(name="Renamed", price=Decimal("1.00"))
        Order.objects.update(total=Decimal("9999.00"))
        make_order(self.data["order"].customer, order_number="YR-0020")

        response = self.restore(raw)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(table_counts(), before)
        self.assertFalse(User.objects.filter(username="intruder").exists())
        self.assertFalse(Order.objects.filter(order_number="YR-0020").exists())
        self.assertEqual(Order.objects.get(order_number="YR-0007").total, Decimal("329.00"))
        menu = Menu.objects.get()
        self.assertEqual((menu.name, menu.price), ("Pepperoni", Decimal("299.00")))

    def test_it_answers_with_a_standalone_page_that_explains_the_signout(self):
        response = self.restore(build_backup())

        self.assertTemplateUsed(response, "admin/restore_done.html")
        self.assertContains(response, "Backup restored")
        self.assertContains(response, "signed out")
        self.assertContains(response, "from the backup")
        self.assertContains(response, "/admin/login/")
        self.assertContains(response, "12 rows")

    def test_everyone_is_signed_out_afterwards(self):
        self.restore(build_backup())
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_the_backups_own_passwords_work_afterwards(self):
        self.restore(build_backup())
        fresh = Client()
        self.assertTrue(fresh.login(username="boss", password=BOSS_PASSWORD))
        self.assertTrue(Client().login(username="maria", password=PASSWORD))

    def test_order_numbering_carries_on_from_the_backup(self):
        raw = build_backup()
        # A later order pushes the live sequence well past the backup's.
        make_order(self.data["order"].customer, order_number="YR-0020")
        next_order_number()
        self.restore(raw)
        self.assertEqual(next_order_number(), "YR-0008")

    def test_a_plain_json_file_works_too(self):
        raw = gzip.decompress(build_backup())
        before = table_counts()
        User.objects.create_user("intruder", "x@example.com", PASSWORD)

        response = self.restore(raw, name="backup.json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(table_counts(), before)

    def test_restoring_a_backup_onto_an_empty_database_works(self):
        raw = build_backup()
        call_command("flush", interactive=False, verbosity=0)
        self.assertEqual(User.objects.count(), 0)
        # Nobody is signed in, so do it through the command the page mirrors.
        call_command("restore_backup", self._write(raw), stdout=StringIO())
        self.assertTrue(User.objects.filter(username="boss").exists())

    def _write(self, raw):
        handle = tempfile.NamedTemporaryFile(suffix=".json.gz", delete=False)
        handle.write(raw)
        handle.close()
        return handle.name


class RefusalsChangeNothingTests(RestoreBase):
    def assert_untouched(self, before):
        self.assertEqual(table_counts(), before)
        self.assertTrue(User.objects.filter(username="boss").exists())

    def test_without_the_confirmation_word_nothing_happens(self):
        before = table_counts()
        for confirm in ("", "restore", "yes", "RESTORE NOW"):
            response = self.restore(build_backup(), confirm=confirm)
            self.assertEqual(response.status_code, 302, confirm)
            self.assertIn("Type RESTORE", " ".join(self.messages_after(response)))
        self.assert_untouched(before)

    def test_the_confirmation_word_is_trimmed_not_guessed(self):
        response = self.restore(build_backup(), confirm="  RESTORE  ")
        self.assertEqual(response.status_code, 200)

    def test_no_file_chosen(self):
        before = table_counts()
        response = self.client.post(self.url, {"confirm": "RESTORE"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("Choose the backup file", " ".join(self.messages_after(response)))
        self.assert_untouched(before)

    def test_a_file_that_is_not_a_backup(self):
        before = table_counts()
        response = self.restore(b"this is a holiday photo, not a backup")
        self.assertEqual(response.status_code, 302)
        self.assertIn("is not a Yellow Ribbon backup", " ".join(self.messages_after(response)))
        self.assert_untouched(before)

    def test_json_that_is_not_a_data_dump(self):
        before = table_counts()
        for payload in ({"hello": 1}, [1, 2, 3], "text"):
            response = self.restore(as_gz(payload))
            self.assertEqual(response.status_code, 302, payload)
        self.assert_untouched(before)

    def test_an_empty_backup_would_wipe_everything_so_it_is_refused(self):
        before = table_counts()
        response = self.restore(as_gz([]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("no active admin account", " ".join(self.messages_after(response)))
        self.assert_untouched(before)

    def test_a_backup_with_no_admin_who_can_sign_in_is_refused(self):
        """Otherwise the restore succeeds and nobody can ever get back in."""
        rows = json.loads(gzip.decompress(build_backup()))
        for row in rows:
            if row["model"] == "accounts.user":
                row["fields"]["is_staff"] = False
        before = table_counts()

        response = self.restore(as_gz(rows))

        self.assertEqual(response.status_code, 302)
        self.assertIn("lock everyone out", " ".join(self.messages_after(response)))
        self.assert_untouched(before)

    def test_an_only_superuser_who_is_deactivated_does_not_count(self):
        rows = json.loads(gzip.decompress(build_backup()))
        for row in rows:
            if row["model"] == "accounts.user" and row["fields"]["is_superuser"]:
                row["fields"]["is_active"] = False
        before = table_counts()

        response = self.restore(as_gz(rows))

        self.assertEqual(response.status_code, 302)
        self.assert_untouched(before)

    def test_a_backup_that_fails_halfway_leaves_the_current_data_intact(self):
        """The wipe and the load are one transaction: a bad row undoes the wipe."""
        rows = json.loads(gzip.decompress(build_backup()))
        rows.append({"model": "orders.order", "pk": 99999, "fields": {"nonsense": 1}})
        before = table_counts()
        order_numbers = set(Order.objects.values_list("order_number", flat=True))

        response = self.restore(as_gz(rows))

        self.assertEqual(response.status_code, 302)
        self.assertIn("nothing was changed", " ".join(self.messages_after(response)))
        self.assert_untouched(before)
        self.assertEqual(set(Order.objects.values_list("order_number", flat=True)), order_numbers)
        # And the person who tried is still signed in, with their session intact.
        self.assertEqual(self.client.get(reverse("backup")).status_code, 200)

    def test_a_row_pointing_at_nothing_also_rolls_back(self):
        rows = json.loads(gzip.decompress(build_backup()))
        for row in rows:
            if row["model"] == "orders.order":
                row["fields"]["customer"] = 424242  # no such customer
        before = table_counts()

        response = self.restore(as_gz(rows))

        self.assertEqual(response.status_code, 302)
        self.assert_untouched(before)

    def test_a_file_over_the_size_limit(self):
        before = table_counts()
        with patch("core.backup.MAX_UPLOAD_BYTES", 10):
            response = self.restore(build_backup())
        self.assertEqual(response.status_code, 302)
        self.assertIn("over", " ".join(self.messages_after(response)))
        self.assert_untouched(before)

    def test_a_small_file_that_unpacks_enormous_is_refused(self):
        bomb = gzip.compress(b"[" + b" " * 5000 + b"]")
        before = table_counts()
        with patch("core.backup.MAX_DECOMPRESSED_BYTES", 1000):
            response = self.restore(bomb)
        self.assertEqual(response.status_code, 302)
        self.assertIn("too large once unpacked", " ".join(self.messages_after(response)))
        self.assert_untouched(before)


class AccessTests(RestoreBase):
    def test_anonymous_is_sent_to_login_and_nothing_changes(self):
        before = table_counts()
        response = Client().post(self.url, {"backup": upload(build_backup()), "confirm": "RESTORE"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])
        self.assertEqual(table_counts(), before)

    def test_staff_who_are_not_superusers_are_refused(self):
        clerk = User.objects.create_user("clerk", "c@example.com", PASSWORD, is_staff=True)
        client = Client()
        client.force_login(clerk)
        before = table_counts()

        response = client.post(self.url, {"backup": upload(build_backup()), "confirm": "RESTORE"})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(table_counts(), before)

    def test_it_cannot_be_triggered_by_a_get(self):
        for method in (self.client.get, self.client.put, self.client.delete):
            self.assertEqual(method(self.url).status_code, 405)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.boss)
        before = table_counts()
        response = client.post(self.url, {"backup": upload(build_backup()), "confirm": "RESTORE"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(table_counts(), before)


class PageTests(RestoreBase):
    def test_the_backup_page_has_the_restore_form(self):
        response = self.client.get(reverse("backup"))

        self.assertContains(response, "Restore from a backup file")
        self.assertContains(response, reverse("backup-restore"))
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertContains(response, 'name="backup"')
        self.assertContains(response, "Type RESTORE to confirm")
        self.assertContains(response, "deletes everything currently here")
        self.assertContains(response, "signed out")

    def test_the_form_asks_for_confirmation_in_the_browser_too(self):
        response = self.client.get(reverse("backup"))
        self.assertContains(response, "return confirm(")

    def test_a_refusal_is_shown_on_the_page(self):
        response = self.restore(build_backup(), confirm="nope", follow=True)
        self.assertContains(response, "Nothing was restored")

    def test_download_still_works(self):
        response = self.client.post(reverse("backup"))
        self.assertEqual(response["Content-Type"], "application/gzip")


class CommandIsNowAllOrNothingTests(RestoreBase):
    def test_flush_with_a_bad_file_keeps_the_existing_data(self):
        """Before, a failed load after --flush left an empty database."""
        rows = json.loads(gzip.decompress(build_backup()))
        rows.append({"model": "orders.order", "pk": 99999, "fields": {"nonsense": 1}})
        before = table_counts()

        with self.assertRaises(CommandError) as caught:
            call_command("restore_backup", self._file(as_gz(rows)), "--flush", stdout=StringIO())

        self.assertIn("nothing was changed", str(caught.exception))
        self.assertEqual(table_counts(), before)

    def _file(self, raw):
        handle = tempfile.NamedTemporaryFile(suffix=".json.gz", delete=False)
        handle.write(raw)
        handle.close()
        return handle.name


class ParseBackupTests(RestoreBase):
    def test_parse_returns_text_and_rows(self):
        text, rows = backup.parse_backup(build_backup())
        self.assertIsInstance(text, str)
        self.assertGreater(len(rows), 0)

    def test_has_admin_login(self):
        _, rows = backup.parse_backup(build_backup())
        self.assertTrue(backup.has_admin_login(rows))
        self.assertFalse(backup.has_admin_login([]))
        self.assertFalse(
            backup.has_admin_login(
                [{"model": "accounts.user", "fields": {"is_superuser": True, "is_staff": True, "is_active": False}}]
            )
        )
        self.assertFalse(
            backup.has_admin_login(
                [{"model": "accounts.user", "fields": {"is_superuser": False, "is_staff": True, "is_active": True}}]
            )
        )
