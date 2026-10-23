"""Login by username or email, and the email uniqueness that makes it safe."""

from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Customer, User

GENERIC_ERROR = "No active account found with the given credentials"
PASSWORD = "Kidapawan!2026"


class LoginTestBase(APITestCase):
    url = reverse("accounts:login")

    def setUp(self):
        self.user = User.objects.create_user(
            username="maria",
            email="Maria.Santos@Gmail.com",
            password=PASSWORD,
        )
        Customer.objects.create(user=self.user)

    def login(self, **body):
        return self.client.post(self.url, body, format="json")


class LoginByUsernameTests(LoginTestBase):
    def test_legacy_username_key_still_works(self):
        """All three Flutter apps send this today."""
        response = self.login(username="maria", password=PASSWORD)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)
        self.assertEqual(response.data["user"]["username"], "maria")

    def test_identifier_key_works(self):
        response = self.login(identifier="maria", password=PASSWORD)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["username"], "maria")

    def test_the_payload_is_unchanged(self):
        by_username = self.login(username="maria", password=PASSWORD).data
        by_identifier = self.login(identifier="maria", password=PASSWORD).data

        self.assertEqual(set(by_username), {"access", "refresh", "user"})
        self.assertEqual(set(by_username), set(by_identifier))
        self.assertEqual(by_username["user"], by_identifier["user"])

    def test_identifier_wins_when_both_are_sent(self):
        other = User.objects.create_user(
            username="pedro", email="pedro@example.com", password=PASSWORD
        )
        Customer.objects.create(user=other)

        response = self.login(
            identifier="pedro", username="maria", password=PASSWORD
        )
        self.assertEqual(response.data["user"]["username"], "pedro")

    def test_surrounding_whitespace_is_trimmed(self):
        response = self.login(identifier="  maria  ", password=PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_wrong_password_is_rejected(self):
        response = self.login(username="maria", password="wrong")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["error"], GENERIC_ERROR)


class LoginByEmailTests(LoginTestBase):
    def test_login_with_the_email_as_stored(self):
        response = self.login(identifier="Maria.Santos@Gmail.com", password=PASSWORD)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["username"], "maria")

    def test_login_with_a_lowercased_email(self):
        response = self.login(identifier="maria.santos@gmail.com", password=PASSWORD)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["username"], "maria")

    def test_login_with_a_shouted_email(self):
        response = self.login(identifier="MARIA.SANTOS@GMAIL.COM", password=PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_login_with_mixed_case_email(self):
        response = self.login(identifier="mArIa.SaNtOs@gMaIl.CoM", password=PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_email_works_through_the_legacy_username_key(self):
        """A client that has not migrated can still pass an email."""
        response = self.login(username="maria.santos@gmail.com", password=PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_wrong_password_on_the_email_path(self):
        response = self.login(identifier="maria.santos@gmail.com", password="wrong")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["error"], GENERIC_ERROR)

    def test_riders_and_admins_can_use_their_email_too(self):
        rider = User.objects.create_user(
            username="rico",
            email="Rico@Example.COM",
            password=PASSWORD,
            is_rider=True,
            is_customer=False,
        )
        admin = User.objects.create_superuser(
            username="boss", email="Boss@Example.COM", password=PASSWORD
        )

        for user, email in ((rider, "rico@example.com"), (admin, "boss@example.com")):
            response = self.login(identifier=email, password=PASSWORD)
            self.assertEqual(response.status_code, status.HTTP_200_OK, email)
            self.assertEqual(response.data["user"]["username"], user.username)


class LoginFailureTests(LoginTestBase):
    def test_unknown_identifier(self):
        response = self.login(identifier="nobody", password=PASSWORD)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["error"], GENERIC_ERROR)

    def test_unknown_email(self):
        response = self.login(identifier="nobody@example.com", password=PASSWORD)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["error"], GENERIC_ERROR)

    def test_the_error_never_reveals_which_accounts_exist(self):
        """A real account with a bad password must look like a fake one."""
        real = self.login(identifier="maria", password="wrong")
        real_email = self.login(identifier="maria.santos@gmail.com", password="wrong")
        fake = self.login(identifier="ghost", password="wrong")
        fake_email = self.login(identifier="ghost@example.com", password="wrong")

        bodies = [r.data["error"] for r in (real, real_email, fake, fake_email)]
        self.assertEqual(len(set(bodies)), 1, bodies)
        self.assertEqual(bodies[0], GENERIC_ERROR)

        codes = {r.status_code for r in (real, real_email, fake, fake_email)}
        self.assertEqual(codes, {status.HTTP_401_UNAUTHORIZED})

    def test_deactivated_account_gets_the_same_message(self):
        self.user.is_active = False
        self.user.save()

        for value in ("maria", "maria.santos@gmail.com"):
            response = self.login(identifier=value, password=PASSWORD)
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
            self.assertEqual(response.data["error"], GENERIC_ERROR)

    def test_missing_identifier(self):
        response = self.login(password=PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_blank_identifier(self):
        response = self.login(identifier="   ", password=PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_password(self):
        response = self.login(identifier="maria")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class LoginAmbiguityTests(APITestCase):
    """
    One account is *named* bob@example.com; a different account *has* that
    email. The lookup must be deterministic — username wins.
    """

    url = reverse("accounts:login")
    SHARED = "bob@example.com"

    def setUp(self):
        # Username is the shared string; their own email is something else.
        self.named = User.objects.create_user(
            username=self.SHARED, email="named@example.com", password=PASSWORD
        )
        Customer.objects.create(user=self.named)

        # This one owns the address as an email.
        self.owner = User.objects.create_user(
            username="bob", email=self.SHARED, password="Different!2026"
        )
        Customer.objects.create(user=self.owner)

    def login(self, **body):
        return self.client.post(self.url, body, format="json")

    def test_username_takes_precedence_over_email(self):
        response = self.login(identifier=self.SHARED, password=PASSWORD)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["username"], self.SHARED)
        self.assertEqual(response.data["user"]["email"], "named@example.com")

    def test_the_email_owners_password_does_not_open_the_username_account(self):
        response = self.login(identifier=self.SHARED, password="Different!2026")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["error"], GENERIC_ERROR)

    def test_the_email_owner_can_still_log_in_by_username(self):
        response = self.login(identifier="bob", password="Different!2026")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["username"], "bob")

    def test_precedence_is_stable_across_repeated_calls(self):
        usernames = {
            self.login(identifier=self.SHARED, password=PASSWORD).data["user"][
                "username"
            ]
            for _ in range(5)
        }
        self.assertEqual(usernames, {self.SHARED})


