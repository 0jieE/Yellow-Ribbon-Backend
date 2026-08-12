from django.contrib import admin

from .models import Order, OrderItem, OrderStatusLog


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    # Prices are snapshots taken at checkout — editing them here would
    # rewrite history (spec section 4).
    readonly_fields = ("menu", "quantity", "unit_price", "line_subtotal")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class OrderStatusLogInline(admin.TabularInline):
    model = OrderStatusLog
    extra = 0
    readonly_fields = ("status", "changed_by", "changed_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_number",
        "customer",
        "status",
        "payment_type",
        "payment_status",
        "total",
        "placed_at",
    )
    list_filter = ("status", "payment_type", "payment_status", "placed_at")
    search_fields = (
        "order_number",
        "customer__user__username",
        "customer__user__email",
        "delivery_address",
    )
    date_hierarchy = "placed_at"
    readonly_fields = (
        "order_number",
        "subtotal",
        "delivery_fee",
        "total",
        "placed_at",
        "eta_minutes",
        "eta_at",
    )
    raw_id_fields = ("customer",)
    inlines = [OrderItemInline, OrderStatusLogInline]
    ordering = ("-placed_at",)
    list_select_related = ("customer__user",)

    fieldsets = (
        (None, {"fields": ("order_number", "customer", "status")}),
        ("Payment", {"fields": ("payment_type", "payment_status")}),
        (
            "Money",
            {
                "fields": ("subtotal", "delivery_fee", "total"),
                "description": (
                    "Computed server-side from the line items. Read-only so a "
                    "receipt cannot be edited after the fact."
                ),
            },
        ),
        (
            "Delivery",
            {
                "fields": (
                    "delivery_address",
                    "delivery_latitude",
                    "delivery_longitude",
                    "delivery_notes",
                    "eta_minutes",
                    "eta_at",
                )
            },
        ),
        (
            "Timeline",
            {
                "fields": (
                    "placed_at",
                    "confirmed_at",
                    "delivered_at",
                    "cancelled_at",
                    "cancel_reason",
                )
            },
        ),
    )


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "menu", "quantity", "unit_price", "line_subtotal")
    list_filter = ("menu", "order__status")
    search_fields = ("order__order_number", "menu__name")
    raw_id_fields = ("order", "menu")
    list_select_related = ("order", "menu")
    # Snapshots taken at checkout (spec section 4) — never editable.
    readonly_fields = ("unit_price", "line_subtotal")


@admin.register(OrderStatusLog)
class OrderStatusLogAdmin(admin.ModelAdmin):
    list_display = ("order", "status", "changed_by", "changed_at")
    list_filter = ("status", "changed_at")
    search_fields = ("order__order_number", "changed_by__username")
    raw_id_fields = ("order", "changed_by")
    date_hierarchy = "changed_at"
    list_select_related = ("order", "changed_by")
    readonly_fields = ("order", "status", "changed_by", "changed_at")

    def has_add_permission(self, request):
        # The audit trail is written by the system, not typed in by hand.
        return False
