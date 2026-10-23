from django.contrib import admin
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import RangeDateFilter

from core.admin_tools import csv_export_action

from .models import Invoice


@admin.register(Invoice)
class InvoiceAdmin(ModelAdmin):
    list_display = (
        "invoice_number",
        "order",
        "amount_paid",
        "payment_reference",
        "paid_at",
        "created_at",
    )
    list_filter = (
        ("paid_at", RangeDateFilter),
        ("created_at", RangeDateFilter),
        "order__payment_type",
    )
    list_filter_submit = True
    actions = [
        csv_export_action(
            "invoices",
            [
                ("Invoice", lambda i: i.invoice_number),
                ("Order", lambda i: i.order.order_number),
                ("Amount paid", lambda i: i.amount_paid),
                ("Reference", lambda i: i.paymongo_payment_id or "COD"),
                ("Paid at", lambda i: i.paid_at.strftime("%Y-%m-%d %H:%M") if i.paid_at else ""),
            ],
        )
    ]
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
