"""Order serializers, including the placement flow from spec section 8."""

from decimal import Decimal

from django.conf import settings
from django.db import transaction
from rest_framework import serializers

from core.geofence import distance_from_service_center_km, is_within_service_area
from core.models import StoreSettings
from core.notifications import notify_new_order
from menu.models import Menu

from .constants import (
    ACTIVE_DELIVERY_STATUSES,
    OrderStatus,
    PaymentStatus,
    PaymentType,
)
from .models import Order, OrderItem, OrderStatusLog, next_order_number


class OrderItemSerializer(serializers.ModelSerializer):
    """Read shape. `menu_name` is denormalised so a receipt still reads
    correctly even if the item is later renamed."""

    menu_name = serializers.CharField(source="menu.name", read_only=True)

    class Meta:
        model = OrderItem
        fields = [
            "id",
            "menu",
            "menu_name",
            "quantity",
            "unit_price",
            "line_subtotal",
        ]
        read_only_fields = fields


class OrderStatusLogSerializer(serializers.ModelSerializer):
    changed_by_username = serializers.CharField(
        source="changed_by.username", read_only=True, default=None
    )

    class Meta:
        model = OrderStatusLog
        fields = ["id", "status", "changed_by", "changed_by_username", "changed_at"]
        read_only_fields = fields


def _iso(value):
    return serializers.DateTimeField().to_representation(value) if value is not None else None


def _test_mode(provider):
    """Lets the apps label sandbox payments honestly in a demo."""
    if provider == "maya":
        return "sandbox" in settings.MAYA_BASE_URL
    if provider == "cod":
        return False
    # PayMongo, and invoices from before `provider` existed (always PayMongo or COD).
    return settings.PAYMONGO_SECRET_KEY.startswith("sk_test_")


def refund_summary(invoice, *, for_admin=False):
    """The refund on an invoice, or None when it never had one."""
    if not invoice.refund_status:
        return None
    summary = {
        "status": invoice.refund_status,
        "amount": str(invoice.refund_amount) if invoice.refund_amount is not None else None,
        "refunded_at": _iso(invoice.refunded_at),
        "reference": invoice.refund_reference or None,
    }
    if for_admin:
        # Why a refund failed is for the shop to act on, not for the customer.
        summary["error"] = invoice.refund_error or None
    return summary


def payment_summary(order, *, for_admin=False):
    """
    The invoice behind an order, so the apps can show the gateway reference
    that proves a payment was settled, who paid, and any refund.

    None until an invoice exists: online orders get one at checkout, COD at
    the door. `paymongo_reference` is the gateway's reference whichever the
    provider (the name is historical): for GCash the source id (src_...) while
    checkout is open and the payment id (pay_...) once settled; for Maya the
    checkout id, then the payment id.

    The gateway fee and what the store nets are the shop's own figures, so
    they are added only for `for_admin`.
    """
    invoice = getattr(order, "invoice", None)
    if invoice is None:
        return None

    summary = {
        "invoice_number": invoice.invoice_number,
        "paymongo_reference": invoice.paymongo_payment_id,
        "amount_paid": str(invoice.amount_paid),
        "paid_at": _iso(invoice.paid_at),
        "test_mode": _test_mode(invoice.provider),
        "provider": invoice.provider or None,
        "payer": invoice.payer or None,
        "refund": refund_summary(invoice, for_admin=for_admin),
    }
    if for_admin:
        summary["fee"] = str(invoice.fee)
        summary["net_amount"] = str(invoice.net_amount)
    return summary


class OrderSerializer(serializers.ModelSerializer):
    """List / detail shape returned to the customer."""

    items = OrderItemSerializer(many=True, read_only=True)
    status_logs = OrderStatusLogSerializer(many=True, read_only=True)
    can_cancel = serializers.BooleanField(
        source="is_cancellable_by_customer", read_only=True
    )
    rider_location = serializers.SerializerMethodField()
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
            "route_polyline",
            "placed_at",
            "confirmed_at",
            "delivered_at",
            "cancelled_at",
            "cancel_reason",
            "can_cancel",
            "items",
            "status_logs",
            "rider_location",
            "payment",
        ]
        read_only_fields = fields

    def get_payment(self, obj):
        return payment_summary(obj)

    def get_rider_location(self, obj):
        """
        Where the rider is right now, for the customer's tracking map.

        Only exposed while a delivery is actually in progress — a rider's
        coordinates are not the customer's business before assignment or
        after the order is done.
        """
        delivery = getattr(obj, "delivery", None)
        if delivery is None or delivery.status not in ACTIVE_DELIVERY_STATUSES:
            return None

        rider = delivery.rider
        if rider.current_latitude is None or rider.current_longitude is None:
            return None

        return {
            "latitude": str(rider.current_latitude),
            "longitude": str(rider.current_longitude),
            "updated_at": rider.last_location_update,
            "delivery_status": delivery.status,
            "rider_name": rider.user.get_full_name() or rider.user.username,
            "vehicle_type": rider.vehicle_type,
        }


