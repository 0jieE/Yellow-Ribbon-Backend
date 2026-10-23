"""
Admin API routes, mounted at /api/admin/ (spec section 7).

Separate from django.contrib.admin — this is the JSON API the Flutter
admin_app consumes.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from accounts.admin_api import AdminRiderViewSet
from core.store_api import store_settings
from orders.admin_api import AdminOrderViewSet, dashboard
from orders.reports import menu_report, riders_report, sales_report

router = DefaultRouter()
router.register(r"orders", AdminOrderViewSet, basename="admin-order")
router.register(r"riders", AdminRiderViewSet, basename="admin-rider")

urlpatterns = [
    path("dashboard/", dashboard, name="admin-dashboard"),
    path("store-settings/", store_settings, name="admin-store-settings"),
    # Date-range reporting. The dashboard covers today only.
    path("reports/sales/", sales_report, name="admin-report-sales"),
    path("reports/riders/", riders_report, name="admin-report-riders"),
    path("reports/menu/", menu_report, name="admin-report-menu"),
    path("", include(router.urls)),
]
