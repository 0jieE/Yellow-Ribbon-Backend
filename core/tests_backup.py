"""
The admin Backup page and the restore command.

The round-trip tests are the point: a backup nobody has restored is a hope,
not a backup. They build realistic data, dump it, wipe the database, load it
back and check the rows, the passwords and the order-number sequence.
"""

import gzip
import json
import os
import tempfile
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import Permission
from django.core.management import CommandError, call_command
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Customer, DeviceToken, Rider, User
from core.backup import (
    BackupError,
    _trailing_number,
    build_backup,
    database_is_empty,
    read_backup,
    restore_backup,
)
from delivery.models import Delivery
from menu.models import Menu
from orders.constants import OrderStatus
from orders.models import Order, OrderItem, OrderStatusLog, next_order_number
from orders.tests_admin import make_order
from payments.models import Invoice, next_invoice_number

PASSWORD = "Kidapawan!2026"


def make_data():
    """A small but complete shop: every table that matters has a row."""
    boss = User.objects.filter(username="boss").first()
    if boss is None:
        boss = User.objects.create_superuser("boss", "boss@example.com", "BossPass!2026")
    boss.user_permissions.add(Permission.objects.get(codename="view_order"))

    maria = User.objects.create_user(
        "maria", "maria@example.com", PASSWORD, first_name="Maria", phone="09171234567"
    )
    customer = Customer.objects.create(user=maria, default_address="Quezon Blvd")
    DeviceToken.objects.create(user=maria, fcm_token="token-abc", platform="android")

    rico = User.objects.create_user(
        "rico", "rico@example.com", PASSWORD, is_rider=True, is_customer=False
    )
    rider = Rider.objects.create(user=rico, vehicle_type="Motorcycle", is_available=True)

    menu = Menu.objects.create(name="Pepperoni", price=Decimal("299.00"))
    order = make_order(
        customer, order_number="YR-0007", status=OrderStatus.DELIVERED, delivered_at=timezone.now()
    )
    OrderItem.objects.create(
        order=order,
        menu=menu,
        quantity=1,
        unit_price=Decimal("299.00"),
        line_subtotal=Decimal("299.00"),
    )
    OrderStatusLog.objects.create(order=order, status=OrderStatus.DELIVERED, changed_by=boss)
    Delivery.objects.create(order=order, rider=rider, rider_earnings=Decimal("30.00"))
    Invoice.objects.create(
        order=order,
        invoice_number="INV-0003",
        amount_paid=Decimal("329.00"),
        paymongo_payment_id="pay_123",
        paid_at=timezone.now(),
    )
    return {"boss": boss, "maria": maria, "order": order}


def write_backup(directory, name="backup.json.gz", data=None):
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(data if data is not None else build_backup())
    return path


def table_counts():
    from core.backup import _backed_up_models

    return {m._meta.label: m.objects.count() for m in _backed_up_models()}


class BackupPageTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        self.client.force_login(self.boss)
        self.url = reverse("backup")

    def test_page_explains_the_backup(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Download backup")
        self.assertContains(response, "restore_backup")

    def test_opening_the_page_downloads_nothing(self):
        response = self.client.get(self.url)
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))

    def test_post_downloads_a_gzipped_json_backup(self):
        make_data()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/gzip")
        self.assertRegex(
            response["Content-Disposition"],
            r'attachment; filename="yellowribbon-backup-\d{8}-\d{4}\.json\.gz"',
        )
        rows = json.loads(gzip.decompress(response.content))
        models = {row["model"] for row in rows}
        for expected in (
            "accounts.user",
            "accounts.customer",
            "accounts.rider",
            "accounts.devicetoken",
            "menu.menu",
            "orders.order",
            "orders.orderitem",
            "orders.orderstatuslog",
            "delivery.delivery",
            "payments.invoice",
        ):
            self.assertIn(expected, models)

    def test_things_migrate_recreates_are_left_out(self):
        self.client.get(reverse("admin:index"))  # creates a session
        rows = json.loads(gzip.decompress(self.client.post(self.url).content))
        models = {row["model"] for row in rows}
        for excluded in (
            "contenttypes.contenttype",
            "auth.permission",
            "sessions.session",
            "admin.logentry",
        ):
            self.assertNotIn(excluded, models)

    def test_page_lists_row_counts(self):
        make_data()
        response = self.client.get(self.url)
        self.assertContains(response, "Orders")
        self.assertContains(response, "Menus")

    def test_dashboard_offers_the_button_to_superusers(self):
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, "Back up now")
        self.assertContains(response, self.url)

    def test_sidebar_links_to_it(self):
        response = self.client.get(reverse("admin:orders_order_changelist"))
        self.assertContains(response, "Database backup")


