"""
Payments and earnings report for the admin app.

Answers "how much did each payment method bring in, what did the gateways
keep, and what went back to customers" for a date range.

Dates are local (Asia/Manila), like the sales report, and the two sides of the
ledger sit on their own date: a payment counts on the day it was **paid**, a
refund on the day it was **refunded**. So refunding last week's order shows up
in this week's refunds, which is when the money actually left.

Gateway fees are the snapshot taken on each invoice at settlement, from the
rates in StoreSettings at that moment. They are not returned on refunds, so
`net` subtracts both.
"""

from decimal import Decimal

from django.db.models import Count, DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from core.pagination import AdminOrderPagination
from core.permissions import IsAdminUser
from orders.reports import parse_range
from orders.serializers import refund_summary

from .models import Invoice, Provider, RefundStatus

MONEY = DecimalField(max_digits=14, decimal_places=2)
ZERO = Value(Decimal("0.00"), output_field=MONEY)

# Report order, and the label the app shows.
PROVIDER_ORDER = [Provider.PAYMONGO, Provider.MAYA, Provider.COD]
PROVIDER_LABELS = {
    Provider.PAYMONGO: "GCash (PayMongo)",
    Provider.MAYA: "Maya",
    Provider.COD: "Cash on delivery",
}


def _money(value):
    return str(value if value is not None else Decimal("0.00"))


def _sum(field):
    return Coalesce(Sum(field), ZERO, output_field=MONEY)


@api_view(["GET"])
@permission_classes([IsAdminUser])
def payments_report(request):
    """
    GET /api/admin/reports/payments/?start=YYYY-MM-DD&end=YYYY-MM-DD

    One row per provider (all three always present, zeros included), plus
    totals and a count of refunds that still need attention.
    """
    start, end = parse_range(request)

    paid = {
        row["provider"]: row
        for row in Invoice.objects.filter(paid_at__date__range=(start, end))
        .values("provider")
        .annotate(count=Count("id"), gross=_sum("amount_paid"), fees=_sum("fee"))
    }
    refunded = {
        row["provider"]: row
        for row in Invoice.objects.filter(
            refund_status=RefundStatus.REFUNDED, refunded_at__date__range=(start, end)
        )
        .values("provider")
        .annotate(count=Count("id"), amount=_sum("refund_amount"))
    }

    rows = []
    totals = {
        "count": 0,
        "gross": Decimal("0.00"),
        "fees": Decimal("0.00"),
        "refunds": Decimal("0.00"),
        "refund_count": 0,
        "net": Decimal("0.00"),
    }
    for provider in PROVIDER_ORDER:
        p = paid.get(provider, {})
        r = refunded.get(provider, {})
        gross = p.get("gross", Decimal("0.00"))
        fees = p.get("fees", Decimal("0.00"))
        refunds = r.get("amount", Decimal("0.00"))
        net = gross - fees - refunds
        count = p.get("count", 0)
        refund_count = r.get("count", 0)

        rows.append(
            {
                "provider": provider.value,
                "label": PROVIDER_LABELS[provider],
                "count": count,
                "gross": _money(gross),
                "fees": _money(fees),
                "refund_count": refund_count,
                "refunds": _money(refunds),
                "net": _money(net),
            }
        )
        totals["count"] += count
        totals["gross"] += gross
        totals["fees"] += fees
        totals["refunds"] += refunds
        totals["refund_count"] += refund_count
        totals["net"] += net

    # Money owed back that has not gone out yet, whatever its date.
    outstanding = Invoice.objects.filter(
        refund_status__in=[RefundStatus.PENDING, RefundStatus.FAILED]
    ).aggregate(count=Count("id"), amount=_sum("refund_amount"))

    return Response(
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "providers": rows,
            "totals": {
                "count": totals["count"],
                "gross": _money(totals["gross"]),
                "fees": _money(totals["fees"]),
                "refund_count": totals["refund_count"],
                "refunds": _money(totals["refunds"]),
                "net": _money(totals["net"]),
            },
            "refunds_outstanding": {
                "count": outstanding["count"],
                "amount": _money(outstanding["amount"]),
            },
        }
    )


def _transaction_row(invoice):
    order = invoice.order
    user = order.customer.user
    return {
        "order_id": order.pk,
        "order_number": order.order_number,
        "customer": user.get_full_name() or user.username,
        "invoice_number": invoice.invoice_number,
        "provider": invoice.provider or None,
        "reference": invoice.paymongo_payment_id or None,
        "payer": invoice.payer or None,
        "amount": str(invoice.amount_paid),
        "fee": str(invoice.fee),
        "net": str(invoice.net_amount),
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
        "refund": refund_summary(invoice, for_admin=True),
    }


@api_view(["GET"])
@permission_classes([IsAdminUser])
def payments_transactions(request):
    """
    GET /api/admin/reports/payments/transactions/
        ?start=&end=&provider=paymongo|maya|cod&refund=none|pending|refunded|failed
        &page=&page_size=

    The settled payments behind the report's numbers, newest first, paginated
    as {"count", "next", "previous", "results"}.
    """
    start, end = parse_range(request)

    invoices = (
        Invoice.objects.filter(paid_at__date__range=(start, end))
        .select_related("order__customer__user")
        .order_by("-paid_at", "-id")
    )

    provider = request.query_params.get("provider")
    if provider:
        if provider not in {p.value for p in Provider}:
            raise ValidationError(
                {"provider": f"Unknown provider '{provider}'. Use paymongo, maya or cod."}
            )
        invoices = invoices.filter(provider=provider)

    refund = request.query_params.get("refund")
    if refund:
        allowed = {"none": RefundStatus.NONE, "pending": "pending", "refunded": "refunded", "failed": "failed"}
        if refund not in allowed:
            raise ValidationError(
                {"refund": f"Unknown refund filter '{refund}'. Use none, pending, refunded or failed."}
            )
        invoices = invoices.filter(refund_status=allowed[refund])

    paginator = AdminOrderPagination()
    page = paginator.paginate_queryset(invoices, request)
    return paginator.get_paginated_response([_transaction_row(i) for i in page])
