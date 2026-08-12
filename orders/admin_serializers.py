"""Serializers for the admin-facing order endpoints (spec section 7)."""

from decimal import Decimal

from django.conf import settings
from rest_framework import serializers

from accounts.models import Rider

from .constants import OrderStatus, allowed_transitions
from .models import Order
from .serializers import OrderItemSerializer, OrderStatusLogSerializer


class AdminOrderSerializer(serializers.ModelSerializer):
    """Everything the admin app shows in the queue and on the detail screen."""

    items = OrderItemSerializer(many=True, read_only=True)
    status_logs = OrderStatusLogSerializer(many=True, read_only=True)

    customer_name = serializers.SerializerMethodField()
    customer_phone = serializers.CharField(
        source="customer.user.phone", read_only=True, default=""
    )
    next_statuses = serializers.SerializerMethodField()
    rider = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id",
            "order_number",
            "status",
            "payment_type",
            "payment_status",
            "subtotal",
            "delivery_fee",
            "total",
            "delivery_address",
            "delivery_latitude",
            "delivery_longitude",
            "delivery_notes",
            "eta_minutes",
            "eta_at",
            "placed_at",
            "confirmed_at",
            "delivered_at",
            "cancelled_at",
            "cancel_reason",
            "customer",
            "customer_name",
            "customer_phone",
            "next_statuses",
            "rider",
            "items",
            "status_logs",
        ]
        read_only_fields = fields

    def get_customer_name(self, obj):
        user = obj.customer.user
        return user.get_full_name() or user.username

    def get_next_statuses(self, obj):
        """Lets the admin app render only the buttons that will be accepted."""
        return [str(s) for s in allowed_transitions(obj.status)]

    def get_rider(self, obj):
        delivery = getattr(obj, "delivery", None)
        if delivery is None:
            return None
        user = delivery.rider.user
        return {
            "delivery_id": delivery.pk,
            "rider_id": delivery.rider_id,
            "username": user.username,
            "name": user.get_full_name() or user.username,
            "phone": user.phone,
            "delivery_status": delivery.status,
            "rider_earnings": str(delivery.rider_earnings),
        }


class OrderStatusUpdateSerializer(serializers.Serializer):
    """
    PATCH /api/admin/orders/{id}/status/

    Rejects illegal jumps — an order cannot go from pending straight to
    delivered (spec section 5).
    """

    status = serializers.ChoiceField(choices=OrderStatus.choices)

    def validate_status(self, value):
        order = self.context["order"]

        if value == order.status:
            raise serializers.ValidationError(
                f"This order is already {order.get_status_display().lower()}."
            )

        legal = allowed_transitions(order.status)
        if value not in legal:
            current = order.get_status_display().lower()
            if not legal:
                raise serializers.ValidationError(
                    f"This order is {current} and can no longer change status."
                )
            raise serializers.ValidationError(
                f"An order that is {current} cannot move to '{value}'. "
                f"Allowed next: {', '.join(str(s) for s in legal)}."
            )
        return value


class AssignRiderSerializer(serializers.Serializer):
    """
    POST /api/admin/orders/{id}/assign/ — body {"rider_id": N}.

    Creates the Delivery and snapshots rider_earnings.
    """

    rider_id = serializers.IntegerField()

    # An unpaid GCash order must not reach a rider, and neither should an
    # order that is already finished.
    UNASSIGNABLE = {
        OrderStatus.PENDING_PAYMENT,
        OrderStatus.DELIVERED,
        OrderStatus.CANCELLED,
    }

    def validate(self, attrs):
        order = self.context["order"]

        if order.status in self.UNASSIGNABLE:
            raise serializers.ValidationError(
                f"An order that is {order.get_status_display().lower()} "
                "cannot be assigned to a rider."
            )

        if hasattr(order, "delivery"):
            raise serializers.ValidationError(
                f"{order.order_number} is already assigned to "
                f"{order.delivery.rider.user.username}."
            )

        try:
            rider = Rider.objects.select_related("user").get(pk=attrs["rider_id"])
        except Rider.DoesNotExist:
            raise serializers.ValidationError(
                {"rider_id": f"No rider with id {attrs['rider_id']}."}
            )

        if not rider.user.is_active:
            raise serializers.ValidationError(
                {"rider_id": f"{rider.user.username}'s account is deactivated."}
            )

        # No check on how many deliveries the rider already holds: they
        # collect a batch at the store and work through it. Only one may be
        # in_transit at a time, and that is enforced when the rider starts
        # the leg, not here.

        attrs["rider"] = rider
        return attrs

    def rider_earnings(self):
        return Decimal(settings.RIDER_EARNINGS_PER_DELIVERY)