class BackupAccessTests(TestCase):
    def setUp(self):
        self.url = reverse("backup")

    def test_anonymous_is_sent_to_login(self):
        for method in (self.client.get, self.client.post):
            response = method(self.url)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login/", response["Location"])

    def test_staff_who_is_not_a_superuser_is_refused(self):
        """The file holds password hashes; staff alone must not get it."""
        staff = User.objects.create_user(
            "clerk", "clerk@example.com", PASSWORD, is_staff=True
        )
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url).status_code, 403)

    def test_customers_and_riders_are_refused(self):
        for flags in ({"is_customer": True}, {"is_rider": True, "is_customer": False}):
            user = User.objects.create_user(
                f"u{len(flags)}{flags.get('is_rider', 0)}",
                f"u{len(flags)}{flags.get('is_rider', 0)}@example.com",
                PASSWORD,
                **flags,
            )
            self.client.force_login(user)
            self.assertIn(self.client.post(self.url).status_code, (302, 403))
            self.assertNotEqual(self.client.post(self.url).status_code, 200)

    def test_staff_do_not_see_the_backup_card_or_link(self):
        staff = User.objects.create_user(
            "clerk", "clerk@example.com", PASSWORD, is_staff=True, is_superuser=False
        )
        staff.user_permissions.add(Permission.objects.get(codename="view_order"))
        self.client.force_login(staff)
        response = self.client.get(reverse("admin:index"))
        self.assertNotContains(response, "Back up now")


class TrailingNumberTests(TestCase):
    def test_reads_the_number_off_the_end(self):
        self.assertEqual(_trailing_number("YR-0043"), 43)
        self.assertEqual(_trailing_number("INV-0003"), 3)
        self.assertEqual(_trailing_number("YR-T0007"), 7)

    def test_no_digits_is_zero(self):
        self.assertEqual(_trailing_number("abc"), 0)
        self.assertEqual(_trailing_number(""), 0)


