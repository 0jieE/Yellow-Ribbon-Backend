"""Menu endpoint tests: visibility rules and admin-only writes."""

from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, Rider, User

from .models import Menu


class MenuTestBase(APITestCase):
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
        Rider.objects.create(user=self.rider)
        self.customer = User.objects.create_user(
            username="maria", email="maria@example.com", password="Kidapawan!2026"
        )
        Customer.objects.create(user=self.customer)

        self.available = Menu.objects.create(
            name="Pepperoni Pizza",
            description="Classic",
            price=Decimal("299.00"),
        )
        self.hidden = Menu.objects.create(
            name="Seasonal Special",
            price=Decimal("399.00"),
            is_available=False,
        )

        self.list_url = reverse("menu:menu-list")

    def detail_url(self, item):
        return reverse("menu:menu-detail", args=[item.pk])


class MenuVisibilityTests(MenuTestBase):
    def test_anonymous_cannot_list(self):
        self.assertEqual(
            self.client.get(self.list_url).status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_customer_sees_available_items_only(self):
        self.client.force_authenticate(self.customer)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [item["name"] for item in response.data]
        self.assertEqual(names, ["Pepperoni Pizza"])

    def test_customer_cannot_retrieve_an_unavailable_item(self):
        self.client.force_authenticate(self.customer)
        response = self.client.get(self.detail_url(self.hidden))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_sees_the_full_catalogue(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_rider_sees_the_full_catalogue(self):
        self.client.force_authenticate(self.rider)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_list_is_ordered_by_name(self):
        Menu.objects.create(name="Aloha Pizza", price=Decimal("259.00"))
        self.client.force_authenticate(self.customer)
        names = [item["name"] for item in self.client.get(self.list_url).data]
        self.assertEqual(names, sorted(names))


class MenuAdminCrudTests(MenuTestBase):
    payload = {
        "name": "Hawaiian Pizza",
        "description": "Ham and pineapple",
        "price": "329.00",
        "is_available": True,
    }

    def test_admin_can_create(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(self.list_url, self.payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        item = Menu.objects.get(name="Hawaiian Pizza")
        self.assertEqual(item.price, Decimal("329.00"))

    def test_admin_can_patch(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.detail_url(self.available), {"price": "349.50"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.available.refresh_from_db()
        self.assertEqual(self.available.price, Decimal("349.50"))

    def test_admin_can_toggle_availability(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(
            self.detail_url(self.hidden), {"is_available": True}, format="json"
        )
        self.hidden.refresh_from_db()
        self.assertTrue(self.hidden.is_available)

    def test_admin_can_delete(self):
        self.client.force_authenticate(self.admin)
        response = self.client.delete(self.detail_url(self.available))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Menu.objects.filter(pk=self.available.pk).exists())

    def test_put_is_not_allowed(self):
        self.client.force_authenticate(self.admin)
        response = self.client.put(
            self.detail_url(self.available), self.payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_negative_price_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.list_url, {**self.payload, "price": "-10.00"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", response.data)

    def test_blank_name_rejected(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.list_url, {**self.payload, "name": "   "}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_timestamps_are_read_only(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            self.list_url,
            {**self.payload, "created_at": "2000-01-01T00:00:00Z"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(response.data["created_at"][:4], "2000")


class MenuWritePermissionTests(MenuTestBase):
    """Only admins may write. Customers and riders are read-only."""

    def test_non_admins_cannot_create(self):
        for user in (self.customer, self.rider):
            self.client.force_authenticate(user)
            response = self.client.post(
                self.list_url,
                {"name": "Rogue Item", "price": "1.00"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, user)
        self.assertFalse(Menu.objects.filter(name="Rogue Item").exists())

    def test_non_admins_cannot_patch(self):
        for user in (self.customer, self.rider):
            self.client.force_authenticate(user)
            response = self.client.patch(
                self.detail_url(self.available), {"price": "1.00"}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, user)
        self.available.refresh_from_db()
        self.assertEqual(self.available.price, Decimal("299.00"))

    def test_non_admins_cannot_delete(self):
        for user in (self.customer, self.rider):
            self.client.force_authenticate(user)
            response = self.client.delete(self.detail_url(self.available))
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, user)
        self.assertTrue(Menu.objects.filter(pk=self.available.pk).exists())
