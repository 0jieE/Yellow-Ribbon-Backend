"""
The customer web app at /order/: a Progressive Web App for iPhones, which
have no Android APK to install.

Server-rendered Django pages with a normal session login. Ordering, paying
and cancelling go through the very same code as the Android app's API
(OrderCreateSerializer, start_checkout, cancel_for_customer), so both apps
follow one set of rules. The only JavaScript is what a browser cannot do
without it: the delivery-pin map, live rider tracking and push notifications.
"""

import json
import logging
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.models import Customer, DevicePlatform, DeviceToken, User
from accounts.serializers import RegisterSerializer
from core.geofence import distance_from_service_center_km
from core.models import StoreSettings
from menu.models import Menu
from orders.constants import OrderStatus, PaymentStatus, PaymentType
from orders.models import Order
from orders.serializers import OrderCreateSerializer, payment_summary
from orders.views import cancel_for_customer
from payments import maya
from payments.services import sync_maya_order
from payments.views import start_checkout

from .cart import Cart
from .forms import CheckoutForm, ProfileForm, SignInForm, SignUpForm

logger = logging.getLogger(__name__)

PUSH_SESSION_KEY = "shop_push_token"
ICONS = {"icon-192.png", "icon-512.png", "icon-maskable-512.png", "badge-72.png", "apple-touch-icon.png", "favicon-64.png"}

# The customer's view of the order's progress, in order.
STEPS = [
    (OrderStatus.PENDING, "Order received"),
    (OrderStatus.CONFIRMED, "Confirmed"),
    (OrderStatus.PREPARING, "In the oven"),
    (OrderStatus.READY, "Ready for the rider"),
    (OrderStatus.OUT_FOR_DELIVERY, "On the way"),
    (OrderStatus.DELIVERED, "Delivered"),
]
LIVE_STATUSES = {OrderStatus.PENDING_PAYMENT, OrderStatus.PENDING, OrderStatus.CONFIRMED,
                 OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY}


def _is_customer(user):
    return user.is_authenticated and user.is_active and user.is_customer and Customer.objects.filter(user=user).exists()


def customer_required(view):
    """Signed in with a customer account, or off to the sign-in page."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), reverse("shop:sign-in"))
        if not _is_customer(request.user):
            return render(request, "shop/not_customer.html", status=403)
        return view(request, *args, **kwargs)

    return wrapped


def _flatten(detail):
    """Every message in a DRF error detail, as plain strings."""
    if isinstance(detail, dict):
        return [m for value in detail.values() for m in _flatten(value)]
    if isinstance(detail, (list, tuple)):
        return [m for value in detail for m in _flatten(value)]
    return [str(detail)]


def _copy_errors(form, errors):
    """Put a serializer's errors on the matching form fields, the rest on the form."""
    for field, detail in errors.items():
        target = field if field in form.fields else None
        for message in _flatten(detail):
            form.add_error(target, message)


# -- menu and cart -----------------------------------------------------------


@require_http_methods(["GET", "POST"])
def menu(request):
    if request.method == "POST":
        menu_id = request.POST.get("menu_id", "")
        item = None
        if menu_id.isascii() and menu_id.isdigit():
            item = Menu.objects.filter(pk=int(menu_id), is_available=True).first()
        if item is None:
            messages.error(request, "That item is not available right now.")
        else:
            Cart(request).add(item.pk)
            messages.success(request, f"Added {item.name} to your cart.")
        return redirect(f"{reverse('shop:menu')}#item-{item.pk}" if item else reverse("shop:menu"))

    return render(request, "shop/menu.html", {
        "menu": Menu.objects.order_by("-is_available", "name"),
        "store": StoreSettings.load(),
        "tab": "menu",
    })


@require_http_methods(["GET", "POST"])
def cart(request):
    basket = Cart(request)
    if request.method == "POST":
        try:
            quantity = int(request.POST.get("quantity", "0"))
        except ValueError:
            quantity = 0
        basket.set(request.POST.get("menu_id", ""), quantity)
        return redirect("shop:cart")

    items = basket.items()
    return render(request, "shop/cart.html", {
        "items": items,
        "subtotal": basket.subtotal(items),
        "has_unavailable": any(not i["menu"].is_available for i in items),
        "store": StoreSettings.load(),
        "tab": "cart",
    })


