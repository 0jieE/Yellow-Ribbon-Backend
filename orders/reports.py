"""
Admin reporting over a date range.

The dashboard answers "how are we doing today". These answer "how did the
month go". Revenue is recognised on delivery, exactly as the dashboard does
it, so a pile of pending orders cannot inflate the figures.

All dates are local dates in TIME_ZONE (Asia/Manila), not UTC — a delivery
at 07:30 Manila on the 3rd belongs to the 3rd, not the 2nd.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from accounts.models import Rider
from core.permissions import IsAdminUser
from delivery.models import Delivery
from menu.models import Menu

from .constants import DeliveryStatus, OrderStatus
from .models import Order

DEFAULT_RANGE_DAYS = 30

# A guard against an accidental ten-year request building a daily series with
# thousands of entries. Raise it if a longer report is ever wanted.
MAX_RANGE_DAYS = 366

# Sums can exceed a single order's 10-digit ceiling, so aggregates get their
# own wider field.
MONEY = DecimalField(max_digits=14, decimal_places=2)
ZERO = Value(Decimal("0.00"), output_field=MONEY)


def _money(value):
    """Decimals go over the wire as strings, like every other amount."""
    return str(value if value is not None else Decimal("0.00"))


def _parse_date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError(
            {field: f"'{value}' is not a date. Use YYYY-MM-DD."}
        )


def parse_range(request):
    """
    Read ?start= and ?end=, defaulting to the last 30 days inclusive.

    Returns (start, end) as local dates, both ends inclusive.
    """
    today = timezone.localdate()

    end_raw = request.query_params.get("end")
    start_raw = request.query_params.get("start")

    end = _parse_date(end_raw, "end") if end_raw else today
    start = (
        _parse_date(start_raw, "start")
        if start_raw
        else end - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    )

    if start > end:
        raise ValidationError(
            {
                "start": (
                    f"start ({start.isoformat()}) is after end "
                    f"({end.isoformat()}). Swap them."
                )
            }
        )

    span = (end - start).days + 1
    if span > MAX_RANGE_DAYS:
        raise ValidationError(
            {
                "end": (
                    f"That range covers {span} days. "
                    f"The most this report will build is {MAX_RANGE_DAYS}."
                )
            }
        )

    return start, end


def _delivered_in(start, end):
    """Orders whose revenue falls in the window. The basis for every figure."""
    return Order.objects.filter(
        status=OrderStatus.DELIVERED, delivered_at__date__range=(start, end)
    )


@api_view(["GET"])
@permission_classes([IsAdminUser])
def sales_report(request):
    """
    GET /api/admin/reports/sales/?start=YYYY-MM-DD&end=YYYY-MM-DD

    Each metric sits on its own date basis: revenue and deliveries on
    delivered_at, placements on placed_at, cancellations on cancelled_at.
    Every day in the range appears in `daily`, zeroes included, so a chart
    has no gaps.
    """
    start, end = parse_range(request)

    delivered = (
        _delivered_in(start, end)
        .annotate(day=TruncDate("delivered_at"))
        .values("day")
        .annotate(
            revenue=Coalesce(Sum("total"), ZERO, output_field=MONEY),
            food_sales=Coalesce(Sum("subtotal"), ZERO, output_field=MONEY),
            delivery_fees=Coalesce(Sum("delivery_fee"), ZERO, output_field=MONEY),
            orders=Count("id"),
        )
    )
    placed = (
        Order.objects.filter(placed_at__date__range=(start, end))
        .annotate(day=TruncDate("placed_at"))
        .values("day")
        .annotate(orders=Count("id"))
    )
    cancelled = (
        Order.objects.filter(
            status=OrderStatus.CANCELLED, cancelled_at__date__range=(start, end)
        )
        .annotate(day=TruncDate("cancelled_at"))
        .values("day")
        .annotate(orders=Count("id"))
    )

    by_day = {row["day"]: row for row in delivered}
    placed_by_day = {row["day"]: row["orders"] for row in placed}
    cancelled_by_day = {row["day"]: row["orders"] for row in cancelled}

    daily = []
    totals = {
        "revenue": Decimal("0.00"),
        "food_sales": Decimal("0.00"),
        "delivery_fees": Decimal("0.00"),
        "orders_placed": 0,
        "orders_delivered": 0,
        "orders_cancelled": 0,
    }

    cursor = start
    while cursor <= end:
        row = by_day.get(cursor)
        revenue = row["revenue"] if row else Decimal("0.00")
        food = row["food_sales"] if row else Decimal("0.00")
        fees = row["delivery_fees"] if row else Decimal("0.00")
        delivered_count = row["orders"] if row else 0
        placed_count = placed_by_day.get(cursor, 0)
        cancelled_count = cancelled_by_day.get(cursor, 0)

        daily.append(
            {
                "date": cursor.isoformat(),
                "revenue": _money(revenue),
                "food_sales": _money(food),
                "delivery_fees": _money(fees),
                "orders_placed": placed_count,
                "orders_delivered": delivered_count,
                "orders_cancelled": cancelled_count,
            }
        )

        totals["revenue"] += revenue
        totals["food_sales"] += food
        totals["delivery_fees"] += fees
        totals["orders_placed"] += placed_count
        totals["orders_delivered"] += delivered_count
        totals["orders_cancelled"] += cancelled_count

        cursor += timedelta(days=1)

    delivered_total = totals["orders_delivered"]
    average = (
        (totals["revenue"] / delivered_total).quantize(Decimal("0.01"))
        if delivered_total
        else Decimal("0.00")
    )

    return Response(
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": len(daily),
            "totals": {
                "revenue": _money(totals["revenue"]),
                "food_sales": _money(totals["food_sales"]),
                "delivery_fees": _money(totals["delivery_fees"]),
                "orders_placed": totals["orders_placed"],
                "orders_delivered": totals["orders_delivered"],
                "orders_cancelled": totals["orders_cancelled"],
                "average_order_value": _money(average),
            },
            "daily": daily,
        }
    )


@api_view(["GET"])
@permission_classes([IsAdminUser])
def riders_report(request):
    """
    GET /api/admin/reports/riders/?start=&end=

    Every rider appears, including those who did nothing in the window —
    an idle rider is worth seeing. Ordered by deliveries completed.

    Note on dates: completed deliveries and earnings are counted on
    `delivered_at`. Failures have no timestamp of their own on the model, so
    they are counted on `assigned_at` — a delivery assigned late on the 1st
    and failed on the 2nd lands on the 1st.
    """
    start, end = parse_range(request)

    completed_in_range = Q(
        deliveries__status=DeliveryStatus.DELIVERED,
        deliveries__delivered_at__date__range=(start, end),
    )
    failed_in_range = Q(
        deliveries__status=DeliveryStatus.FAILED,
        deliveries__assigned_at__date__range=(start, end),
    )

    riders = (
        Rider.objects.select_related("user")
        .annotate(
            completed=Count("deliveries", filter=completed_in_range, distinct=True),
            failed=Count("deliveries", filter=failed_in_range, distinct=True),
            earned=Coalesce(
                Sum("deliveries__rider_earnings", filter=completed_in_range),
                ZERO,
                output_field=MONEY,
            ),
        )
        .order_by("-completed", "-earned", "user__username")
    )

    rows = [
        {
            "rider_id": rider.pk,
            "username": rider.user.username,
            "name": rider.user.get_full_name() or rider.user.username,
            "is_active": rider.user.is_active,
            "deliveries_completed": rider.completed,
            "deliveries_failed": rider.failed,
            "earnings": _money(rider.earned),
        }
        for rider in riders
    ]

    return Response(
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "totals": {
                "deliveries_completed": sum(r["deliveries_completed"] for r in rows),
                "deliveries_failed": sum(r["deliveries_failed"] for r in rows),
                "earnings": _money(
                    sum((Decimal(r["earnings"]) for r in rows), Decimal("0.00"))
                ),
            },
            "riders": rows,
        }
    )


@api_view(["GET"])
@permission_classes([IsAdminUser])
def menu_report(request):
    """
    GET /api/admin/reports/menu/?start=&end=

    Every menu item appears, including ones that sold nothing — an item with
    zero sales is the worst seller and the whole point of looking. Ordered by
    quantity sold, so the best are at the top and the worst at the bottom.

    Counts only lines on delivered orders, matching the revenue basis.
    """
    start, end = parse_range(request)

    sold_in_range = Q(
        order_items__order__status=OrderStatus.DELIVERED,
        order_items__order__delivered_at__date__range=(start, end),
    )

    items = (
        Menu.objects.annotate(
            quantity_sold=Coalesce(
                Sum("order_items__quantity", filter=sold_in_range), Value(0)
            ),
            revenue=Coalesce(
                Sum("order_items__line_subtotal", filter=sold_in_range),
                ZERO,
                output_field=MONEY,
            ),
        )
        .order_by("-quantity_sold", "-revenue", "name")
    )

    rows = [
        {
            "menu_id": item.pk,
            "name": item.name,
            "price": str(item.price),
            "is_available": item.is_available,
            "quantity_sold": item.quantity_sold,
            "revenue": _money(item.revenue),
        }
        for item in items
    ]

    return Response(
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "totals": {
                "items": len(rows),
                "quantity_sold": sum(r["quantity_sold"] for r in rows),
                # Food only — delivery fees are not attributable to an item.
                "revenue": _money(
                    sum((Decimal(r["revenue"]) for r in rows), Decimal("0.00"))
                ),
            },
            "items": rows,
        }
    )
