"""
Email is optional: the apps sign in by username and no longer send an address.

What must hold: every flow works with no email, many users can have none at
once, an address that IS set is still unique (case-insensitively), and
nobody's existing address is touched.
"""

from io import StringIO

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, Rider, User

PASSWORD = "Kidapawan!2026"


def register_body(username, **extra):
    return {
        "username": username,
        "password": PASSWORD,
        "password_confirm": PASSWORD,
        **extra,
    }


class RegisterWithoutEmailTests(APITestCase):
    url = reverse("accounts:register")

    def test_no_email_key_at_all(self):
        response = self.client.post(self.url, register_body("ana"), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        user = User.objects.get(username="ana")
        self.assertEqual(user.email, "")
        self.assertTrue(user.is_customer)
        self.assertTrue(Customer.objects.filter(user=user).exists())

    def test_blank_email(self):
        response = self.client.post(self.url, register_body("ana", email=""), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(User.objects.get(username="ana").email, "")

    def test_many_customers_can_register_without_one(self):
        for name in ("ana", "ben", "cara", "dino"):
            response = self.client.post(self.url, register_body(name), format="json")
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, name)
        self.assertEqual(User.objects.filter(email="").count(), 4)

    def test_the_response_still_has_an_email_key(self):
        response = self.client.post(self.url, register_body("ana"), format="json")
        self.assertEqual(response.data.get("email", ""), "")

    def test_a_registered_user_can_still_log_in_by_username(self):
        self.client.post(self.url, register_body("ana"), format="json")
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "ana", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["username"], "ana")
        self.assertEqual(response.data["user"]["email"], "")

    def test_an_address_that_is_given_is_still_unique(self):
        self.client.post(self.url, register_body("ana", email="ana@example.com"), format="json")
        response = self.client.post(
            self.url, register_body("ben", email="ANA@example.com"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data["fields"])

    def test_an_invalid_address_is_still_refused(self):
        response = self.client.post(self.url, register_body("ana", email="not-an-email"), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data["fields"])

    def test_people_with_and_without_email_coexist(self):
        self.client.post(self.url, register_body("ana"), format="json")
        self.client.post(self.url, register_body("ben", email="ben@example.com"), format="json")
        self.client.post(self.url, register_body("cara"), format="json")
        self.assertEqual(User.objects.count(), 3)

    def test_logging_in_with_a_blank_identifier_never_matches_a_user_without_email(self):
        self.client.post(self.url, register_body("ana"), format="json")
        for identifier in ("", "   "):
            response = self.client.post(
                reverse("accounts:login"),
                {"identifier": identifier, "password": PASSWORD},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, repr(identifier))


class ProfileTests(APITestCase):
    url = reverse("accounts:me")

    def setUp(self):
        self.user = User.objects.create_user("ana", password=PASSWORD)
        Customer.objects.create(user=self.user)

    def test_patch_without_email_still_works(self):
        self.client.force_authenticate(self.user)
        response = self.client.patch(self.url, {"first_name": "Ana"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Ana")

    def test_me_for_a_user_without_email(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], "")

    def test_an_existing_email_survives_an_unrelated_patch(self):
        self.user.email = "ana@example.com"
        self.user.save()
        self.client.force_authenticate(self.user)
        self.client.patch(self.url, {"phone": "0917"}, format="json")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "ana@example.com")

    def test_email_can_be_cleared(self):
        self.user.email = "ana@example.com"
        self.user.save()
        self.client.force_authenticate(self.user)
        response = self.client.patch(self.url, {"email": ""}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "")

    def test_setting_an_address_someone_else_has_is_refused(self):
        User.objects.create_user("ben", email="ben@example.com", password=PASSWORD)
        self.client.force_authenticate(self.user)
        response = self.client.patch(self.url, {"email": "BEN@example.com"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_two_users_clearing_their_email_do_not_clash(self):
        other = User.objects.create_user("ben", email="ben@example.com", password=PASSWORD)
        Customer.objects.create(user=other)
        self.user.email = "ana@example.com"
        self.user.save()
        for user in (self.user, other):
            self.client.force_authenticate(user)
            self.assertEqual(
                self.client.patch(self.url, {"email": ""}, format="json").status_code,
                status.HTTP_200_OK,
            )


class AdminRiderTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", password=PASSWORD)
        self.client.force_authenticate(self.admin)
        self.list_url = reverse("admin-rider-list")

    def body(self, username, **extra):
        return {"username": username, "password": PASSWORD, **extra}

    def test_create_without_email(self):
        response = self.client.post(self.list_url, self.body("rico"), format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        user = User.objects.get(username="rico")
        self.assertEqual(user.email, "")
        self.assertTrue(user.is_rider)
        self.assertTrue(Rider.objects.filter(user=user).exists())

    def test_create_with_blank_email(self):
        response = self.client.post(self.list_url, self.body("rico", email=""), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_many_riders_without_email(self):
        for name in ("rico", "rey", "rodel"):
            response = self.client.post(self.list_url, self.body(name), format="json")
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, name)

    def test_create_with_an_email_still_checks_for_duplicates(self):
        self.client.post(self.list_url, self.body("rico", email="rico@example.com"), format="json")
        response = self.client.post(
            self.list_url, self.body("rey", email="RICO@example.com"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data["fields"])

    def test_rider_response_has_an_email_key(self):
        response = self.client.post(self.list_url, self.body("rico"), format="json")
        self.assertEqual(response.data["email"], "")

    def test_patch_without_email_leaves_it_alone(self):
        created = self.client.post(
            self.list_url, self.body("rico", email="rico@example.com"), format="json"
        )
        url = reverse("admin-rider-detail", args=[created.data["id"]])

        response = self.client.patch(url, {"vehicle_type": "Scooter"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(User.objects.get(username="rico").email, "rico@example.com")

    def test_patch_can_clear_the_email(self):
        created = self.client.post(
            self.list_url, self.body("rico", email="rico@example.com"), format="json"
        )
        url = reverse("admin-rider-detail", args=[created.data["id"]])

        response = self.client.patch(url, {"email": ""}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(User.objects.get(username="rico").email, "")

    def test_two_riders_can_both_have_no_email_after_an_edit(self):
        ids = [
            self.client.post(self.list_url, self.body(n, email=f"{n}@example.com"), format="json").data["id"]
            for n in ("rico", "rey")
        ]
        for pk in ids:
            response = self.client.patch(reverse("admin-rider-detail", args=[pk]), {"email": ""}, format="json")
            self.assertEqual(response.status_code, status.HTTP_200_OK)


class ModelAndConstraintTests(TestCase):
    def test_the_field_is_optional_but_never_null(self):
        field = User._meta.get_field("email")
        self.assertTrue(field.blank)
        self.assertFalse(field.null)
        self.assertFalse(field.unique)

    def test_the_database_allows_many_blanks(self):
        for name in ("a", "b", "c"):
            User.objects.create_user(name, password=PASSWORD)
        self.assertEqual(User.objects.filter(email="").count(), 3)

    def test_the_database_still_refuses_case_variant_addresses(self):
        User.objects.create_user("one", email="dupe@example.com", password=PASSWORD)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create_user("two", email="DUPE@example.com", password=PASSWORD)

    def test_a_blank_does_not_collide_with_an_address(self):
        User.objects.create_user("one", email="one@example.com", password=PASSWORD)
        User.objects.create_user("two", password=PASSWORD)
        User.objects.create_user("three", email="", password=PASSWORD)
        self.assertEqual(User.objects.count(), 3)

    def test_none_is_stored_as_blank(self):
        user = User.objects.create_user("x", email=None, password=PASSWORD)
        self.assertEqual(user.email, "")

    def test_existing_addresses_are_untouched(self):
        # (Django's create_user lowercases the domain part; that is not new.)
        user = User.objects.create_user("x", email="Keep.Me@example.com", password=PASSWORD)
        user.refresh_from_db()
        self.assertEqual(user.email, "Keep.Me@example.com")

    def test_createsuperuser_needs_no_email(self):
        call_command(
            "createsuperuser",
            interactive=False,
            username="boss",
            stdout=StringIO(),
        )
        user = User.objects.get(username="boss")
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.email, "")

    def test_create_superuser_in_code_needs_no_email(self):
        user = User.objects.create_superuser("boss", password=PASSWORD)
        self.assertTrue(user.is_admin)
        self.assertEqual(user.email, "")

    def test_two_superusers_without_email(self):
        User.objects.create_superuser("boss", password=PASSWORD)
        User.objects.create_superuser("boss2", password=PASSWORD)
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 2)


class DjangoAdminTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password=PASSWORD)
        self.client.force_login(self.boss)

    def test_the_add_user_form_does_not_require_email(self):
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "username": "newbie",
                "password1": "Sup3r!Secret-Pass",
                "password2": "Sup3r!Secret-Pass",
                "is_customer": "on",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(username="newbie", email="").exists())

    def test_two_users_without_email_through_the_admin(self):
        for name in ("one", "two"):
            self.client.post(
                reverse("admin:accounts_user_add"),
                {
                    "username": name,
                    "password1": "Sup3r!Secret-Pass",
                    "password2": "Sup3r!Secret-Pass",
                },
            )
        self.assertEqual(User.objects.filter(username__in=["one", "two"]).count(), 2)

    def test_the_user_list_loads_with_blank_emails(self):
        User.objects.create_user("noemail", password=PASSWORD)
        self.assertEqual(self.client.get(reverse("admin:accounts_user_changelist")).status_code, 200)