# -- checkout ----------------------------------------------------------------


def _fee_for(store, lat, lng):
    if lat is None or lng is None:
        return store.delivery_fee_for(None)
    return store.delivery_fee_for(distance_from_service_center_km(lat, lng, store))


def _open_payment(request, order):
    """Send the customer to GCash or Maya, or back to the order with the reason it failed."""
    response = start_checkout(request, order, web=True)
    if response.status_code < 300:
        return redirect(response.data["checkout_url"])
    messages.error(request, response.data.get("error", "The payment page could not be opened."))
    return redirect("shop:order", pk=order.pk)


@customer_required
@require_http_methods(["GET", "POST"])
def checkout(request):
    basket = Cart(request)
    items = basket.items()
    available = [i for i in items if i["menu"].is_available]
    if not available:
        messages.info(request, "Your cart is empty. Add something from the menu first.")
        return redirect("shop:cart")

    customer = request.user.customer
    store = StoreSettings.load()

    if request.method == "POST":
        form = CheckoutForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            serializer = OrderCreateSerializer(
                data={
                    "items": basket.as_order_items(available),
                    "delivery_address": data["delivery_address"],
                    "delivery_latitude": str(data["delivery_latitude"]),
                    "delivery_longitude": str(data["delivery_longitude"]),
                    "delivery_notes": data["delivery_notes"],
                    "payment_type": data["payment_type"],
                },
                context={"request": request},
            )
            if serializer.is_valid():
                order = serializer.save()
                basket.clear()
                if data["remember_address"]:
                    customer.default_address = data["delivery_address"]
                    customer.default_latitude = data["delivery_latitude"]
                    customer.default_longitude = data["delivery_longitude"]
                    customer.save(update_fields=["default_address", "default_latitude", "default_longitude"])
                logger.info("Web order %s placed by %s.", order.order_number, request.user.username)
                if order.payment_type == PaymentType.COD:
                    messages.success(request, f"Order {order.order_number} placed. Pay the rider in cash when it arrives.")
                    return redirect("shop:order", pk=order.pk)
                return _open_payment(request, order)
            _copy_errors(form, serializer.errors)
    else:
        form = CheckoutForm(initial={
            "delivery_address": customer.default_address,
            "delivery_latitude": customer.default_latitude,
            "delivery_longitude": customer.default_longitude,
        })

    subtotal = basket.subtotal(available)
    fee = _fee_for(store, customer.default_latitude, customer.default_longitude)
    return render(request, "shop/checkout.html", {
        "form": form,
        "items": available,
        "subtotal": subtotal,
        "fee": fee,
        "total": subtotal + fee,
        "store": store,
        "maya_enabled": bool(settings.MAYA_PUBLIC_KEY and settings.MAYA_SECRET_KEY),
        "map_config": {
            "center": [float(store.service_area_center_lat), float(store.service_area_center_lng)],
            "radiusKm": store.radius_km,
            "pin": (
                [float(customer.default_latitude), float(customer.default_longitude)]
                if customer.default_latitude is not None and customer.default_longitude is not None
                else None
            ),
            "quoteUrl": reverse("orders:delivery-quote"),
            "subtotal": str(subtotal),
        },
        "tab": "cart",
    })


# -- orders ------------------------------------------------------------------


@customer_required
@require_GET
def orders(request):
    mine = Order.objects.filter(customer__user=request.user).order_by("-placed_at")
    return render(request, "shop/orders.html", {"orders": mine, "tab": "orders"})


def _steps(order):
    if order.status in (OrderStatus.CANCELLED, OrderStatus.PENDING_PAYMENT):
        return []
    reached = [s for s, _ in STEPS].index(order.status) if order.status in dict(STEPS) else -1
    return [
        {"label": label, "done": index <= reached, "current": index == reached}
        for index, (_, label) in enumerate(STEPS)
    ]


