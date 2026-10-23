from django.contrib import admin
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import RangeDateFilter
from unfold.decorators import action, display

from core.admin_tools import csv_export_action

from .admin_actions import retry_refunds_for
from .models import Invoice

REFUND_LABELS = {"Pending": "warning", "Refunded": "success", "Failed": "danger"}
PROVIDER_LABELS = {
    "PayMongo (GCash)": "info",
    "Maya": "success",
    "Cash on delivery": None,
}


@admin.register(Invoice)
class InvoiceAdmin(ModelAdmin):
    list_display = (
        "invoice_number",
        "order",
        "provider_badge",
        "amount_paid",
        "fee",
        "net_amount",
        "payer",
        "refund_badge",
        "paid_at",
    )
    list_filter = (
        "provider",
        "refund_status",
        ("paid_at", RangeDateFilter),
        ("created_at", RangeDateFilter),
    )
    list_filter_submit = True
    actions = [
        "retry_refund",
        csv_export_action(
            "invoices",
            [
                ("Invoice", lambda i: i.invoice_number),
                ("Order", lambda i: i.order.order_number),
                ("Provider", lambda i: i.provider),
                ("Amount paid", lambda i: i.amount_paid),
                ("Gateway fee", lambda i: i.fee),
                ("Net", lambda i: i.net_amount),
                ("Payer", lambda i: i.payer),
                ("Reference", lambda i: i.paymongo_payment_id or "COD"),
                ("Paid at", lambda i: i.paid_at.strftime("%Y-%m-%d %H:%M") if i.paid_at else ""),
                ("Refund status", lambda i: i.refund_status),
                ("Refund amount", lambda i: i.refund_amount if i.refund_amount is not None else ""),
                ("Refunded at", lambda i: i.refunded_at.strftime("%Y-%m-%d %H:%M") if i.refunded_at else ""),
            ],
        ),
    ]
    search_fields = (
        "invoice_number",
        "order__order_number",
        "paymongo_payment_id",
        "provider_rrn",
        "payer",
        "refund_reference",
        "order__customer__user__username",
    )
    raw_id_fields = ("order",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("order",)
    # Money, references and refunds are written by the gateways, not by hand.
    readonly_fields = (
        "invoice_number",
        "provider",
        "provider_rrn",
        "amount_paid",
        "fee",
        "net_amount",
        "payer",
        "paymongo_payment_id",
        "paymongo_checkout_url",
        "paid_at",
        "created_at",
        "refund_status",
        "refund_amount",
        "refunded_at",
        "refund_reference",
        "refund_error",
    )

    @display(description="provider", ordering="provider", label=PROVIDER_LABELS)
    def provider_badge(self, obj):
        return obj.get_provider_display() or "-"

    @display(description="refund", ordering="refund_status", label=REFUND_LABELS)
    def refund_badge(self, obj):
        return obj.get_refund_status_display() if obj.refund_status else ""

    @action(description="Retry refund", permissions=["change"])
    def retry_refund(self, request, queryset):
        retry_refunds_for(self, request, [invoice.order for invoice in queryset.select_related("order")])
