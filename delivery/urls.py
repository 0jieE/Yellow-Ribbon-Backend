"""Mounted at /api/rider/ by config.urls."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "delivery"

router = DefaultRouter()
router.register(r"deliveries", views.RiderDeliveryViewSet, basename="rider-delivery")

urlpatterns = [
    path("location/", views.location_ping, name="location"),
    path("availability/", views.availability, name="availability"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("", include(router.urls)),
]