@customer_required
@require_http_methods(["GET", "POST"])
def order_detail(request, pk):
    order = get_object_or_404(
        Order.objects.select_related("invoice", "delivery__rider__user").prefetch_related("items__menu"),
        pk=pk,
        customer__user=request.user,
    )
    paid = order.payment_status == PaymentStatus.PAID
    can_pay = order.status == OrderStatus.PENDING_PAYMENT and order.payment_type != PaymentType.COD and not paid

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "pay" and can_pay:
            return _open_payment(request, order)
        if action == "cancel":
            _, error = cancel_for_customer(request.user, order.pk, request.POST.get("reason", ""))
            if error:
                messages.error(request, error)
            else:
                messages.success(request, f"{order.order_number} is cancelled.")
        return redirect("shop:order", pk=order.pk)

    return render(request, "shop/order_detail.html", {
        "order": order,
        "items": order.items.all(),
        "steps": _steps(order),
        "can_pay": can_pay,
        "payment": payment_summary(order),
        "live": order.status in LIVE_STATUSES,
        "track_config": {
            "orderUrl": reverse("orders:order-detail", args=[order.pk]),
            "status": order.status,
            "destination": [float(order.delivery_latitude), float(order.delivery_longitude)],
        },
        "tab": "orders",
    })


@require_GET
def payment_return(request, outcome):
    """
    Where GCash or Maya sends a web customer after paying (via the payment
    return pages, which have already verified a Maya payment with Maya).

    On iPhone the payment page may have opened in Safari rather than in the
    installed app, where nobody is signed in; then this just says how it went
    and to go back to the app, which picks the new status up by itself.
    """
    if outcome not in ("success", "failed"):
        raise Http404
    order_id = request.GET.get("order", "")
    order = None
    if order_id.isascii() and order_id.isdigit() and _is_customer(request.user):
        order = Order.objects.filter(pk=int(order_id), customer__user=request.user).first()

    if order is None:
        return render(request, "shop/payment_return.html", {"outcome": outcome})

    if order.payment_type == PaymentType.MAYA and order.payment_status != PaymentStatus.PAID:
        try:
            sync_maya_order(order)
            order.refresh_from_db()
        except maya.MayaError as exc:
            logger.warning("Maya sync on web return failed for %s: %s", order.order_number, exc)

    if order.payment_status == PaymentStatus.PAID:
        messages.success(request, f"Payment received for {order.order_number}. The kitchen has your order.")
    elif outcome == "failed":
        messages.error(request, "The payment was not completed. You can try again or cancel the order.")
    else:
        messages.info(request, "We are confirming your payment. This page updates by itself.")
    return redirect("shop:order", pk=order.pk)


# -- account -----------------------------------------------------------------


@require_http_methods(["GET", "POST"])
def sign_in(request):
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if not next_url.startswith("/order/"):
        next_url = reverse("shop:menu")

    form = SignInForm(request, data=request.POST or None)
    refused = None
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        if _is_customer(user):
            login(request, user)
            return redirect(next_url)
        refused = (
            "That is a rider or staff account. This app is for customers; riders use "
            "the Ribbon Rider app."
        )
    return render(request, "shop/sign_in.html", {"form": form, "next": next_url, "refused": refused})


@require_http_methods(["GET", "POST"])
def sign_up(request):
    form = SignUpForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        serializer = RegisterSerializer(data=form.cleaned_data)
        if serializer.is_valid():
            user = serializer.save()
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            messages.success(request, f"Welcome, {user.first_name or user.username}! Pick something from the menu.")
            return redirect("shop:menu")
        _copy_errors(form, serializer.errors)
    return render(request, "shop/sign_up.html", {"form": form})


@require_POST
def sign_out(request):
    token = request.session.get(PUSH_SESSION_KEY)
    if token and request.user.is_authenticated:
        # This phone should stop getting this account's notifications.
        DeviceToken.objects.filter(user=request.user, fcm_token=token).delete()
    logout(request)
    return redirect("shop:menu")


