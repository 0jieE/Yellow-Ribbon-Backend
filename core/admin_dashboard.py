"""
Everything the Unfold admin needs that is not a model admin: the dashboard on
/admin/, the sidebar badge, the environment ribbon, and the status badge
colours shared by every changelist.

Revenue is recognised on delivery and "today" is the Manila calendar day, the
same rules as GET /api/admin/dashboard/ — the headline numbers come from that
endpoint's own function so the two screens cannot disagree.
"""

import json
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.timesince import timesince

from accounts.models import Rider
from orders.admin_api import dashboard_summary
from orders.constants import (
    ACTIVE_DELIVERY_STATUSES,
    DeliveryStatus,
    OrderStatus,
    PaymentStatus,
)
from orders.models import Order, OrderItem

CHART_DAYS = 7
LIST_LIMIT = 6

# Unfold badge variants: info, danger, warning, success, primary, or None for
# neutral grey. Keyed by the *display* text because that is what @display
# receives for its `label=` mapping.
ORDER_STATUS_VARIANT = {
    OrderStatus.PENDING_PAYMENT: None,
    OrderStatus.PENDING: "warning",
    OrderStatus.CONFIRMED: "info",
    OrderStatus.PREPARING: "info",
    OrderStatus.READY: "primary",
    OrderStatus.OUT_FOR_DELIVERY: "primary",
    OrderStatus.DELIVERED: "success",
    OrderStatus.CANCELLED: "danger",
}

DELIVERY_STATUS_VARIANT = {
    DeliveryStatus.ASSIGNED: "warning",
    DeliveryStatus.PICKED_UP: "info",
    DeliveryStatus.IN_TRANSIT: "primary",
    DeliveryStatus.DELIVERED: "success",
    DeliveryStatus.FAILED: "danger",
}

PAYMENT_STATUS_VARIANT = {
    PaymentStatus.UNPAID: "warning",
    PaymentStatus.PAID: "success",
    PaymentStatus.FAILED: "danger",
    PaymentStatus.REFUNDED: "info",
}


def _by_display(variants):
    """{value: variant} -> {display text: variant}, the shape @display wants."""
    return {str(status.label): variant for status, variant in variants.items()}


ORDER_STATUS_LABELS = _by_display(ORDER_STATUS_VARIANT)
DELIVERY_STATUS_LABELS = _by_display(DELIVERY_STATUS_VARIANT)
PAYMENT_STATUS_LABELS = _by_display(PAYMENT_STATUS_VARIANT)


def peso(value):
    return f"₱{Decimal(value or 0):,.2f}"


def badge(text, variant=None):
    """A status pill, for table cells the dashboard builds by hand."""
    return render_to_string(
        "unfold/helpers/label.html",
        {"text": text, "variant": variant, "size": "md"},
    )


def environment_callback(_request):
    """Ribbon in the header, so nobody mistakes production for a dev box."""
    if settings.DEBUG:
        return ["Development", "info"]
    return ["Production", "danger"]


def pending_orders_badge(_request):
    """Sidebar counter: orders waiting on the shop to accept them."""
    count = Order.objects.filter(status=OrderStatus.PENDING).count()
    return str(count) if count else None


def _changelist(model, **query):
    url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
    if query:
        url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
    return url


def _order_link(order):
    url = reverse("admin:orders_order_change", args=[order.pk])
    return format_html('<a href="{}" class="font-semibold">{}</a>', url, order.order_number)


def _customer_name(order):
    user = order.customer.user
    return user.get_full_name() or user.username


def _order_rows(orders):
    return [
        [
            _order_link(o),
            _customer_name(o),
            badge(o.get_status_display(), ORDER_STATUS_VARIANT[o.status]),
            peso(o.total),
            f"{timesince(o.placed_at, depth=1)} ago",
        ]
        for o in orders
    ]


ORDER_HEADERS = ["Order", "Customer", "Status", "Total", "Placed"]


def _revenue_chart(today):
    """Delivered revenue per local day for the last CHART_DAYS days, as JSON."""
    first = today - timedelta(days=CHART_DAYS - 1)
    per_day = {
        row["day"]: row
        for row in Order.objects.filter(
            status=OrderStatus.DELIVERED, delivered_at__date__gte=first
        )
        .annotate(day=TruncDate("delivered_at"))
        .values("day")
        .annotate(revenue=Sum("total"), orders=Count("id"))
    }
    days = [first + timedelta(days=i) for i in range(CHART_DAYS)]
    return json.dumps(
        {
            "labels": [d.strftime("%a %d") for d in days],
            "datasets": [
                {
                    "label": "Revenue (₱)",
                    "data": [float(per_day.get(d, {}).get("revenue") or 0) for d in days],
                    "backgroundColor": "var(--color-primary-500)",
                    "borderRadius": 4,
                    # Unfold's default caps bars at 4px, which reads as lines.
                    "maxBarThickness": 36,
                }
            ],
        }
    )


