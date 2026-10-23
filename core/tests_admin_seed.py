"""Django admin registration coverage and the seed command."""

from decimal import Decimal
from io import StringIO

from django.apps import apps
from django.contrib import admin
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.models import Customer, Rider, User
from core.management.commands.seed_demo import (
    CUSTOMER_USERNAME,
    MENU_ITEMS,
    RIDER_USERNAME,
)
from menu.models import Menu

LOCAL_APPS = {"accounts", "menu", "orders", "delivery", "payments", "core"}


def listed_admins():
    """(model, admin) for every local model that has a changelist of its own.

    A singleton such as StoreSettings is edited in place: its list redirects to
    the one row, so list_display, search and filters do not apply to it.
    """
    return [
        (model, site_admin)
        for model, site_admin in admin.site._registry.items()
        if model._meta.app_label in LOCAL_APPS and not getattr(site_admin, "is_singleton", False)
    ]


class AdminRegistrationTests(TestCase):
    """Section 13: every model registered — the admin is the demo fallback."""

    def local_models(self):
        return [m for m in apps.get_models() if m._meta.app_label in LOCAL_APPS]

    def test_every_model_is_registered(self):
        unregistered = [
            f"{m._meta.app_label}.{m.__name__}"
            for m in self.local_models()
            if m not in admin.site._registry
        ]
        self.assertEqual(unregistered, [], f"Not registered in admin: {unregistered}")

    def test_expected_models_are_present(self):
        registered = {m.__name__ for m in admin.site._registry}
        for name in (
            "User",
            "Customer",
            "Rider",
            "DeviceToken",
            "Menu",
            "Order",
            "OrderItem",
            "OrderStatusLog",
            "Delivery",
            "Invoice",
        ):
            self.assertIn(name, registered, f"{name} is missing from the admin")

    def test_every_admin_has_a_list_display(self):
        for model, site_admin in listed_admins():
            self.assertGreaterEqual(
                len(site_admin.list_display), 2, f"{model.__name__} list_display"
            )

    def test_every_admin_has_search_fields(self):
        for model, site_admin in listed_admins():
            self.assertTrue(
                site_admin.search_fields, f"{model.__name__} has no search_fields"
            )

    def test_every_admin_has_filters(self):
        for model, site_admin in listed_admins():
            self.assertTrue(
                site_admin.list_filter, f"{model.__name__} has no list_filter"
            )

    def test_site_is_branded(self):
        self.assertEqual(admin.site.site_header, "Yellow Ribbon Pizza")


