"""Admin-facing rider management (spec section 7)."""

from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.db.models import Prefetch
from rest_framework import mixins, serializers, viewsets
from rest_framework import status as http
from rest_framework.decorators import action
from rest_framework.response import Response

from core.permissions import IsAdminUser
from delivery.models import Delivery
from orders.constants import ACTIVE_DELIVERY_STATUSES, DeliveryStatus

from .models import Rider, User
from .serializers import save_user_or_field_error


class RiderSerializer(serializers.ModelSerializer):
    """Read shape for the admin rider list."""

    user_id = serializers.IntegerField(source="user.id", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)
    name = serializers.SerializerMethodField()
    phone = serializers.CharField(source="user.phone", read_only=True)
    is_active = serializers.BooleanField(source="user.is_active", read_only=True)
    deliveries_held = serializers.SerializerMethodField()
    current_delivery = serializers.SerializerMethodField()

    class Meta:
        model = Rider
        fields = [
            "id",
            "user_id",
            "username",
            "email",
            "first_name",
            "last_name",
            "name",
            "phone",
            "is_active",
            "vehicle_type",
            "plate_number",
            "is_available",
            "current_latitude",
            "current_longitude",
            "last_location_update",
            "deliveries_held",
            "current_delivery",
            "created_at",
        ]
        read_only_fields = fields

    def get_name(self, obj):
        return obj.user.get_full_name() or obj.user.username

    def _active(self, obj):
        """Prefetched by the viewset; falls back to a query if used bare."""
        cached = getattr(obj, "active_deliveries", None)
        if cached is not None:
            return cached
        return list(
            obj.deliveries.filter(
                status__in=ACTIVE_DELIVERY_STATUSES
            ).select_related("order")
        )

    def get_deliveries_held(self, obj):
        """How many orders this rider is carrying, at any active stage."""
        return len(self._active(obj))

    def get_current_delivery(self, obj):
        """
        The leg being driven right now, or null when the rider is still
        loading up. Only one may be in_transit at a time, so this is
        unambiguous — together with deliveries_held it reads as
        "3 orders, currently on YR-0028".
        """
        delivery = next(
            (d for d in self._active(obj) if d.status == DeliveryStatus.IN_TRANSIT),
            None,
        )
        if delivery is None:
            return None
        return {
            "delivery_id": delivery.pk,
            "order_number": delivery.order.order_number,
            "status": delivery.status,
        }


class RiderCreateSerializer(serializers.Serializer):
    """
    POST /api/admin/riders/

    Riders are created by the admin, never by self-registration (spec
    section 6). The role flags are set here, not taken from the body.
    """

    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    first_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    vehicle_type = serializers.CharField(max_length=50, required=False, allow_blank=True)
    plate_number = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("That username is taken.")
        return value

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("That email is already registered.")
        return value

    def validate_password(self, value):
        validate_password(value)
        return value

    @transaction.atomic
    def create(self, validated_data):
        profile_fields = {
            "vehicle_type": validated_data.pop("vehicle_type", ""),
            "plate_number": validated_data.pop("plate_number", ""),
        }
        password = validated_data.pop("password")

        user = User(
            **validated_data,
            is_rider=True,
            is_customer=False,
            is_admin=False,
        )
        user.set_password(password)
        save_user_or_field_error(user)

        # is_available starts False — the rider goes on duty from their own app.
        return Rider.objects.create(user=user, **profile_fields)

    def to_representation(self, instance):
        return RiderSerializer(instance, context=self.context).data


class RiderUpdateSerializer(serializers.ModelSerializer):
    """
    PATCH /api/admin/riders/{id}/

    Covers both halves of a rider account — the User row and the Rider
    profile — in one call. `username` is deliberately not editable: it is
    what the rider logs in with and what order history is read against.
    `is_available` is the rider's own on-duty toggle, not the admin's.
    """

    first_name = serializers.CharField(
        source="user.first_name", max_length=100, required=False, allow_blank=True
    )
    last_name = serializers.CharField(
        source="user.last_name", max_length=100, required=False, allow_blank=True
    )
    email = serializers.EmailField(source="user.email", required=False)
    phone = serializers.CharField(
        source="user.phone", max_length=20, required=False, allow_blank=True
    )

    class Meta:
        model = Rider
        fields = [
            "first_name",
            "last_name",
            "email",
            "phone",
            "vehicle_type",
            "plate_number",
        ]

    def validate_email(self, value):
        clash = User.objects.filter(email__iexact=value).exclude(
            pk=self.instance.user_id
        )
        if clash.exists():
            raise serializers.ValidationError("That email is already registered.")
        return value

    @transaction.atomic
    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", {})
        if user_data:
            user = instance.user
            for field, value in user_data.items():
                setattr(user, field, value)
            user.save(update_fields=list(user_data))

        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        return instance

    def to_representation(self, instance):
        return RiderSerializer(instance, context=self.context).data