def _top_items(today):
    first = today - timedelta(days=CHART_DAYS - 1)
    return (
        OrderItem.objects.filter(
            order__status=OrderStatus.DELIVERED, order__delivered_at__date__gte=first
        )
        .values("menu__name")
        .annotate(sold=Sum("quantity"))
        .order_by("-sold")[:5]
    )


def dashboard_callback(_request, context):
    """Feeds templates/admin/index.html."""
    summary = dashboard_summary()
    today = timezone.localdate()
    counts = summary["status_counts"]
    day = summary["today"]
    riders = summary["riders"]

    orders = Order.objects.select_related("customer__user")

    awaiting = orders.filter(status=OrderStatus.PENDING).order_by("placed_at")
    needs_rider = orders.filter(
        status__in=[OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY],
        delivery__isnull=True,
    ).order_by("placed_at")
    recent = orders.order_by("-placed_at")[:8]

    on_duty = (
        Rider.objects.filter(is_available=True)
        .select_related("user")
        .annotate(
            active=Count(
                "deliveries", filter=Q(deliveries__status__in=ACTIVE_DELIVERY_STATUSES)
            )
        )
        .order_by("user__username")
    )

    context.update(
        {
            "kpis": [
                {
                    "title": "Revenue today",
                    "value": peso(day["revenue"]),
                    "footer": f"{peso(day['food_sales'])} food · {peso(day['delivery_fees'])} delivery",
                    "icon": "payments",
                    "href": _changelist(Order, status__exact="delivered"),
                },
                {
                    "title": "Orders today",
                    "value": day["orders_placed"],
                    "footer": f"{day['orders_delivered']} delivered · {day['orders_cancelled']} cancelled",
                    "icon": "receipt_long",
                    "href": _changelist(Order),
                },
                {
                    "title": "Active orders",
                    "value": summary["active_orders"],
                    "footer": f"{counts[OrderStatus.PENDING]} awaiting confirmation",
                    "icon": "pending_actions",
                    "href": _changelist(Order, status__exact="pending"),
                },
                {
                    "title": "Riders on duty",
                    "value": riders["on_duty"],
                    "footer": f"{riders['on_delivery']} on a delivery · {riders['total']} total",
                    "icon": "two_wheeler",
                    "href": _changelist(Rider, is_available__exact=1),
                },
            ],
            "status_chips": [
                {
                    "text": f"{status.label} · {counts[status.value]}",
                    "variant": ORDER_STATUS_VARIANT[status],
                    "href": _changelist(Order, status__exact=status.value),
                }
                for status in OrderStatus
            ],
            "revenue_chart": _revenue_chart(today),
            "chart_title": f"Revenue, last {CHART_DAYS} days",
            "top_items_table": {
                "headers": ["Item", "Sold"],
                "rows": [[row["menu__name"], row["sold"]] for row in _top_items(today)],
            },
            "top_items_title": f"Top sellers, last {CHART_DAYS} days",
            "awaiting_desc": f"{awaiting.count()} waiting for the shop to accept",
            "awaiting_table": {
                "headers": ORDER_HEADERS,
                "rows": _order_rows(awaiting[:LIST_LIMIT]),
            },
            "needs_rider_desc": f"{needs_rider.count()} accepted, no rider assigned",
            "needs_rider_table": {
                "headers": ORDER_HEADERS,
                "rows": _order_rows(needs_rider[:LIST_LIMIT]),
            },
            "recent_table": {
                "headers": ORDER_HEADERS,
                "rows": _order_rows(recent),
            },
            "riders_table": {
                "headers": ["Rider", "Vehicle", "Active deliveries"],
                "rows": [
                    [
                        format_html(
                            '<a href="{}" class="font-semibold">{}</a>',
                            reverse("admin:accounts_rider_change", args=[r.pk]),
                            r.user.get_full_name() or r.user.username,
                        ),
                        r.vehicle_type or "—",
                        r.active,
                    ]
                    for r in on_duty
                ],
            },
        }
    )
    return context