class EmailUniquenessTests(APITestCase):
    register_url = reverse("accounts:register")

    payload = {
        "username": "juan",
        "email": "juan@example.com",
        "password": PASSWORD,
        "password_confirm": PASSWORD,
    }

    def test_email_is_optional_on_the_model(self):
        # Optional now; uniqueness only applies to addresses that are set
        # (see accounts/tests_optional_email.py for the full behaviour).
        field = User._meta.get_field("email")
        self.assertTrue(field.blank)
        self.assertFalse(field.null)

    def test_registration_does_not_require_an_email(self):
        body = {k: v for k, v in self.payload.items() if k != "email"}
        response = self.client.post(self.register_url, body, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_blank_email_is_accepted(self):
        response = self.client.post(
            self.register_url, {**self.payload, "email": ""}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_duplicate_email_is_a_field_error_not_a_500(self):
        self.client.post(self.register_url, self.payload, format="json")
        response = self.client.post(
            self.register_url,
            {**self.payload, "username": "juan2"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data["fields"])
        self.assertEqual(User.objects.filter(email__iexact="juan@example.com").count(), 1)

    def test_a_case_variant_email_is_also_a_duplicate(self):
        self.client.post(self.register_url, self.payload, format="json")
        response = self.client.post(
            self.register_url,
            {**self.payload, "username": "juan2", "email": "JUAN@example.com"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data["fields"])

    def test_the_database_refuses_case_variant_emails(self):
        """
        The serializers check with iexact, but createsuperuser and data
        imports skip them. Without this the email login lookup could match
        two accounts.
        """
        User.objects.create_user(
            username="one", email="dupe@example.com", password=PASSWORD
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create_user(
                    username="two", email="DUPE@example.com", password=PASSWORD
                )

    def test_duplicate_username_is_still_a_field_error(self):
        self.client.post(self.register_url, self.payload, format="json")
        response = self.client.post(
            self.register_url,
            {**self.payload, "email": "other@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_registered_customer_can_log_in_with_their_email(self):
        """End to end: register, then sign in by email in a different case."""
        self.client.post(self.register_url, self.payload, format="json")

        response = self.client.post(
            reverse("accounts:login"),
            {"identifier": "JUAN@Example.com", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["username"], "juan")
