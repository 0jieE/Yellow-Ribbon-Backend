"""Customer-facing order endpoints (spec section 7)."""

from decimal import Decimal, InvalidOperation

from django.db import transaction
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import DevicePlatform
from core.geofence import distance_from_service_center_km, is_within_service_area
from core.models import StoreSettings
from core.notifications import notify_order_cancelled_by_customer
from core.permissions import IsCustomerUser

from .admin_api import apply_status_change

from .constants import (
    DELIVERY_STATUS,
    ORDER_STATUS,
    ORDER_STATUS_TRANSITIONS,
    PAYMENT_STATUS,
    PAYMENT_TYPE,
    OrderStatus,
)
from .models import Order
from .serializers import OrderCancelSerializer, OrderCreateSerializer, OrderSerializer


class OrderViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    POST /api/orders/              place an order
    GET  /api/orders/              own orders
    GET  /api/orders/{id}/         own order detail
    POST /api/orders/{id}/cancel/  only while status is pending
    """

    permission_classes = [IsCustomerUser]

    def get_serializer_class(self):
        if self.action == "create":
            return OrderCreateSerializer
        if self.action == "cancel":
            return OrderCancelSerializer
        return OrderSerializer

    def get_queryset(self):
        """
        Scoped to the caller here, not in the serializer, so there is no
        route by which a customer reads someone else's order.
        """
        return (
            Order.objects.filter(customer__user=self.request.user)
            .prefetch_related("items__menu", "status_logs__changed_by")
            .select_related("customer", "delivery__rider__user", "invoice")
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        with transaction.atomic():
            # Not get_queryset(): its select_related("delivery__...") is an
            # outer join, and Postgres refuses FOR UPDATE on the nullable side.
            order = (
                Order.objects.select_for_update()
                .filter(pk=pk, customer__user=request.user)
                .first()
            )
            if order is None:
                return Response(
                    {"error": "No Order matches the given query."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if not order.is_cancellable_by_customer:
                return Response(
                    {
                        "error": (
                            f"An order that is already {order.get_status_display().lower()} "
                            "can no longer be cancelled. Please contact the store."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            # Shares the timestamping, audit log and rider stand-down push
            # with every other status change. The customer gets no push of
            # their own — they are the one who pressed cancel.
            apply_status_change(
                order,
                OrderStatus.CANCELLED,
                changed_by=request.user,
                reason=serializer.validated_data.get("cancel_reason", ""),
                notify_customer=False,
            )

            # The kitchen may already be working on it.
            notify_order_cancelled_by_customer(order)

        return Response(OrderSerializer(order, context={"request": request}).data)


@api_view(["GET"])
@permission_classes([IsCustomerUser])
def delivery_quote(request):
    """
    GET /api/orders/delivery-quote/?lat=..&lng=.. — the fee for an address.

    Flat fees are the same everywhere, but with a per-km rate set the fee
    depends on the address, and the checkout screen should show the real
    figure before the customer places the order.
    """
    try:
        lat = Decimal(request.query_params["lat"])
        lng = Decimal(request.query_params["lng"])
    except (KeyError, InvalidOperation):
        return Response(
            {"error": "lat and lng are required numbers."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    store = StoreSettings.load()
    distance = distance_from_service_center_km(lat, lng, store)
    return Response(
        {
            "delivery_fee": f"{store.delivery_fee_for(distance):.2f}",
            "distance_km": round(distance, 2),
            "within_service_area": is_within_service_area(lat, lng, store),
            "mode": "per_km" if store.delivery_fee_per_km else "flat",
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def constants(_request):
    """
    GET /api/constants/ — one source of truth for all three Flutter apps
    (spec section 5).
    """
    store = StoreSettings.load()
    return Response(
        {
            "order_status": ORDER_STATUS,
            "delivery_status": DELIVERY_STATUS,
            "payment_type": PAYMENT_TYPE,
            "payment_status": PAYMENT_STATUS,
            "device_platform": [c.value for c in DevicePlatform],
            "order_status_transitions": {
                current: sorted(str(s) for s in allowed)
                for current, allowed in ORDER_STATUS_TRANSITIONS.items()
            },
            "delivery_fee": f"{store.delivery_fee:.2f}",
            "service_area": {
                "center_lat": store.center[0],
                "center_lng": store.center[1],
                "radius_km": store.radius_km,
                "city": store.service_area_city,
            },
        }
    )
