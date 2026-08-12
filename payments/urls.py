"""Mounted at /api/payments/ by config.urls."""

from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("checkout/", views.checkout, name="checkout"),
    path("webhook/", views.webhook, name="webhook"),
    # PayMongo redirect targets. Public pages, no auth — they carry no data.
    path("return/success/", views.return_success, name="return-success"),
    path("return/failed/", views.return_failed, name="return-failed"),
]
