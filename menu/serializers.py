from rest_framework import serializers

from .models import Menu


class MenuSerializer(serializers.ModelSerializer):
    """
    `photo` comes back as an absolute URL because the viewset puts the
    request in the serializer context — the Flutter apps can load it directly.
    """

    class Meta:
        model = Menu
        fields = [
            "id",
            "name",
            "description",
            "price",
            "photo",
            "is_available",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Name cannot be blank.")
        return value

    def validate_price(self, value):
        if value < 0:
            raise serializers.ValidationError("Price cannot be negative.")
        return value