class AdminPageTests(APITestCase):
    """The changelists actually render — a broken one only shows up at runtime."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="boss", email="boss@example.com", password="Kidapawan!2026"
        )
        self.client.force_login(self.admin)

    def test_index_loads(self):
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)

    def test_every_changelist_loads(self):
        for model, _ in listed_admins():
            url = reverse(
                f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist"
            )
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, f"{url} returned {response.status_code}")

    def test_changelists_survive_a_search(self):
        for model, _ in listed_admins():
            url = reverse(
                f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist"
            )
            response = self.client.get(url, {"q": "maria"})
            self.assertEqual(response.status_code, 200, url)


class SeedCommandTests(TestCase):
    def run_seed(self, *args):
        out = StringIO()
        call_command("seed_demo", *args, stdout=out)
        return out.getvalue()

    def test_creates_menu_rider_and_customer(self):
        self.run_seed()

        self.assertEqual(Menu.objects.count(), len(MENU_ITEMS))
        rider = User.objects.get(username=RIDER_USERNAME)
        self.assertTrue(rider.is_rider)
        self.assertFalse(rider.is_customer)
        self.assertTrue(Rider.objects.filter(user=rider).exists())

        customer = User.objects.get(username=CUSTOMER_USERNAME)
        self.assertTrue(customer.is_customer)
        self.assertFalse(customer.is_rider)
        self.assertTrue(Customer.objects.filter(user=customer).exists())

    def test_running_twice_duplicates_nothing(self):
        self.run_seed()
        counts = (
            Menu.objects.count(),
            User.objects.count(),
            Rider.objects.count(),
            Customer.objects.count(),
        )

        self.run_seed()
        self.run_seed()

        self.assertEqual(
            counts,
            (
                Menu.objects.count(),
                User.objects.count(),
                Rider.objects.count(),
                Customer.objects.count(),
            ),
        )

    def test_second_run_reports_nothing_created(self):
        self.run_seed()
        output = self.run_seed()
        self.assertIn(f"{len(MENU_ITEMS)} already correct", output)

    def test_pre_existing_accounts_are_untouched(self):
        """The user's own accounts must survive the seed unchanged."""
        mine = User.objects.create_superuser(
            username="boss", email="boss@example.com", password="MyOwnPassword!1"
        )
        other = User.objects.create_user(
            username="jen", email="jen@example.com", password="MyOwnPassword!1"
        )
        Customer.objects.create(user=other, default_address="My real address")

        self.run_seed()
        self.run_seed("--reset-passwords")

        mine.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(mine.check_password("MyOwnPassword!1"))
        self.assertTrue(other.check_password("MyOwnPassword!1"))
        self.assertEqual(other.customer.default_address, "My real address")
        self.assertEqual(mine.email, "boss@example.com")

    def test_existing_menu_prices_are_not_overwritten(self):
        self.run_seed()
        item = Menu.objects.get(name="Pepperoni Pizza")
        item.price = Decimal("999.00")
        item.is_available = False
        item.save()

        self.run_seed()

        item.refresh_from_db()
        self.assertEqual(item.price, Decimal("999.00"))
        self.assertFalse(item.is_available)

    def test_a_deleted_menu_item_is_recreated(self):
        self.run_seed()
        Menu.objects.filter(name="Garlic Bread").delete()

        self.run_seed()
        self.assertTrue(Menu.objects.filter(name="Garlic Bread").exists())

    def test_default_password_works(self):
        self.run_seed()
        rider = User.objects.get(username=RIDER_USERNAME)
        self.assertTrue(rider.check_password("Kidapawan!2026"))

    def test_password_can_be_overridden(self):
        self.run_seed("--password", "Another!Pass99")
        self.assertTrue(
            User.objects.get(username=RIDER_USERNAME).check_password("Another!Pass99")
        )

    def test_rerun_leaves_the_demo_password_alone(self):
        self.run_seed("--password", "First!Pass99")
        self.run_seed("--password", "Second!Pass99")

        self.assertTrue(
            User.objects.get(username=RIDER_USERNAME).check_password("First!Pass99")
        )

    def test_reset_passwords_flag_changes_it(self):
        self.run_seed("--password", "First!Pass99")
        self.run_seed("--password", "Second!Pass99", "--reset-passwords")

        self.assertTrue(
            User.objects.get(username=RIDER_USERNAME).check_password("Second!Pass99")
        )

    def test_dry_run_writes_nothing(self):
        output = self.run_seed("--dry-run")

        self.assertEqual(Menu.objects.count(), 0)
        self.assertFalse(User.objects.filter(username=RIDER_USERNAME).exists())
        self.assertIn("nothing was written", output)

    def test_seed_includes_an_unavailable_item(self):
        """So the Unavailable badge has something to render in the demo."""
        self.run_seed()
        self.assertTrue(Menu.objects.filter(is_available=False).exists())
        self.assertTrue(Menu.objects.filter(is_available=True).count() >= 5)

    def test_rider_starts_off_duty(self):
        self.run_seed()
        self.assertFalse(Rider.objects.get(user__username=RIDER_USERNAME).is_available)

    def test_customer_address_is_inside_the_service_area(self):
        from core.geofence import is_within_service_area

        self.run_seed()
        profile = Customer.objects.get(user__username=CUSTOMER_USERNAME)
        self.assertTrue(
            is_within_service_area(profile.default_latitude, profile.default_longitude)
        )

    def test_no_superuser_is_created(self):
        """Admins come from createsuperuser (spec section 6)."""
        self.run_seed()
        self.assertFalse(User.objects.filter(is_superuser=True).exists())

    def test_seeded_accounts_can_log_in(self):
        self.run_seed()
        for username in (RIDER_USERNAME, CUSTOMER_USERNAME):
            response = self.client.post(
                reverse("accounts:login"),
                {"username": username, "password": "Kidapawan!2026"},
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200, username)
