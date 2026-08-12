"""Rider endpoints (spec section 7)."""

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework import mixins, viewsets
from rest_framework import status as http
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from accounts.models import Rider
from core.maps import refresh_eta_from_rider
from core.notifications import notify_delivery_completed, notify_delivery_failed
from core.permissions import IsRiderUser
from orders.admin_api import apply_status_change
from orders.constants import (
    ACTIVE_DELIVERY_STATUSES,
    TERMINAL_DELIVERY_STATUSES,
    DeliveryStatus,
    OrderStatus,
    PaymentStatus,
    PaymentType,
)
from orders.models import Order
from payments.services import record_cod_payment

from .models import Delivery
from .serializers import (
    AvailabilitySerializer,
    DeliverySerializer,
    DeliveryStatusUpdateSerializer,
    ProofOfDeliverySerializer,
)

# Delivery milestones that stamp a timestamp on the delivery row.
DELIVERY_TIMESTAMPS = {
    DeliveryStatus.PICKED_UP: "picked_up_at",
    DeliveryStatus.DELIVERED: "delivered_at",
}


class RiderDeliveryViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    """
    GET   /api/rider/deliveries/                assigned + in progress
    GET   /api/rider/deliveries/history/        completed and failed
    GET   /api/rider/deliveries/{id}/           detail
    PATCH /api/rider/deliveries/{id}/status/    picked_up -> in_transit -> delivered
    POST  /api/rider/deliveries/{id}/proof/     multipart photo
    """

    permission_classes = [IsRiderUser]
    serializer_class = DeliverySerializer

    def get_queryset(self):
        """Scoped to the caller — a rider never sees another rider's work."""
        return (
            Delivery.objects.filter(rider__user=self.request.user)
            .select_related("order__customer__user", "rider__user")
            .prefetch_related("order__items__menu")
        )

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset().filter(
            status__in=ACTIVE_DELIVERY_STATUSES
        ).order_by("assigned_at")
        return Response(self.get_serializer(queryset, many=True).data)

    @action(detail=False, methods=["get"])
    def history(self, request):
        queryset = self.get_queryset().filter(
            status__in=TERMINAL_DELIVERY_STATUSES
        ).order_by("-delivered_at", "-assigned_at")
        return Response(self.get_serializer(queryset, many=True).data)

    # `status` would shadow the DRF status module, so pin path and name.
    @action(detail=True, methods=["patch"], url_path="status", url_name="status")
    def change_status(self, request, pk=None):
        with transaction.atomic():
            delivery = (
                Delivery.objects.select_for_update()
                .filter(pk=pk, rider__user=request.user)
                .first()
            )
            if delivery is None:
                return Response(
                    {"error": "No Delivery matches the given query."},
                    status=http.HTTP_404_NOT_FOUND,
                )

            serializer = DeliveryStatusUpdateSerializer(
                data=request.data, context={"delivery": delivery}
            )
            serializer.is_valid(raise_exception=True)
            new_status = serializer.validated_data["status"]

            order = Order.objects.select_for_update().get(pk=delivery.order_id)

            # The order enum documents `ready` as "Ready for rider pickup", so
            # a rider cannot collect food the kitchen has not finished.
            if new_status == DeliveryStatus.PICKED_UP and order.status != OrderStatus.READY:
                return Response(
                    {
                        "error": (
                            f"{order.order_number} is not ready for pickup yet "
                            f"(it is {order.get_status_display().lower()}). "
                            "The store marks an order ready when the kitchen is done."
                        )
                    },
                    status=http.HTTP_400_BAD_REQUEST,
                )

            fields = ["status"]
            delivery.status = new_status
            stamp = DELIVERY_TIMESTAMPS.get(new_status)
            if stamp and getattr(delivery, stamp) is None:
                setattr(delivery, stamp, timezone.now())
                fields.append(stamp)

            try:
                with transaction.atomic():
                    delivery.save(update_fields=fields)
            except IntegrityError:
                # uniq_rider_one_in_transit. The serializer check above will
                # normally have caught this; reaching here means another leg
                # started in the moment between the two.
                return Response(
                    {
                        "error": (
                            "Another delivery just started. Finish it before "
                            "starting this one."
                        )
                    },
                    status=http.HTTP_400_BAD_REQUEST,
                )

            self._sync_order(
                order, delivery, new_status, request.user, serializer.validated_data
            )

        # EDT trigger 2 of 2 (spec section 9): the rider is now moving, so
        # measure from where they actually are. Outside the transaction so a
        # Maps outage cannot roll back the status change. Explicitly NOT done
        # on the location ping, which fires every 10-15 seconds.
        if new_status == DeliveryStatus.IN_TRANSIT:
            refresh_eta_from_rider(order, delivery.rider, trigger="in_transit")

        # TODO(step 9): push FCM to the customer, and to the admin on completion.
        delivery = self.get_queryset().get(pk=delivery.pk)
        return Response(self.get_serializer(delivery).data)

    def _sync_order(self, order, delivery, new_status, rider_user, validated):
        """
        Keep the order in step with the delivery. The customer app tracks the
        order, not the delivery, so these have to move together.
        """
        if new_status == DeliveryStatus.PICKED_UP:
            apply_status_change(order, OrderStatus.OUT_FOR_DELIVERY, rider_user)

        elif new_status == DeliveryStatus.DELIVERED:
            apply_status_change(order, OrderStatus.DELIVERED, rider_user)
            if (
                order.payment_type == PaymentType.COD
                and order.payment_status != PaymentStatus.PAID
            ):
                # The rider collected the cash at the door. This writes the
                # invoice and flips payment_status; it is idempotent, so a
                # replayed completion cannot produce a second receipt.
                record_cod_payment(order, changed_by=rider_user)

            # The customer already got the "Delivered" push from
            # apply_status_change; this is the admin half of the pair.
            notify_delivery_completed(order)

        elif new_status == DeliveryStatus.FAILED:
            # The order is deliberately left where it is. A failed drop-off is
            # an admin decision — retry with another rider, or cancel.
            reason = validated.get("failure_reason", "")
            if reason and not order.cancel_reason:
                order.cancel_reason = reason
                order.save(update_fields=["cancel_reason"])

            # Nothing else moves this order, so without this push a failed
            # delivery would sit unnoticed until someone opened the dashboard.
            notify_delivery_failed(order, delivery)

    @action(
        detail=True,
        methods=["post"],
        parser_classes=[MultiPartParser, FormParser],
    )
    def proof(self, request, pk=None):
        delivery = self.get_queryset().filter(pk=pk).first()
        if delivery is None:
            return Response(
                {"error": "No Delivery matches the given query."},
                status=http.HTTP_404_NOT_FOUND,
            )

        if delivery.status == DeliveryStatus.ASSIGNED:
            return Response(
                {"error": "Pick the order up before uploading proof of delivery."},
                status=http.HTTP_400_BAD_REQUEST,
            )

        # Not partial: the photo is the whole point of this endpoint, and a
        # partial update would accept an empty body and report success.
        serializer = ProofOfDeliverySerializer(
            delivery, data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        delivery.refresh_from_db()
        return Response(
            DeliverySerializer(delivery, context={"request": request}).data
        )


@api_view(["POST"])
@permission_classes([IsRiderUser])
def location_ping(request):
    """
    POST /api/rider/location/ — body {"lat": ..., "lng": ...}

    The rider app calls this every 10-15 seconds, so it is deliberately the
    cheapest endpoint in the system: no serializer, no model instance loaded,
    no status log, no geofence. One UPDATE overwriting the rider's row, then
    204. Coordinates are overwritten, never appended.
    """
    data = request.data
    try:
        lat = float(data["lat"])
        lng = float(data["lng"])
    except (KeyError, TypeError, ValueError):
        return Response(
            {"error": "Send numeric 'lat' and 'lng'."},
            status=http.HTTP_400_BAD_REQUEST,
        )

    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
        return Response(
            {"error": "Coordinates out of range."},
            status=http.HTTP_400_BAD_REQUEST,
        )

    updated = Rider.objects.filter(user_id=request.user.pk).update(
        current_latitude=Decimal(f"{lat:.6f}"),
        current_longitude=Decimal(f"{lng:.6f}"),
        last_location_update=timezone.now(),
    )
    if not updated:
        return Response(
            {"error": "This account has no rider profile."},
            status=http.HTTP_404_NOT_FOUND,
        )

    return Response(status=http.HTTP_204_NO_CONTENT)


@api_view(["PATCH"])
@permission_classes([IsRiderUser])
def availability(request):
    """PATCH /api/rider/availability/ — body {"is_available": true|false}."""
    serializer = AvailabilitySerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    is_available = serializer.validated_data["is_available"]

    updated = Rider.objects.filter(user_id=request.user.pk).update(
        is_available=is_available
    )
    if not updated:
        return Response(
            {"error": "This account has no rider profile."},
            status=http.HTTP_404_NOT_FOUND,
        )

    return Response({"is_available": is_available})


@api_view(["GET"])
@permission_classes([IsRiderUser])
def dashboard(request):
    """
    GET /api/rider/dashboard/ — assigned count, completed count, earnings.

    "Today" is the local date in TIME_ZONE (Asia/Manila).
    """
    rider = (
        Rider.objects.filter(user_id=request.user.pk).select_related("user").first()
    )
    if rider is None:
        return Response(
            {"error": "This account has no rider profile."},
            status=http.HTTP_404_NOT_FOUND,
        )

    today = timezone.localdate()
    deliveries = Delivery.objects.filter(rider=rider)

    counts = deliveries.aggregate(
        active=Count("id", filter=Q(status__in=ACTIVE_DELIVERY_STATUSES)),
        completed=Count("id", filter=Q(status=DeliveryStatus.DELIVERED)),
        failed=Count("id", filter=Q(status=DeliveryStatus.FAILED)),
    )

    completed = deliveries.filter(status=DeliveryStatus.DELIVERED)
    total_earnings = completed.aggregate(s=Sum("rider_earnings"))["s"] or Decimal("0.00")
    today_qs = completed.filter(delivered_at__date=today)
    today_earnings = today_qs.aggregate(s=Sum("rider_earnings"))["s"] or Decimal("0.00")

    return Response(
        {
            "rider_id": rider.pk,
            "name": rider.user.get_full_name() or rider.user.username,
            "is_available": rider.is_available,
            "assigned": counts["active"],
            "completed": counts["completed"],
            "failed": counts["failed"],
            "earnings": {
                # Sum of the rider_earnings snapshots, so past pay never moves.
                "total": str(total_earnings),
                "today": str(today_earnings),
            },
            "today": {"completed": today_qs.count()},
            "current_location": {
                "latitude": str(rider.current_latitude)
                if rider.current_latitude is not None
                else None,
                "longitude": str(rider.current_longitude)
                if rider.current_longitude is not None
                else None,
                "updated_at": rider.last_location_update,
            },
        }
    )
