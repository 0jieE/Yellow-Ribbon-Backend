"""
Store settings: the delivery fee and service area are the admin's to change.

The point of the model is that nothing reads the environment variables any
more once the row exists, so these tests change the row and watch the order
flow, the geofence and /api/constants/ follow.
"""

from decimal import Decimal

from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Customer, Rider, User
from core.geofence import distance_from_service_center_km
from core.maps import store_location
from core.models import StoreSettings
from orders.models import Order
from orders.tests import IN_AREA, OUT_OF_AREA, OrderTestBase

PASSWORD = "Kidapawan!2026"


class StoreSettingsModelTests(TestCase):
    def test_first_read_creates_the_row_from_the_environment(self):
        self.assertFalse(StoreSettings.objects.exists())

        store = StoreSettings.load()

        self.assertEqual(store.pk, 1)
        self.assertEqual(store.delivery_fee, Decimal("30.00"))
        self.assertEqual(store.service_area_radius_km, Decimal("8.00"))
        self.assertEqual(store.service_area_city, "Kidapawan City")

    @override_settings(
        DELIVERY_FEE="45.00",
        SERVICE_AREA_CENTER_LAT=14.5995,
        SERVICE_AREA_CENTER_LNG=120.9842,
        SERVICE_AREA_RADIUS_KM=5.0,
    )
    def test_seed_uses_whatever_the_environment_says(self):
        store = StoreSettings.load()
        self.assertEqual(store.delivery_fee, Decimal("45.00"))
        self.assertEqual(store.center, (14.5995, 120.9842))
        self.assertEqual(store.radius_km, 5.0)

    def test_the_environment_is_ignored_once_the_row_exists(self):
        store = StoreSettings.load()
        store.delivery_fee = Decimal("55.00")
        store.save()

        with override_settings(DELIVERY_FEE="99.00"):
            self.assertEqual(StoreSettings.load().delivery_fee, Decimal("55.00"))

    def test_there_is_only_ever_one_row(self):
        StoreSettings.load()
        StoreSettings(delivery_fee=Decimal("10.00")).save()
        StoreSettings(delivery_fee=Decimal("20.00")).save()

        self.assertEqual(StoreSettings.objects.count(), 1)
        self.assertEqual(StoreSettings.load().delivery_fee, Decimal("20.00"))

    def test_the_row_cannot_be_deleted(self):
        store = StoreSettings.load()
        with self.assertRaises(ProtectedError):
            store.delete()
        self.assertTrue(StoreSettings.objects.exists())

    def test_flat_fee_ignores_distance(self):
        store = StoreSettings.load()
        self.assertEqual(store.delivery_fee_for(0), Decimal("30.00"))
        self.assertEqual(store.delivery_fee_for(7.9), Decimal("30.00"))
        self.assertEqual(store.delivery_fee_for(None), Decimal("30.00"))

    def test_per_km_fee_adds_to_the_base(self):
        store = StoreSettings.load()
        store.delivery_fee = Decimal("20.00")
        store.delivery_fee_per_km = Decimal("5.00")

        self.assertEqual(store.delivery_fee_for(0), Decimal("20.00"))
        self.assertEqual(store.delivery_fee_for(3), Decimal("35.00"))
        self.assertEqual(store.delivery_fee_for(2.5), Decimal("32.50"))

    def test_per_km_fee_rounds_to_centavos(self):
        store = StoreSettings.load()
        store.delivery_fee = Decimal("20.00")
        store.delivery_fee_per_km = Decimal("7.00")
        # 20 + 7 * 1.2345 = 28.6415
        self.assertEqual(store.delivery_fee_for(1.2345), Decimal("28.64"))

    def test_maps_measures_from_the_store_settings(self):
        store = StoreSettings.load()
        store.service_area_center_lat = Decimal("8.500000")
        store.service_area_center_lng = Decimal("126.500000")
        store.save()
        self.assertEqual(store_location(), (8.5, 126.5))