class SetRiderPasswordSerializer(serializers.Serializer):
    """
    POST /api/admin/riders/{id}/set-password/

    Riders have no self-service reset, so this is the only way back in for a
    locked-out account. Runs Django's validators against the target user, so
    a password too similar to their own name or email is rejected.
    """

    password = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )

    def validate_password(self, value):
        # Raised as a field error, so the admin app can put it under the input.
        validate_password(value, user=self.context.get("user"))
        return value


class RiderActiveSerializer(serializers.Serializer):
    """
    PATCH /api/admin/riders/{id}/deactivate/

    Explicit rather than a flip, so a retried request cannot toggle the
    account back on. Omit the body to deactivate.
    """

    is_active = serializers.BooleanField(required=False, default=False)


class AdminRiderViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """
    GET   /api/admin/riders/                    list riders
    POST  /api/admin/riders/                    create a rider account
    GET   /api/admin/riders/{id}/               detail
    PATCH /api/admin/riders/{id}/               update profile and user fields
    POST  /api/admin/riders/{id}/set-password/  admin sets a new password
    PATCH /api/admin/riders/{id}/deactivate/    switch the account off or on
    GET   /api/admin/riders/available/          on duty and not mid-delivery
    """

    permission_classes = [IsAdminUser]
    # No PUT: a partial update is the only shape the admin app needs, and a
    # full replace would silently blank fields it did not send.
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_serializer_class(self):
        if self.action == "create":
            return RiderCreateSerializer
        if self.action in ("update", "partial_update"):
            return RiderUpdateSerializer
        if self.action == "set_password":
            return SetRiderPasswordSerializer
        if self.action == "deactivate":
            return RiderActiveSerializer
        return RiderSerializer

    def get_queryset(self):
        # Only the active deliveries are loaded — a rider's completed history
        # can be long and none of it is shown on this screen.
        return (
            Rider.objects.select_related("user")
            .prefetch_related(
                Prefetch(
                    "deliveries",
                    queryset=Delivery.objects.filter(
                        status__in=ACTIVE_DELIVERY_STATUSES
                    ).select_related("order"),
                    to_attr="active_deliveries",
                )
            )
            .order_by("user__username")
        )

    @action(detail=False, methods=["get"])
    def available(self, request):
        """
        On duty, active account, and not already driving a leg.

        Deliberately narrower than "no active delivery": riders batch several
        orders at the store before setting off, so someone holding two at
        `assigned` or `picked_up` can still take a third. Once they are
        `in_transit` they have left and cannot collect anything more.
        """
        riders = (
            self.get_queryset()
            .filter(is_available=True, user__is_active=True)
            .exclude(deliveries__status=DeliveryStatus.IN_TRANSIT)
        )
        return Response(RiderSerializer(riders, many=True).data)

    @action(
        detail=True,
        methods=["post"],
        url_path="set-password",
        url_name="set-password",
    )
    def set_password(self, request, pk=None):
        """The recovery path for a locked-out rider."""
        rider = self.get_object()
        serializer = SetRiderPasswordSerializer(
            data=request.data, context={"user": rider.user}
        )
        serializer.is_valid(raise_exception=True)

        user = rider.user
        user.set_password(serializer.validated_data["password"])
        user.save(update_fields=["password"])

        return Response({"detail": f"Password updated for {user.username}."})

    @action(detail=True, methods=["patch"])
    def deactivate(self, request, pk=None):
        """
        Switch the account off, or back on with {"is_active": true}.

        Never a delete: a rider is referenced by every delivery they ever
        made, and removing the row would take that history with it.
        """
        rider = self.get_object()
        serializer = RiderActiveSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        is_active = serializer.validated_data["is_active"]

        user = rider.user

        if not is_active:
            held = (
                Delivery.objects.filter(
                    rider=rider, status__in=ACTIVE_DELIVERY_STATUSES
                )
                .select_related("order")
                .order_by("assigned_at")
            )
            numbers = [d.order.order_number for d in held]
            if numbers:
                name = user.get_full_name() or user.username
                return Response(
                    {
                        "error": (
                            f"{name} is still carrying {', '.join(numbers)}. "
                            "Reassign or complete them before deactivating."
                        )
                    },
                    status=http.HTTP_400_BAD_REQUEST,
                )

        with transaction.atomic():
            if user.is_active != is_active:
                user.is_active = is_active
                user.save(update_fields=["is_active"])

            # A switched-off account must not sit in the available list.
            if not is_active and rider.is_available:
                rider.is_available = False
                rider.save(update_fields=["is_available"])

        rider = self.get_queryset().get(pk=rider.pk)
        return Response(RiderSerializer(rider, context=self.get_serializer_context()).data)
