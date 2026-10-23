"""Mounted at /api/orders/ by config.urls."""

from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import OrderViewSet, delivery_quote

app_name = "orders"

router = DefaultRouter()
router.register(r"", OrderViewSet, basename="order")

urlpatterns = [
    # Before the router: its "<pk>/" route would swallow this path.
    path("delivery-quote/", delivery_quote, name="delivery-quote"),
    *router.urls,
]
