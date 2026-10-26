"""Admin-facing order endpoints (spec section 7)."""

from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework import status as http
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from accounts.models import Rider
from core.notifications import (
    notify_order_status,
    notify_rider_assigned,
    notify_rider_order_cancelled,
    notify_rider_order_ready,
)
from core.pagination import AdminOrderPagination
from core.permissions import IsAdminUser
from delivery.models import Delivery

from .admin_serializers import (
    AdminOrderSerializer,
    AssignRiderSerializer,
    OrderStatusUpdateSerializer,
)
from .constants import (
    ACTIVE_DELIVERY_STATUSES,
    ORDER_STATUS,
    TERMINAL_DELIVERY_STATUSES,
    DeliveryStatus,
    OrderStatus,
)
from .models import Order, OrderStatusLog

# Setting these alongside the status change keeps the order timeline honest
# without a separate bookkeeping call at every call site.
STATUS_TIMESTAMPS = {
    OrderStatus.CONFIRMED: "confirmed_at",
    OrderStatus.DELIVERED: "delivered_at",
    OrderStatus.CANCELLED: "cancelled_at",
}


def _notify_assigned_rider_of_cancellation(order):
    """
    Tell a rider still holding this order to stop.

    Called for every cancellation, whoever triggered it. A rider whose
    delivery is already delivered or failed is not disturbed.
    """
    delivery = getattr(order, "delivery", None)
    if delivery is None or delivery.status in TERMINAL_DELIVERY_STATUSES:
        return
    notify_rider_order_cancelled(order, delivery.rider)


def _notify_assigned_rider_order_ready(order):
    """
    Tell the rider holding this order that the food is ready.

    Only while the delivery is still `assigned`: a rider who has already
    picked it up has no use for "pick it up at the store". An unassigned order
    has nobody to tell; the push goes out when someone is assigned instead.
    """
    delivery = getattr(order, "delivery", None)
    if delivery is None or delivery.status != DeliveryStatus.ASSIGNED:
        return
    notify_rider_order_ready(order, delivery.rider)


def apply_status_change(order, new_status, changed_by, reason="", notify_customer=True):
    """
    Move an order and record it. Callers must already have validated the
    transition — this does the writing, not the deciding.
    """
    fields = ["status"]
    order.status = new_status

    stamp = STATUS_TIMESTAMPS.get(new_status)
    if stamp and getattr(order, stamp) is None:
        setattr(order, stamp, timezone.now())
        fields.append(stamp)

    if new_status == OrderStatus.CANCELLED and reason:
        order.cancel_reason = reason
        fields.append("cancel_reason")

    order.save(update_fields=fields)
    OrderStatusLog.objects.create(order=order, status=new_status, changed_by=changed_by)

    # Every order status change funnels through here — admin actions, rider
    # pickup and completion, the PayMongo webhook, and the stale-order
    # reaper — so one hook covers the customer for all of them.
    if notify_customer:
        notify_order_status(order)

    # The rider needs to know too, and is not the customer, so this is not
    # gated on notify_customer either.
    if new_status == OrderStatus.READY:
        _notify_assigned_rider_order_ready(order)

    # Not gated on notify_customer: a rider mid-route has to be told the
    # order is off even when the customer cancelled it themselves and needs
    # no push of their own.
    if new_status == OrderStatus.CANCELLED:
        _notify_assigned_rider_of_cancellation(order)

        # A paid online order is refunded. This only queues it; the gateway is
        # called after the cancel commits, so a rollback cannot refund a live
        # order, and a failed refund cannot undo the cancel.
        from payments.refunds import schedule_refund

        schedule_refund(order)

    return order


class AdminOrderViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """
    GET   /api/admin/orders/                all orders, ?status= filter
    GET   /api/admin/orders/{id}/           detail
    PATCH /api/admin/orders/{id}/status/    advance status
    POST  /api/admin/orders/{id}/assign/    body {"rider_id": N}
    """

    permission_classes = [IsAdminUser]
    serializer_class = AdminOrderSerializer
    pagination_class = AdminOrderPagination

    def get_queryset(self):
        return (
            Order.objects.all()
            .select_related("customer__user", "delivery__rider__user", "invoice")
            .prefetch_related("items__menu", "status_logs__changed_by")
        )

    def filter_queryset(self, queryset):
        """?status=pending, ?status=pending,confirmed, or repeated ?status=."""
        raw = self.request.query_params.getlist("status")
        wanted = [s.strip() for value in raw for s in value.split(",") if s.strip()]
        if not wanted:
            return queryset

        unknown = sorted(set(wanted) - set(ORDER_STATUS))
        if unknown:
            raise ValidationError(
                {
                    "status": (
                        f"Unknown status '{unknown[0]}'. "
                        f"Valid values: {', '.join(ORDER_STATUS)}."
                    )
                }
            )
        return queryset.filter(status__in=wanted)

    def _locked_order(self, pk):
        # Deliberately not get_queryset(): its select_related("delivery__...")
        # is an outer join, and Postgres refuses FOR UPDATE on the nullable
        # side of one. The row is re-read with the joins for the response.
        return Order.objects.select_for_update().filter(pk=pk).first()

    def _fresh_response(self, order, status_code=http.HTTP_200_OK):
        order = self.get_queryset().get(pk=order.pk)
        serializer = AdminOrderSerializer(order, context=self.get_serializer_context())
        return Response(serializer.data, status=status_code)

    # The method cannot be called `status` without shadowing the DRF status
    # module, so path and route name are pinned explicitly.
    @action(detail=True, methods=["patch"], url_path="status", url_name="status")
    def change_status(self, request, pk=None):
        with transaction.atomic():
            order = self._locked_order(pk)
            if order is None:
                return Response(
                    {"error": "No Order matches the given query."},
                    status=http.HTTP_404_NOT_FOUND,
                )

            serializer = OrderStatusUpdateSerializer(
                data=request.data, context={"order": order}
            )
            serializer.is_valid(raise_exception=True)

            apply_status_change(
                order,
                serializer.validated_data["status"],
                changed_by=request.user,
                reason=request.data.get("cancel_reason", ""),
            )

        # The customer push is fired by apply_status_change above.
        return self._fresh_response(order)

    @action(detail=True, methods=["post"], url_path="refund", url_name="refund")
    def refund(self, request, pk=None):
        """
        POST /api/admin/orders/{id}/refund/ — retry a cancelled order's refund.

        For a refund that failed, one still pending (asks the gateway how it is
        getting on), or a cancelled paid order that never got one. Synchronous:
        the response is the order with the refund's new state.
        """
        from payments.refunds import RefundError, retry_refund

        order = Order.objects.filter(pk=pk).first()
        if order is None:
            return Response(
                {"error": "No Order matches the given query."},
                status=http.HTTP_404_NOT_FOUND,
            )
        try:
            retry_refund(order)
        except RefundError as exc:
            return Response({"error": str(exc)}, status=http.HTTP_400_BAD_REQUEST)
        return self._fresh_response(order)

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        with transaction.atomic():
            order = self._locked_order(pk)
            if order is None:
                return Response(
                    {"error": "No Order matches the given query."},
                    status=http.HTTP_404_NOT_FOUND,
                )

            serializer = AssignRiderSerializer(
                data=request.data, context={"order": order}
            )
            serializer.is_valid(raise_exception=True)

            rider = serializer.validated_data["rider"]
            Delivery.objects.create(
                order=order,
                rider=rider,
                rider_earnings=serializer.rider_earnings(),
            )
            notify_rider_assigned(order, rider)

        # No ETA here. A rider taking a batch will not set off for the first
        # order immediately, so an assignment-time figure would be wrong the
        # moment it was written. It is computed when the leg actually starts
        # — see the in_transit trigger in delivery/views.py.
        return self._fresh_response(order, http.HTTP_201_CREATED)


def dashboard_summary():
    """
    Counts and revenue for today, as the plain dict the API returns.

    Shared by GET /api/admin/dashboard/ and the dashboard on /admin/, so the
    two can never disagree. "Today" is the local date in TIME_ZONE
    (Asia/Manila), not UTC.
    """
    today = timezone.localdate()

    placed_today = Order.objects.filter(placed_at__date=today)
    delivered_today = Order.objects.filter(
        status=OrderStatus.DELIVERED, delivered_at__date=today
    )

    money = delivered_today.aggregate(
        gross=Sum("total"), fees=Sum("delivery_fee"), food=Sum("subtotal")
    )

    # One grouped query rather than one COUNT per status.
    counts = dict(
        Order.objects.values_list("status")
        .annotate(n=Count("id"))
        .values_list("status", "n")
    )
    status_counts = {s: counts.get(s, 0) for s in ORDER_STATUS}

    rider_stats = Rider.objects.aggregate(
        total=Count("id"),
        on_duty=Count("id", filter=Q(is_available=True)),
    )
    on_delivery = (
        Delivery.objects.filter(status__in=ACTIVE_DELIVERY_STATUSES)
        .values("rider_id")
        .distinct()
        .count()
    )

    return {
        "date": today.isoformat(),
        "today": {
            "orders_placed": placed_today.count(),
            "orders_delivered": delivered_today.count(),
            "orders_cancelled": placed_today.filter(
                status=OrderStatus.CANCELLED
            ).count(),
            # Revenue is recognised on delivery, not on placement, so a
            # pending order does not inflate the day's takings.
            "revenue": str(money["gross"] or Decimal("0.00")),
            "food_sales": str(money["food"] or Decimal("0.00")),
            "delivery_fees": str(money["fees"] or Decimal("0.00")),
        },
        "status_counts": status_counts,
        "active_orders": sum(
            count
            for s, count in status_counts.items()
            if s not in (OrderStatus.DELIVERED, OrderStatus.CANCELLED)
        ),
        "riders": {
            "total": rider_stats["total"],
            "on_duty": rider_stats["on_duty"],
            "on_delivery": on_delivery,
        },
    }


@api_view(["GET"])
@permission_classes([IsAdminUser])
def dashboard(_request):
    """GET /api/admin/dashboard/ — counts and revenue for today."""
    return Response(dashboard_summary())
