from django.contrib import admin

from .models import Invoice


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "invoice_number",
        "order",
        "amount_paid",
        "payment_reference",
        "paid_at",
        "created_at",
    )
    list_filter = ("paid_at", "created_at", "order__payment_type")
    search_fields = (
        "invoice_number",
        "order__order_number",
        "paymongo_payment_id",
        "order__customer__user__username",
    )
    raw_id_fields = ("order",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("order",)
    # Money and references are written by the webhook, not by hand.
    readonly_fields = (
        "invoice_number",
        "amount_paid",
        "paymongo_payment_id",
        "paymongo_checkout_url",
        "paid_at",
        "created_at",
    )

    @admin.display(description="reference")
    def payment_reference(self, obj):
        return obj.paymongo_payment_id or "COD"
