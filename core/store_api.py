"""GET/PATCH /api/admin/store-settings/ — the store admin's settings screen."""

from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from .models import StoreSettings
from .permissions import IsAdminUser


class StoreSettingsSerializer(serializers.ModelSerializer):
    """
    Every setting the admin can change, with the model's own validation:
    fee >= 0, radius 0.1 to 100 km, latitude and longitude in range, gateway
    percentages 0 to 100.
    """

    delivery_fee_mode = serializers.SerializerMethodField()

    class Meta:
        model = StoreSettings
        fields = [
            "delivery_fee",
            "delivery_fee_per_km",
            "delivery_fee_mode",
            "service_area_city",
            "service_area_center_lat",
            "service_area_center_lng",
            "service_area_radius_km",
            "paymongo_fee_percent",
            "paymongo_fee_fixed",
            "maya_fee_percent",
            "maya_fee_fixed",
            "updated_at",
        ]
        read_only_fields = ["delivery_fee_mode", "updated_at"]

    def get_delivery_fee_mode(self, obj):
        return "per_km" if obj.delivery_fee_per_km else "flat"

    def validate_service_area_city(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("The service area needs a name.")
        return value


@api_view(["GET", "PATCH"])
@permission_classes([IsAdminUser])
def store_settings(request):
    store = StoreSettings.load()

    if request.method == "PATCH":
        serializer = StoreSettingsSerializer(store, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        store.refresh_from_db()

    return Response(StoreSettingsSerializer(store).data)
