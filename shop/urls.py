"""Mounted at /order/ by config.urls: the customer web app (PWA)."""

from django.urls import path

from . import views

app_name = "shop"

urlpatterns = [
    path("", views.menu, name="menu"),
    path("cart/", views.cart, name="cart"),
    path("checkout/", views.checkout, name="checkout"),
    path("orders/", views.orders, name="orders"),
    # No trailing slash: these are the exact paths push notifications and the
    # payment return pages link to (PWA_BASE_URL + "/orders/<id>").
    path("orders/<int:pk>", views.order_detail, name="order"),
    path("payment/<str:outcome>", views.payment_return, name="payment-return"),
    path("profile/", views.profile, name="profile"),
    path("profile/edit/", views.profile_edit, name="profile-edit"),
    path("sign-in/", views.sign_in, name="sign-in"),
    path("sign-up/", views.sign_up, name="sign-up"),
    path("sign-out/", views.sign_out, name="sign-out"),
    path("push/register/", views.push_register, name="push-register"),
    path("push/unregister/", views.push_unregister, name="push-unregister"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="sw"),
    path("icons/<str:name>", views.icon, name="icon"),
]