class StoreSettingsApiTests(APITestCase):
    url = reverse("admin-store-settings")

    def setUp(self):
        self.admin = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        self.customer_user = User.objects.create_user(
            "maria", "maria@example.com", PASSWORD
        )
        Customer.objects.create(user=self.customer_user)
        self.rider_user = User.objects.create_user(
            "rico", "rico@example.com", PASSWORD, is_rider=True, is_customer=False
        )
        Rider.objects.create(user=self.rider_user)

    def test_only_an_admin_may_use_it(self):
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)
        for user in (self.customer_user, self.rider_user):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(
                self.client.patch(self.url, {"delivery_fee": "1.00"}, format="json").status_code,
                status.HTTP_403_FORBIDDEN,
            )
        self.assertEqual(StoreSettings.load().delivery_fee, Decimal("30.00"))

    def test_get_returns_every_setting(self):
        self.client.force_authenticate(self.admin)
        data = self.client.get(self.url).data

        self.assertEqual(
            sorted(data),
            sorted(
                [
                    "delivery_fee",
                    "delivery_fee_per_km",
                    "delivery_fee_mode",
                    "service_area_city",
                    "service_area_center_lat",
                    "service_area_center_lng",
                    "service_area_radius_km",
                    "paymongo_fee_percent",
                    "paymongo_fee_fixed",
                    "maya_fee_percent",
                    "maya_fee_fixed",
                    "updated_at",
                ]
            ),
        )
        self.assertEqual(data["delivery_fee"], "30.00")
        self.assertEqual(data["delivery_fee_mode"], "flat")
        self.assertEqual(data["service_area_radius_km"], "8.00")
        self.assertEqual(data["service_area_city"], "Kidapawan City")

    def test_patch_changes_only_what_it_names(self):
        self.client.force_authenticate(self.admin)

        response = self.client.patch(
            self.url, {"delivery_fee": "45.50", "service_area_radius_km": "12"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["delivery_fee"], "45.50")
        self.assertEqual(response.data["service_area_radius_km"], "12.00")
        store = StoreSettings.load()
        self.assertEqual(store.delivery_fee, Decimal("45.50"))
        self.assertEqual(store.service_area_city, "Kidapawan City")
        self.assertEqual(store.service_area_center_lat, Decimal("7.008300"))

    def test_per_km_rate_switches_the_mode(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(self.url, {"delivery_fee_per_km": "4.00"}, format="json")
        self.assertEqual(response.data["delivery_fee_mode"], "per_km")

    def test_read_only_fields_are_ignored(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.url, {"delivery_fee_mode": "per_km", "updated_at": "2000-01-01T00:00:00Z"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["delivery_fee_mode"], "flat")
        self.assertNotEqual(response.data["updated_at"], "2000-01-01T00:00:00Z")

    def assert_rejected(self, field, value):
        self.client.force_authenticate(self.admin)
        before = self.client.get(self.url).data[field]
        response = self.client.patch(self.url, {field: value}, format="json")
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, f"{field}={value!r}"
        )
        # The project error handler nests per-field messages under "fields".
        self.assertIn(field, response.data["fields"])
        self.assertEqual(self.client.get(self.url).data[field], before)

    def test_fee_cannot_be_negative(self):
        self.assert_rejected("delivery_fee", "-1.00")
        self.assert_rejected("delivery_fee_per_km", "-0.50")

    def test_fee_has_a_sane_ceiling(self):
        self.assert_rejected("delivery_fee", "10000.01")

    def test_radius_must_be_positive_and_not_absurd(self):
        self.assert_rejected("service_area_radius_km", "0")
        self.assert_rejected("service_area_radius_km", "-5")
        self.assert_rejected("service_area_radius_km", "100.01")

    def test_latitude_and_longitude_must_be_on_earth(self):
        self.assert_rejected("service_area_center_lat", "90.5")
        self.assert_rejected("service_area_center_lat", "-91")
        self.assert_rejected("service_area_center_lng", "180.5")
        self.assert_rejected("service_area_center_lng", "-181")

    def test_gateway_percentages_are_percentages(self):
        self.assert_rejected("paymongo_fee_percent", "100.01")
        self.assert_rejected("maya_fee_percent", "-0.1")
        self.assert_rejected("maya_fee_fixed", "-1")

    def test_garbage_is_a_400_not_a_500(self):
        self.assert_rejected("delivery_fee", "free")
        self.assert_rejected("service_area_center_lat", "north")

    def test_city_cannot_be_blank(self):
        self.assert_rejected("service_area_city", "   ")

    def test_boundaries_are_accepted(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(
            self.url,
            {
                "delivery_fee": "0",
                "service_area_radius_km": "100",
                "service_area_center_lat": "-90",
                "service_area_center_lng": "180",
                "paymongo_fee_percent": "0",
                "maya_fee_percent": "100",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class OrderFlowFollowsTheSettingsTests(OrderTestBase):
    def set_store(self, **fields):
        store = StoreSettings.load()
        for name, value in fields.items():
            setattr(store, name, value)
        store.save()
        return store

    def test_a_new_fee_applies_to_the_next_order(self):
        self.set_store(delivery_fee=Decimal("45.00"))

        response = self.place()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        order = Order.objects.get()
        self.assertEqual(order.delivery_fee, Decimal("45.00"))
        self.assertEqual(order.total, order.subtotal + Decimal("45.00"))

    def test_past_orders_keep_the_fee_they_were_placed_with(self):
        self.place()
        old = Order.objects.get()

        self.set_store(delivery_fee=Decimal("99.00"))
        self.place()

        old.refresh_from_db()
        self.assertEqual(old.delivery_fee, Decimal("30.00"))
        self.assertEqual(old.total, old.subtotal + Decimal("30.00"))

    def test_a_free_delivery_order_works(self):
        self.set_store(delivery_fee=Decimal("0.00"))
        self.place()
        order = Order.objects.get()
        self.assertEqual(order.delivery_fee, Decimal("0.00"))
        self.assertEqual(order.total, order.subtotal)

    def test_per_km_fee_is_charged_on_the_order(self):
        store = self.set_store(
            delivery_fee=Decimal("20.00"), delivery_fee_per_km=Decimal("5.00")
        )
        lat, lng = "7.053300", "125.089400"  # about 5 km north of the store
        distance = distance_from_service_center_km(Decimal(lat), Decimal(lng), store)

        self.place(delivery_latitude=lat, delivery_longitude=lng)

        order = Order.objects.get()
        self.assertEqual(order.delivery_fee, store.delivery_fee_for(distance))
        self.assertGreater(order.delivery_fee, Decimal("40.00"))

    def test_per_km_fee_at_the_store_is_just_the_base(self):
        self.set_store(delivery_fee=Decimal("20.00"), delivery_fee_per_km=Decimal("5.00"))
        self.place()
        self.assertEqual(Order.objects.get().delivery_fee, Decimal("20.00"))

    def test_a_smaller_radius_turns_away_an_address_that_was_fine(self):
        self.assertEqual(self.place().status_code, status.HTTP_201_CREATED)
        self.set_store(service_area_radius_km=Decimal("0.50"))

        response = self.place(delivery_latitude="7.053300", delivery_longitude="125.089400")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_moving_the_centre_moves_the_area(self):
        self.assertEqual(self.place(**OUT_OF_AREA).status_code, status.HTTP_400_BAD_REQUEST)
        self.set_store(
            service_area_center_lat=Decimal("7.190700"),
            service_area_center_lng=Decimal("125.455300"),
        )
        self.assertEqual(self.place(**OUT_OF_AREA).status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.place(**IN_AREA).status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_refusal_names_the_configured_city(self):
        self.set_store(service_area_city="Davao City")
        response = self.place(**OUT_OF_AREA)
        self.assertIn("Davao City", response.data["error"])
        self.assertNotIn("Kidapawan", response.data["error"])


class ConstantsFollowTheSettingsTests(OrderTestBase):
    url = reverse("constants")

    def get(self):
        self.client.force_authenticate(self.user)
        return self.client.get(self.url).data

    def test_the_shape_is_exactly_what_the_apps_already_read(self):
        data = self.get()

        self.assertEqual(
            sorted(data),
            sorted(
                [
                    "order_status",
                    "delivery_status",
                    "payment_type",
                    "payment_status",
                    "device_platform",
                    "order_status_transitions",
                    "delivery_fee",
                    "service_area",
                ]
            ),
        )
        self.assertEqual(
            sorted(data["service_area"]), ["center_lat", "center_lng", "city", "radius_km"]
        )
        # Types matter to a Dart client: fee is a string, coordinates numbers.
        self.assertIsInstance(data["delivery_fee"], str)
        self.assertIsInstance(data["service_area"]["center_lat"], float)
        self.assertIsInstance(data["service_area"]["center_lng"], float)
        self.assertIsInstance(data["service_area"]["radius_km"], float)
        self.assertIsInstance(data["service_area"]["city"], str)

    def test_default_values_are_unchanged(self):
        data = self.get()
        self.assertEqual(data["delivery_fee"], "30.00")
        self.assertEqual(data["service_area"]["radius_km"], 8.0)
        self.assertEqual(data["service_area"]["center_lat"], 7.0083)
        self.assertEqual(data["service_area"]["city"], "Kidapawan City")

    def test_it_reflects_an_edit_immediately(self):
        store = StoreSettings.load()
        store.delivery_fee = Decimal("42.50")
        store.service_area_radius_km = Decimal("12.5")
        store.service_area_city = "Digos City"
        store.save()

        data = self.get()

        self.assertEqual(data["delivery_fee"], "42.50")
        self.assertEqual(data["service_area"]["radius_km"], 12.5)
        self.assertEqual(data["service_area"]["city"], "Digos City")

    def test_it_reflects_an_edit_made_through_the_api(self):
        self.client.force_authenticate(self.admin)
        self.client.patch(
            reverse("admin-store-settings"), {"delivery_fee": "60"}, format="json"
        )
        self.assertEqual(self.get()["delivery_fee"], "60.00")


class DeliveryQuoteTests(OrderTestBase):
    url = reverse("orders:delivery-quote")

    def quote(self, **params):
        self.client.force_authenticate(self.user)
        return self.client.get(self.url, params)

    def test_flat_quote(self):
        response = self.quote(lat="7.0083", lng="125.0894")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "delivery_fee": "30.00",
                "distance_km": 0.0,
                "within_service_area": True,
                "mode": "flat",
            },
        )

    def test_per_km_quote_matches_what_the_order_will_charge(self):
        store = StoreSettings.load()
        store.delivery_fee = Decimal("20.00")
        store.delivery_fee_per_km = Decimal("5.00")
        store.save()

        quoted = self.quote(lat="7.0533", lng="125.0894").data
        self.place(delivery_latitude="7.053300", delivery_longitude="125.089400")

        self.assertEqual(quoted["mode"], "per_km")
        self.assertEqual(quoted["delivery_fee"], f"{Order.objects.get().delivery_fee:.2f}")

    def test_outside_the_area_is_reported_not_refused(self):
        response = self.quote(lat=OUT_OF_AREA["delivery_latitude"], lng=OUT_OF_AREA["delivery_longitude"])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["within_service_area"])

    def test_bad_parameters_are_a_400(self):
        self.assertEqual(self.quote().status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.quote(lat="x", lng="1").status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.quote(lat="7.0").status_code, status.HTTP_400_BAD_REQUEST)

    def test_customers_only(self):
        self.assertEqual(self.client.get(self.url, {"lat": 7, "lng": 125}).status_code, status.HTTP_401_UNAUTHORIZED)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(self.url, {"lat": 7, "lng": 125}).status_code, status.HTTP_403_FORBIDDEN)


class StoreSettingsAdminPageTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", "boss@example.com", PASSWORD)
        self.client.force_login(self.boss)

    def test_the_list_opens_the_single_row(self):
        response = self.client.get(reverse("admin:core_storesettings_changelist"))
        self.assertRedirects(
            response,
            reverse("admin:core_storesettings_change", args=[1]),
            fetch_redirect_response=False,
        )

    def test_the_form_renders_with_the_current_values(self):
        StoreSettings.load()
        response = self.client.get(reverse("admin:core_storesettings_change", args=[1]))
        self.assertContains(response, "Delivery fee")
        self.assertContains(response, "Payment gateway fees")

    def test_it_can_be_saved(self):
        store = StoreSettings.load()
        data = {
            "delivery_fee": "55.00",
            "delivery_fee_per_km": "0.00",
            "service_area_city": store.service_area_city,
            "service_area_center_lat": "7.008300",
            "service_area_center_lng": "125.089400",
            "service_area_radius_km": "8.00",
            "paymongo_fee_percent": "2.50",
            "paymongo_fee_fixed": "0.00",
            "maya_fee_percent": "1.50",
            "maya_fee_fixed": "0.00",
        }
        response = self.client.post(
            reverse("admin:core_storesettings_change", args=[1]), data, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StoreSettings.load().delivery_fee, Decimal("55.00"))

    def test_invalid_values_are_refused_by_the_form(self):
        store = StoreSettings.load()
        response = self.client.post(
            reverse("admin:core_storesettings_change", args=[1]),
            {
                "delivery_fee": "-5",
                "delivery_fee_per_km": "0",
                "service_area_city": "X",
                "service_area_center_lat": "7",
                "service_area_center_lng": "125",
                "service_area_radius_km": "0",
                "paymongo_fee_percent": "2.5",
                "paymongo_fee_fixed": "0",
                "maya_fee_percent": "1.5",
                "maya_fee_fixed": "0",
            },
        )
        self.assertEqual(response.status_code, 200)  # form redisplayed
        store.refresh_from_db()
        self.assertEqual(store.delivery_fee, Decimal("30.00"))

    def test_cannot_add_or_delete(self):
        StoreSettings.load()
        self.assertEqual(self.client.get(reverse("admin:core_storesettings_add")).status_code, 403)
        self.assertEqual(
            self.client.post(reverse("admin:core_storesettings_delete", args=[1]), {"post": "yes"}).status_code,
            403,
        )
        self.assertTrue(StoreSettings.objects.exists())

    def test_sidebar_links_to_it(self):
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, "Store settings")