class OrderItemInputSerializer(serializers.Serializer):
    """One requested line: what to buy and how many. Never a price."""

    menu_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class OrderCreateSerializer(serializers.Serializer):
    """
    POST /api/orders/ — the placement flow, in the order spec section 8
    requires. The geofence runs before menu validation and before anything
    is written.
    """

    items = OrderItemInputSerializer(many=True, allow_empty=False)
    delivery_address = serializers.CharField(max_length=255)
    delivery_latitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    delivery_longitude = serializers.DecimalField(max_digits=9, decimal_places=6)
    delivery_notes = serializers.CharField(
        required=False, allow_blank=True, default=""
    )
    payment_type = serializers.ChoiceField(choices=PaymentType.choices)

    # -- step 1 --------------------------------------------------------

    def _check_service_area(self, attrs):
        lat = attrs["delivery_latitude"]
        lng = attrs["delivery_longitude"]
        store = StoreSettings.load()

        if not is_within_service_area(lat, lng, store):
            distance = distance_from_service_center_km(lat, lng, store)
            raise serializers.ValidationError(
                "We only deliver within {:g} km of {}. That address "
                "is about {:.1f} km away.".format(
                    store.radius_km, store.service_area_city, distance
                )
            )

    # -- step 2 --------------------------------------------------------

    def _resolve_items(self, items):
        """
        Every menu_id must exist and be available. Returns resolved lines
        with prices read from the database, never from the request.
        """
        # order_items is unique on (order, menu), so repeat lines are merged
        # into a single line rather than rejected.
        wanted = {}
        for item in items:
            wanted[item["menu_id"]] = wanted.get(item["menu_id"], 0) + item["quantity"]

        found = {m.pk: m for m in Menu.objects.filter(pk__in=wanted)}

        missing = sorted(set(wanted) - set(found))
        if missing:
            raise serializers.ValidationError(
                {"items": f"No menu item with id {missing[0]}."}
            )

        unavailable = sorted(pk for pk, m in found.items() if not m.is_available)
        if unavailable:
            name = found[unavailable[0]].name
            raise serializers.ValidationError(
                {"items": f"'{name}' is currently unavailable."}
            )

        return [
            {
                "menu": found[pk],
                "quantity": quantity,
                "unit_price": found[pk].price,
                "line_subtotal": found[pk].price * quantity,
            }
            for pk, quantity in wanted.items()
        ]

    def validate(self, attrs):
        # Order matters. Geofence first, before the menu is touched.
        self._check_service_area(attrs)
        attrs["resolved_lines"] = self._resolve_items(attrs["items"])
        return attrs

    # -- steps 3 to 6 --------------------------------------------------

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("resolved_lines")
        user = self.context["request"].user

        # step 3: totals computed server-side.
        subtotal = sum((line["line_subtotal"] for line in lines), Decimal("0.00"))
        store = StoreSettings.load()
        delivery_fee = store.delivery_fee_for(
            distance_from_service_center_km(
                validated_data["delivery_latitude"],
                validated_data["delivery_longitude"],
                store,
            )
        )
        total = subtotal + delivery_fee

        # step 5: an unpaid GCash order must not reach the kitchen.
        payment_type = validated_data["payment_type"]
        status = (
            OrderStatus.PENDING
            if payment_type == PaymentType.COD
            else OrderStatus.PENDING_PAYMENT
        )

        # step 4: order + items in one transaction.
        order = Order.objects.create(
            order_number=next_order_number(),
            customer=user.customer,
            status=status,
            payment_type=payment_type,
            payment_status=PaymentStatus.UNPAID,
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            total=total,
            delivery_address=validated_data["delivery_address"],
            delivery_latitude=validated_data["delivery_latitude"],
            delivery_longitude=validated_data["delivery_longitude"],
            delivery_notes=validated_data.get("delivery_notes", ""),
        )

        OrderItem.objects.bulk_create(
            [
                OrderItem(
                    order=order,
                    menu=line["menu"],
                    quantity=line["quantity"],
                    unit_price=line["unit_price"],
                    line_subtotal=line["line_subtotal"],
                )
                for line in lines
            ]
        )

        # step 6: audit trail.
        OrderStatusLog.objects.create(order=order, status=status, changed_by=user)

        # A COD order is live the moment it is placed, so the admin hears
        # about it now. A GCash order is still unpaid and must not reach the
        # kitchen — its admin push fires from the PayMongo webhook instead.
        if payment_type == PaymentType.COD:
            notify_new_order(order)

        return order

    def to_representation(self, instance):
        return OrderSerializer(instance, context=self.context).data


class OrderCancelSerializer(serializers.Serializer):
    """POST /api/orders/{id}/cancel/ — allowed only while status is pending."""

    cancel_reason = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
