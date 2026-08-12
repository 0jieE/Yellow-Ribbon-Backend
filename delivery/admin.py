from django.contrib import admin
from django.utils.html import format_html

from .models import Delivery


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "order",
        "rider",
        "status",
        "rider_earnings",
        "has_proof",
        "assigned_at",
        "delivered_at",
    )
    list_filter = ("status", "assigned_at", "rider__vehicle_type")
    search_fields = (
        "order__order_number",
        "rider__user__username",
        "rider__user__first_name",
        "rider__user__last_name",
        "rider__plate_number",
    )
    raw_id_fields = ("order", "rider")
    date_hierarchy = "assigned_at"
    ordering = ("-assigned_at",)
    list_select_related = ("order", "rider__user")
    # Snapshot at assignment (spec section 4) — not editable after the fact.
    readonly_fields = ("rider_earnings", "assigned_at", "proof_preview")

    fieldsets = (
        (None, {"fields": ("order", "rider", "status", "rider_earnings")}),
        ("Proof of delivery", {"fields": ("proof_of_delivery", "proof_preview")}),
        ("Timeline", {"fields": ("assigned_at", "picked_up_at", "delivered_at")}),
    )

    @admin.display(description="proof", boolean=True)
    def has_proof(self, obj):
        return bool(obj.proof_of_delivery)

    @admin.display(description="preview")
    def proof_preview(self, obj):
        if not obj.proof_of_delivery:
            return "No photo uploaded."
        return format_html(
            '<img src="{}" style="max-height:240px;" />', obj.proof_of_delivery.url
        )