@customer_required
@require_http_methods(["GET", "POST"])
def profile(request):
    user = request.user
    form = ProfileForm(
        request.POST or None,
        initial={"first_name": user.first_name, "last_name": user.last_name, "phone": user.phone, "email": user.email},
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        email = data["email"]
        if email and User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
            form.add_error("email", "That email is already in use.")
        else:
            for field in ("first_name", "last_name", "phone", "email"):
                setattr(user, field, data[field])
            user.save(update_fields=["first_name", "last_name", "phone", "email"])
            messages.success(request, "Your details are saved.")
            return redirect("shop:profile")
    return render(request, "shop/profile.html", {
        "form": form,
        "customer": user.customer,
        "push_on": DeviceToken.objects.filter(
            user=user, platform=DevicePlatform.WEB, fcm_token=request.session.get(PUSH_SESSION_KEY, "")
        ).exists(),
        "tab": "profile",
    })


# -- push notifications ------------------------------------------------------


def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return {}


@require_POST
def push_register(request):
    """Save this phone's FCM web token for the signed-in customer (called by push.js)."""
    if not _is_customer(request.user):
        return JsonResponse({"error": "Sign in with a customer account first."}, status=403)
    token = str(_json_body(request).get("token", "")).strip()
    if not token or len(token) > 255:
        return JsonResponse({"error": "No notification token was given."}, status=400)
    with transaction.atomic():
        # Keyed on the token, as in the API: a shared phone moves to whoever signed in last.
        DeviceToken.objects.update_or_create(
            fcm_token=token, defaults={"user": request.user, "platform": DevicePlatform.WEB}
        )
    request.session[PUSH_SESSION_KEY] = token
    return JsonResponse({"ok": True})


@require_POST
def push_unregister(request):
    token = request.session.pop(PUSH_SESSION_KEY, None)
    if token and request.user.is_authenticated:
        DeviceToken.objects.filter(user=request.user, fcm_token=token).delete()
    return JsonResponse({"ok": True})


# -- the bits that make it an installable app --------------------------------


@require_GET
def manifest(request):
    data = {
        "name": "Yellow Ribbon Pizza",
        "short_name": "Yellow Ribbon",
        "description": "Order pizza from Yellow Ribbon in Kidapawan City.",
        "id": "/order/",
        "start_url": "/order/",
        "scope": "/order/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#F4B708",
        "theme_color": "#F4B708",
        "icons": [
            {"src": static("shop/icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": static("shop/icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {"src": static("shop/icons/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return JsonResponse(data, content_type="application/manifest+json")


@require_GET
def service_worker(request):
    """
    /order/sw.js: served from here, not /static/, so its scope covers /order/.

    It only receives pushes: Firebase's SDK shows each notification (every
    push from the backend carries one, as iOS requires) and opens the link in
    it when tapped.
    """
    body = render(request, "shop/sw.js", {
        "firebase_config": json.dumps(settings.FIREBASE_WEB_CONFIG),
        "firebase_version": FIREBASE_JS_VERSION,
    }).content
    response = HttpResponse(body, content_type="application/javascript")
    response["Cache-Control"] = "no-cache"
    response["Service-Worker-Allowed"] = "/order/"
    return response


@require_GET
def icon(request, name):
    """/order/icons/<name>: the paths the web push config names, sent to the static file."""
    if name not in ICONS:
        raise Http404
    return redirect(static(f"shop/icons/{name}"))


FIREBASE_JS_VERSION = "10.14.1"


def push_context(request):
    """Template context processor: what push.js needs on every /order/ page."""
    if not request.path.startswith("/order/"):
        return {}
    return {
        "push_config": {
            "firebase": settings.FIREBASE_WEB_CONFIG,
            "vapidKey": settings.FIREBASE_WEB_VAPID_KEY or None,
            "registerUrl": reverse("shop:push-register"),
            "unregisterUrl": reverse("shop:push-unregister"),
            "swUrl": reverse("shop:sw"),
        },
        "firebase_version": FIREBASE_JS_VERSION,
        "signed_in_customer": _is_customer(request.user),
    }