class RoundTripTests(TransactionTestCase):
    """Dump, wipe, restore. TransactionTestCase because restore flushes."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def wipe(self):
        call_command("flush", interactive=False, verbosity=0)

    def test_everything_comes_back(self):
        make_data()
        before = table_counts()
        path = write_backup(self.dir.name)

        self.wipe()
        self.assertTrue(database_is_empty())
        restore_backup(path)

        self.assertEqual(table_counts(), before)

    def test_values_survive_not_just_row_counts(self):
        make_data()
        path = write_backup(self.dir.name)

        self.wipe()
        restore_backup(path)

        order = Order.objects.get(order_number="YR-0007")
        self.assertEqual(order.total, Decimal("329.00"))
        self.assertEqual(order.status, OrderStatus.DELIVERED)
        self.assertEqual(order.items.get().unit_price, Decimal("299.00"))
        self.assertEqual(order.invoice.invoice_number, "INV-0003")
        self.assertEqual(order.delivery.rider_earnings, Decimal("30.00"))
        self.assertEqual(order.customer.user.username, "maria")
        self.assertEqual(order.customer.default_address, "Quezon Blvd")
        self.assertEqual(DeviceToken.objects.get().fcm_token, "token-abc")

    def test_passwords_still_work(self):
        make_data()
        path = write_backup(self.dir.name)

        self.wipe()
        restore_backup(path)

        self.assertTrue(User.objects.get(username="boss").check_password("BossPass!2026"))
        self.assertTrue(User.objects.get(username="maria").check_password(PASSWORD))

    def test_user_permissions_are_matched_by_name_not_id(self):
        make_data()
        path = write_backup(self.dir.name)

        self.wipe()
        restore_backup(path)

        boss = User.objects.get(username="boss")
        self.assertTrue(boss.user_permissions.filter(codename="view_order").exists())

    def test_order_and_invoice_numbers_carry_on(self):
        """Otherwise the next order is YR-0001 again and collides."""
        make_data()
        path = write_backup(self.dir.name)

        self.wipe()
        _, sequences = restore_backup(path)

        self.assertEqual(next_order_number(), "YR-0008")
        self.assertEqual(next_invoice_number(), "INV-0004")
        self.assertEqual(sequences["orders_order_number_seq"], 7)

    def test_an_empty_backup_restarts_numbering(self):
        make_data()
        path = write_backup(self.dir.name)
        self.wipe()
        restore_backup(path)
        self.wipe()
        empty = write_backup(self.dir.name, "empty.json.gz", data=gzip.compress(b"[]"))

        restore_backup(empty)

        self.assertEqual(next_order_number(), "YR-0001")

    def test_refuses_a_database_that_already_has_data(self):
        make_data()
        path = write_backup(self.dir.name)

        with self.assertRaisesMessage(BackupError, "already has data"):
            restore_backup(path)

    def test_flush_replaces_what_is_there(self):
        make_data()
        path = write_backup(self.dir.name)
        User.objects.create_user("extra", "extra@example.com", PASSWORD)

        restore_backup(path, flush=True)

        self.assertFalse(User.objects.filter(username="extra").exists())
        self.assertTrue(User.objects.filter(username="maria").exists())

    def test_the_admin_created_by_hand_on_a_new_database_does_not_block_it(self):
        """The real situation: a fresh Render DB with only `createsuperuser`."""
        make_data()
        path = write_backup(self.dir.name)
        self.wipe()
        User.objects.create_superuser("admin", "admin@example.com", "admin123")

        restore_backup(path, flush=True)

        self.assertTrue(User.objects.filter(username="boss").exists())
        self.assertFalse(User.objects.filter(username="admin").exists())

    def test_plain_json_is_accepted_too(self):
        make_data()
        text = gzip.decompress(build_backup())
        path = write_backup(self.dir.name, "plain.json", data=text)
        before = table_counts()

        self.wipe()
        restore_backup(path)

        self.assertEqual(table_counts(), before)

    def test_a_failed_restore_loads_nothing(self):
        """A bad row must not leave a half-restored database."""
        make_data()
        rows = json.loads(gzip.decompress(build_backup()))
        rows.append({"model": "orders.order", "pk": 99999, "fields": {"nonsense": 1}})
        path = write_backup(
            self.dir.name, data=gzip.compress(json.dumps(rows).encode("utf-8"))
        )
        self.wipe()

        with self.assertRaises(Exception):
            restore_backup(path)

        self.assertTrue(database_is_empty())

    def test_command_restores_and_reports(self):
        make_data()
        path = write_backup(self.dir.name)
        self.wipe()
        out = StringIO()

        call_command("restore_backup", path, stdout=out)

        self.assertIn("Restored", out.getvalue())
        self.assertIn("orders_order_number_seq: next number is 8", out.getvalue())
        self.assertTrue(User.objects.filter(username="maria").exists())

    def test_command_refuses_a_non_empty_database_without_flush(self):
        make_data()
        path = write_backup(self.dir.name)

        with self.assertRaisesMessage(CommandError, "already has data"):
            call_command("restore_backup", path)

        call_command("restore_backup", path, "--flush", stdout=StringIO())

    def test_view_download_restores_cleanly(self):
        """End to end: what the button hands out is what the command accepts."""
        data = make_data()
        self.client.force_login(data["boss"])
        response = self.client.post(reverse("backup"))
        path = write_backup(self.dir.name, data=response.content)
        before = table_counts()

        self.wipe()
        restore_backup(path)

        self.assertEqual(table_counts(), before)


class ReadBackupTests(TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, data):
        return write_backup(self.dir.name, "x.json.gz", data=data)

    def test_missing_file(self):
        with self.assertRaises(BackupError):
            read_backup(os.path.join(self.dir.name, "nope.json.gz"))

    def test_not_json(self):
        with self.assertRaises(BackupError):
            read_backup(self.path(b"this is not a backup"))

    def test_json_that_is_not_a_dump(self):
        with self.assertRaises(BackupError):
            read_backup(self.path(gzip.compress(b'{"hello": 1}')))
        with self.assertRaises(BackupError):
            read_backup(self.path(gzip.compress(b"[1, 2, 3]")))

    def test_counts_rows(self):
        data = gzip.compress(b'[{"model": "menu.menu", "pk": 1, "fields": {}}]')
        self.assertEqual(read_backup(self.path(data))[1], 1)
