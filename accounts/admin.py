from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db.models import Count, Q

from orders.constants import ACTIVE_DELIVERY_STATUSES

from .models import Customer, DeviceToken, Rider, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "phone", "role", "is_active", "date_joined")
    list_filter = ("is_admin", "is_rider", "is_customer", "is_active", "is_staff")
    search_fields = ("username", "email", "first_name", "last_name", "phone")
    ordering = ("username",)
    readonly_fields = ("date_joined", "last_login")

    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Yellow Ribbon", {"fields": ("phone", "is_admin", "is_rider", "is_customer")}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        (
            "Yellow Ribbon",
            {"fields": ("email", "phone", "is_admin", "is_rider", "is_customer")},
        ),
    )

    @admin.display(description="role")
    def role(self, obj):
        return obj.role


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "full_name",
        "phone",
        "default_address",
        "order_count",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = (
        "user__username",
        "user__email",
        "user__first_name",
        "user__last_name",
        "user__phone",
        "default_address",
    )
    raw_id_fields = ("user",)
    readonly_fields = ("created_at",)
    ordering = ("user__username",)
    # Without this the changelist runs one query per row for the username.
    list_select_related = ("user",)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_orders=Count("orders"))

    @admin.display(description="name")
    def full_name(self, obj):
        return obj.user.get_full_name() or "—"

    @admin.display(description="phone")
    def phone(self, obj):
        return obj.user.phone or "—"

    @admin.display(description="orders", ordering="_orders")
    def order_count(self, obj):
        return obj._orders


@admin.register(DeviceToken)
class DeviceTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "short_token", "platform", "updated_at")
    list_filter = ("platform", "updated_at")
    search_fields = ("user__username", "user__email", "fcm_token")
    raw_id_fields = ("user",)
    readonly_fields = ("updated_at",)
    list_select_related = ("user",)

    @admin.display(description="token")
    def short_token(self, obj):
        return f"{obj.fcm_token[:24]}…" if len(obj.fcm_token) > 24 else obj.fcm_token


@admin.register(Rider)
class RiderAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "full_name",
        "vehicle_type",
        "plate_number",
        "is_available",
        "active_deliveries",
        "last_location_update",
    )
    list_filter = ("is_available", "vehicle_type", "created_at")
    search_fields = (
        "user__username",
        "user__email",
        "user__first_name",
        "user__last_name",
        "plate_number",
    )
    raw_id_fields = ("user",)
    readonly_fields = (
        "current_latitude",
        "current_longitude",
        "last_location_update",
        "created_at",
    )
    ordering = ("user__username",)
    list_select_related = ("user",)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                _active=Count(
                    "deliveries",
                    filter=Q(deliveries__status__in=ACTIVE_DELIVERY_STATUSES),
                )
            )
        )

    @admin.display(description="name")
    def full_name(self, obj):
        return obj.user.get_full_name() or "—"

    @admin.display(description="on delivery", ordering="_active")
    def active_deliveries(self, obj):
        return obj._active
