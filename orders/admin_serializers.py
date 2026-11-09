"""Serializers for the admin-facing order endpoints (spec section 7)."""

from decimal import Decimal

from django.conf import settings
from rest_framework import serializers

from accounts.models import Rider

from .constants import (
    RIDER_ASSIGNABLE_STATUSES,
    DeliveryStatus,
    OrderStatus,
    admin_allowed_transitions,
)
from .models import Order
from .serializers import (
    OrderItemSerializer,
    OrderStatusLogSerializer,
    payment_summary,
)


class AdminOrderSerializer(serializers.ModelSerializer):
    """Everything the admin app shows in the queue and on the detail screen."""

    items = OrderItemSerializer(many=True, read_only=True)
    status_logs = OrderStatusLogSerializer(many=True, read_only=True)

    customer_name = serializers.SerializerMethodField()
    customer_phone = serializers.CharField(
        source="customer.user.phone", read_only=True, default=""
    )
    next_statuses = serializers.SerializerMethodField()
    can_assign_rider = serializers.SerializerMethodField()
    can_change_rider = serializers.SerializerMethodField()
    rider = serializers.SerializerMethodField()
    payment = serializers.SerializerMethodField()

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
            "can_assign_rider",
            "can_change_rider",
            "rider",
            "payment",
            "items",
            "status_logs",
        ]
        read_only_fields = fields

    def get_payment(self, obj):
        return payment_summary(obj, for_admin=True)

    def get_customer_name(self, obj):
        user = obj.customer.user
        return user.get_full_name() or user.username

    def get_next_statuses(self, obj):
        """
        Lets the admin app render only the buttons that will be accepted.
        Never out_for_delivery or delivered: the rider's pickup and drop-off
        move those.
        """
        return [str(s) for s in admin_allowed_transitions(obj.status)]

    def get_can_assign_rider(self, obj):
        return can_assign_rider(obj)

    def get_can_change_rider(self, obj):
        return can_change_rider(obj)

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
            "failure_reason": delivery.failure_reason or None,
            "failed_at": serializers.DateTimeField().to_representation(delivery.failed_at)
            if delivery.failed_at
            else None,
        }


def _delivery(order):
    return getattr(order, "delivery", None)


def can_assign_rider(order):
    """Accepted, still at the store, and nobody assigned yet."""
    return order.status in RIDER_ASSIGNABLE_STATUSES and _delivery(order) is None


def can_change_rider(order):
    """Someone is assigned but has not picked the order up."""
    delivery = _delivery(order)
    return (
        order.status in RIDER_ASSIGNABLE_STATUSES
        and delivery is not None
        and delivery.status == DeliveryStatus.ASSIGNED
    )


class OrderStatusUpdateSerializer(serializers.Serializer):
    """
    PATCH /api/admin/orders/{id}/status/

    Rejects illegal jumps — an order cannot go from pending straight to
    delivered (spec section 5) — and the two steps that belong to the rider.
    """

    RIDER_STEPS = {
        OrderStatus.OUT_FOR_DELIVERY: (
            "The rider app sends an order out for delivery when the rider picks it up."
        ),
        OrderStatus.DELIVERED: "The rider app marks an order delivered at the door.",
    }

    status = serializers.ChoiceField(choices=OrderStatus.choices)

    def validate_status(self, value):
        order = self.context["order"]

        if value == order.status:
            raise serializers.ValidationError(
                f"This order is already {order.get_status_display().lower()}."
            )

        legal = admin_allowed_transitions(order.status)
        if value not in legal:
            current = order.get_status_display().lower()
            if value in self.RIDER_STEPS and order.status not in (
                OrderStatus.DELIVERED,
                OrderStatus.CANCELLED,
            ):
                raise serializers.ValidationError(self.RIDER_STEPS[value])
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

    Assigns a rider once the store has accepted the order (confirmed,
    preparing or ready), or changes the rider while the delivery is still
    `assigned`. Once the rider has picked the order up it stays with them.
    After validation, `reassigning` is the delivery being handed over, or None
    for a first assignment.
    """

    rider_id = serializers.IntegerField()

    NOT_YET = {
        OrderStatus.PENDING_PAYMENT: "This order is still waiting for payment. Assign a rider once it is paid and accepted.",
        OrderStatus.PENDING: "Accept the order before assigning a rider.",
    }

    def validate(self, attrs):
        order = self.context["order"]
        self.reassigning = None

        if order.status in self.NOT_YET:
            raise serializers.ValidationError(self.NOT_YET[order.status])

        delivery = _delivery(order)
        if delivery is not None and delivery.status != DeliveryStatus.ASSIGNED:
            raise serializers.ValidationError(
                f"{delivery.rider.user.username} has already picked up {order.order_number}, "
                "so the rider can no longer be changed."
                if delivery.status != DeliveryStatus.FAILED
                else f"The delivery of {order.order_number} has failed. Cancel the order instead."
            )

        if order.status not in RIDER_ASSIGNABLE_STATUSES:
            raise serializers.ValidationError(
                f"An order that is {order.get_status_display().lower()} "
                "cannot be assigned to a rider."
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

        if delivery is not None:
            if delivery.rider_id == rider.pk:
                raise serializers.ValidationError(
                    f"{order.order_number} is already assigned to {rider.user.username}."
                )
            self.reassigning = delivery

        # No check on how many deliveries the rider already holds: they
        # collect a batch at the store and work through it. Only one may be
        # in_transit at a time, and that is enforced when the rider starts
        # the leg, not here.

        attrs["rider"] = rider
        return attrs

    def rider_earnings(self):
        return Decimal(settings.RIDER_EARNINGS_PER_DELIVERY)
