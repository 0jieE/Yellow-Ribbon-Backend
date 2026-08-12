"""Smoke tests for the auth endpoints and the role permission classes."""

from django.urls import path, reverse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.test import APITestCase, URLPatternsTestCase

from core.permissions import IsAdminUser, IsCustomerUser, IsRiderUser

from .models import Customer, Rider, User


class RegistrationTests(APITestCase):
    url = reverse("accounts:register")

    payload = {
        "username": "juan",
        "email": "juan@example.com",
        "password": "Kidapawan!2026",
        "password_confirm": "Kidapawan!2026",
        "first_name": "Juan",
        "last_name": "Dela Cruz",
        "phone": "09171234567",
        "default_address": "Blue gate, Quezon Blvd",
        "default_latitude": "7.008300",
        "default_longitude": "125.089400",
    }

    def test_register_creates_customer_with_profile(self):
        response = self.client.post(self.url, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username="juan")
        self.assertTrue(user.is_customer)
        self.assertFalse(user.is_rider)
        self.assertFalse(user.is_admin)
        self.assertTrue(user.check_password("Kidapawan!2026"))
        self.assertNotIn("password", response.data)

        profile = Customer.objects.get(user=user)
        self.assertEqual(profile.default_address, "Blue gate, Quezon Blvd")

    def test_role_flags_cannot_be_escalated_from_the_request_body(self):
        response = self.client.post(
            self.url,
            {**self.payload, "is_admin": True, "is_rider": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username="juan")
        self.assertFalse(user.is_admin)
        self.assertFalse(user.is_rider)

    def test_mismatched_passwords_rejected(self):
        response = self.client.post(
            self.url, {**self.payload, "password_confirm": "different"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.data)

    def test_weak_password_rejected(self):
        response = self.client.post(
            self.url,
            {**self.payload, "password": "12345678", "password_confirm": "12345678"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(username="juan").exists())

    def test_duplicate_email_rejected(self):
        self.client.post(self.url, self.payload, format="json")
        response = self.client.post(
            self.url, {**self.payload, "username": "juan2"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(User.objects.filter(email="juan@example.com").count(), 1)

    def test_failed_registration_creates_no_orphan_user(self):
        # The Customer row and the User row must land or fail together.
        self.client.post(
            self.url, {**self.payload, "password_confirm": "nope"}, format="json"
        )
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(Customer.objects.count(), 0)


class LoginTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="maria", email="maria@example.com", password="Kidapawan!2026"
        )
        Customer.objects.create(user=self.user)

    def test_login_returns_tokens_and_role_flags(self):
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "maria", "password": "Kidapawan!2026"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

        user = response.data["user"]
        self.assertEqual(user["username"], "maria")
        self.assertTrue(user["is_customer"])
        self.assertFalse(user["is_admin"])
        self.assertFalse(user["is_rider"])
        self.assertEqual(user["role"], "customer")

    def test_bad_password_rejected(self):
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "maria", "password": "wrong"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertIn("error", response.data)

    def test_refresh_returns_new_access_token(self):
        login = self.client.post(
            reverse("accounts:login"),
            {"username": "maria", "password": "Kidapawan!2026"},
            format="json",
        )
        response = self.client.post(
            reverse("accounts:refresh"),
            {"refresh": login.data["refresh"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)


class MeEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="maria", email="maria@example.com", password="Kidapawan!2026"
        )
        self.profile = Customer.objects.create(user=self.user, default_address="Old")

    def test_requires_authentication(self):
        response = self.client.get(reverse("accounts:me"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_returns_own_profile_with_nested_customer(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(reverse("accounts:me"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["username"], "maria")
        self.assertEqual(response.data["customer"]["default_address"], "Old")
        self.assertIsNone(response.data["rider"])

    def test_patch_updates_user_and_nested_customer(self):
        self.client.force_authenticate(self.user)
        response = self.client.patch(
            reverse("accounts:me"),
            {
                "phone": "09998887777",
                "customer": {
                    "default_address": "New landmark",
                    "default_latitude": "7.010000",
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(self.user.phone, "09998887777")
        self.assertEqual(self.profile.default_address, "New landmark")

    def test_role_flags_are_read_only(self):
        self.client.force_authenticate(self.user)
        self.client.patch(reverse("accounts:me"), {"is_admin": True}, format="json")

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_admin)

    def test_logout_acknowledges(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(reverse("accounts:logout"))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


# --- Permission classes -------------------------------------------------


@api_view(["GET"])
@permission_classes([IsAdminUser])
def admin_only(_request):
    return Response({"ok": True})


@api_view(["GET"])
@permission_classes([IsRiderUser])
def rider_only(_request):
    return Response({"ok": True})


@api_view(["GET"])
@permission_classes([IsCustomerUser])
def customer_only(_request):
    return Response({"ok": True})


class PermissionClassTests(APITestCase, URLPatternsTestCase):
    urlpatterns = [
        path("t/admin/", admin_only),
        path("t/rider/", rider_only),
        path("t/customer/", customer_only),
    ]

    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="boss", email="boss@example.com", password="Kidapawan!2026"
        )
        self.rider = User.objects.create_user(
            username="rico",
            email="rico@example.com",
            password="Kidapawan!2026",
            is_rider=True,
            is_customer=False,
        )
        Rider.objects.create(user=self.rider, vehicle_type="Motorcycle")
        self.customer = User.objects.create_user(
            username="maria", email="maria@example.com", password="Kidapawan!2026"
        )
        Customer.objects.create(user=self.customer)

    def assert_access(self, user, url, allowed):
        self.client.force_authenticate(user)
        response = self.client.get(url)
        expected = status.HTTP_200_OK if allowed else status.HTTP_403_FORBIDDEN
        self.assertEqual(response.status_code, expected, f"{user} -> {url}")

    def test_createsuperuser_sets_the_admin_role_flag(self):
        self.assertTrue(self.admin.is_admin)
        self.assertTrue(self.admin.is_staff)
        self.assertFalse(self.admin.is_customer)

    def test_each_role_reaches_only_its_own_endpoint(self):
        self.assert_access(self.admin, "/t/admin/", True)
        self.assert_access(self.admin, "/t/rider/", False)
        self.assert_access(self.admin, "/t/customer/", False)

        self.assert_access(self.rider, "/t/rider/", True)
        self.assert_access(self.rider, "/t/admin/", False)
        self.assert_access(self.rider, "/t/customer/", False)

        self.assert_access(self.customer, "/t/customer/", True)
        self.assert_access(self.customer, "/t/admin/", False)
        self.assert_access(self.customer, "/t/rider/", False)

    def test_anonymous_is_rejected(self):
        for url in ("/t/admin/", "/t/rider/", "/t/customer/"):
            self.assertEqual(
                self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED
            )

    def test_deactivated_user_loses_access(self):
        self.customer.is_active = False
        self.customer.save()
        self.assert_access(self.customer, "/t/customer/", False)
