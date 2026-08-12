"""Rider-facing delivery serializers (spec section 7)."""

from rest_framework import serializers

from orders.constants import DeliveryStatus, allowed_delivery_transitions
from orders.serializers import OrderItemSerializer

from .models import Delivery


class DeliveryOrderSerializer(serializers.Serializer):
    """
    The order as the rider needs to see it: where to go, what to hand over,
    and whether to collect cash.
    """

    id = serializers.IntegerField(read_only=True)
    order_number = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    payment_type = serializers.CharField(read_only=True)
    payment_status = serializers.CharField(read_only=True)
    total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    delivery_address = serializers.CharField(read_only=True)
    delivery_latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, read_only=True
    )
    delivery_longitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, read_only=True
    )
    delivery_notes = serializers.CharField(read_only=True)
    eta_minutes = serializers.IntegerField(read_only=True)
    eta_at = serializers.DateTimeField(read_only=True)
    route_polyline = serializers.CharField(read_only=True)
    placed_at = serializers.DateTimeField(read_only=True)
    items = OrderItemSerializer(many=True, read_only=True)

    customer_name = serializers.SerializerMethodField()
    customer_phone = serializers.SerializerMethodField()
    amount_to_collect = serializers.SerializerMethodField()

    def get_customer_name(self, obj):
        user = obj.customer.user
        return user.get_full_name() or user.username

    def get_customer_phone(self, obj):
        return obj.customer.user.phone

    def get_amount_to_collect(self, obj):
        """COD riders collect the full total. Paid GCash orders collect nothing."""
        if obj.payment_type == "cod" and obj.payment_status != "paid":
            return str(obj.total)
        return "0.00"


class DeliverySerializer(serializers.ModelSerializer):
    order = DeliveryOrderSerializer(read_only=True)
    next_statuses = serializers.SerializerMethodField()

    class Meta:
        model = Delivery
        fields = [
            "id",
            "status",
            "rider_earnings",
            "proof_of_delivery",
            "assigned_at",
            "picked_up_at",
            "delivered_at",
            "next_statuses",
            "order",
        ]
        read_only_fields = fields

    def get_next_statuses(self, obj):
        return [str(s) for s in allowed_delivery_transitions(obj.status)]


class DeliveryStatusUpdateSerializer(serializers.Serializer):
    """PATCH /api/rider/deliveries/{id}/status/."""

    status = serializers.ChoiceField(choices=DeliveryStatus.choices)
    failure_reason = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )

    def validate_status(self, value):
        delivery = self.context["delivery"]

        if value == delivery.status:
            raise serializers.ValidationError(
                f"This delivery is already {delivery.get_status_display().lower()}."
            )

        legal = allowed_delivery_transitions(delivery.status)
        current = delivery.get_status_display().lower()
        if not legal:
            raise serializers.ValidationError(
                f"This delivery is {current} and can no longer change status."
            )
        if value not in legal:
            raise serializers.ValidationError(
                f"A delivery that is {current} cannot move to '{value}'. "
                f"Allowed next: {', '.join(str(s) for s in legal)}."
            )

        if value == DeliveryStatus.IN_TRANSIT:
            # A rider carries a batch but delivers them one at a time, so the
            # ETA stays a single origin-to-destination leg.
            in_flight = (
                Delivery.objects.filter(
                    rider_id=delivery.rider_id, status=DeliveryStatus.IN_TRANSIT
                )
                .exclude(pk=delivery.pk)
                .select_related("order")
                .first()
            )
            if in_flight is not None:
                raise serializers.ValidationError(
                    f"You are already delivering "
                    f"{in_flight.order.order_number}. Finish or fail that one "
                    "before starting another."
                )

        return value


class ProofOfDeliverySerializer(serializers.ModelSerializer):
    """POST /api/rider/deliveries/{id}/proof/ — multipart photo upload."""

    class Meta:
        model = Delivery
        fields = ["proof_of_delivery"]
        extra_kwargs = {"proof_of_delivery": {"required": True, "allow_null": False}}


class AvailabilitySerializer(serializers.Serializer):
    """PATCH /api/rider/availability/."""

    is_available = serializers.BooleanField()
