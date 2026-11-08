"""
How an order looks to the customer: the same labels, colours, icons and
seven-step progress as the Android customer app (customer_app/lib/customer/
order_status.dart), so an order reads the same on both.
"""

from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from django.utils.dateformat import format as date_format

from orders.constants import OrderStatus, PaymentStatus, PaymentType

GREY = "#616161"
AWAITING = "#B26A00"

# status: (label, colour, Material icon)
STATUS_STYLES = {
    "pending_payment": ("Awaiting payment", "#8D6E63", "hourglass_empty"),
    "pending": ("Pending", "#B26A00", "receipt_long"),
    "confirmed": ("Confirmed", "#1565C0", "check_circle"),
    "preparing": ("Preparing", "#6A1B9A", "local_fire_department"),
    "ready": ("Ready", "#00838F", "inventory_2"),
    "out_for_delivery": ("Out for delivery", "#00796B", "delivery_dining"),
    # A delivery status, shown because "in transit" is what the customer cares about.
    "in_transit": ("In transit", "#00695C", "two_wheeler"),
    "delivered": ("Delivered", "#2E7D32", "task_alt"),
    "cancelled": ("Cancelled", "#C62828", "cancel"),
}

STEPS = ["pending", "confirmed", "preparing", "ready", "out_for_delivery", "in_transit", "delivered"]
STEP_LABELS = ["Order placed", "Confirmed", "Preparing", "Ready for pickup", "Picked up", "On the way", "Delivered"]
TOTAL_STEPS = len(STEPS)

PAYMENT_NAMES = {PaymentType.COD: "Cash on delivery", PaymentType.GCASH: "GCash", PaymentType.MAYA: "Maya Wallet"}
PAYMENT_LABELS = {PaymentType.GCASH: "GCash", PaymentType.MAYA: "Maya"}


def full_date(value):
    """'9 Nov 2026, 2:05 PM' in the shop's time zone, as the app writes dates."""
    if value is None:
        return "—"
    return date_format(timezone.localtime(value), "j M Y, g:i A")


def status_style(status):
    label, colour, icon = STATUS_STYLES.get(status, (status.replace("_", " "), GREY, "info"))
    return {"key": status, "label": label, "colour": colour, "icon": icon}


def delivery_status(order):
    try:
        return order.delivery.status
    except ObjectDoesNotExist:
        return None


def steps_done(status, delivery):
    """How many of the seven steps are done; None for a cancelled order."""
    if status == OrderStatus.CANCELLED:
        return None
    if status == OrderStatus.PENDING_PAYMENT:
        return 0
    if status == OrderStatus.DELIVERED:
        return TOTAL_STEPS
    if status == OrderStatus.OUT_FOR_DELIVERY:
        return 6 if delivery == "in_transit" else 5
    return STEPS.index(status) + 1 if status in STEPS else 0


def progress(order):
    """Everything the orders list and the order page show about where an order is."""
    delivery = delivery_status(order)
    display = "in_transit" if order.status == OrderStatus.OUT_FOR_DELIVERY and delivery == "in_transit" else order.status
    style = status_style(display)
    done = steps_done(order.status, delivery)

    if order.status == OrderStatus.CANCELLED:
        step_label = "Cancelled"
    elif order.status == OrderStatus.PENDING_PAYMENT:
        step_label = "Awaiting payment"
    elif not done:
        step_label = status_style(order.status)["label"]
    else:
        step_label = STEP_LABELS[done - 1]

    terminal = order.status in (OrderStatus.DELIVERED, OrderStatus.CANCELLED)
    if order.status == OrderStatus.DELIVERED:
        note = "completed"
    elif order.status == OrderStatus.CANCELLED:
        note = "cancelled"
    elif order.eta_minutes and order.status == OrderStatus.OUT_FOR_DELIVERY:
        note = f"~{order.eta_minutes} min"
    else:
        left = TOTAL_STEPS - (done or 0)
        note = "almost there" if left <= 0 else f"{left} left"

    return {
        "style": style,
        "done": done,
        "total": TOTAL_STEPS,
        "percent": 0 if done is None else round(done * 100 / TOTAL_STEPS),
        "step_label": step_label,
        "bar_colour": "#BDBDBD" if order.status == OrderStatus.CANCELLED else style["colour"],
        "label_colour": "#9E9E9E" if done is None else style["colour"],
        "terminal": terminal,
        "note": note,
        "delivery_status": delivery,
    }


def tracking_message(order):
    """What the tinted box on the order page says while there is no rider on the map."""
    return {
        OrderStatus.PENDING_PAYMENT: "Tracking starts once your payment is confirmed.",
        OrderStatus.PENDING: "Waiting for the store to accept your order.",
        OrderStatus.CONFIRMED: "The store has accepted your order. A rider is assigned once it is ready.",
        OrderStatus.PREPARING: "Your order is being prepared.",
        OrderStatus.READY: "Your order is ready and waiting for a rider.",
        OrderStatus.OUT_FOR_DELIVERY: (
            "Your rider has collected your order. The map and arrival time appear once they "
            "set off — this page refreshes itself."
        ),
        OrderStatus.DELIVERED: f"Delivered {full_date(order.delivered_at)}.",
        OrderStatus.CANCELLED: "This order was cancelled.",
    }.get(order.status, "Live tracking is not available for this order.")


def awaiting_payment(order):
    return (
        order.status == OrderStatus.PENDING_PAYMENT
        and order.payment_type != PaymentType.COD
        and order.payment_status != PaymentStatus.PAID
    )


def payment_chip(order, invoice):
    has_refund = bool(invoice is not None and invoice.refund_status)
    if order.payment_status == PaymentStatus.PAID and has_refund:
        return {"label": "Refund pending", "colour": AWAITING}
    if order.payment_status == PaymentStatus.PAID:
        return {"label": "Paid", "colour": "#2E7D32"}
    if order.payment_status == PaymentStatus.FAILED:
        return {"label": "Failed", "colour": "#C62828"}
    if order.payment_status == PaymentStatus.REFUNDED:
        return {"label": "Refunded", "colour": GREY}
    if awaiting_payment(order):
        return {"label": "Awaiting payment", "colour": AWAITING}
    if order.payment_type == PaymentType.COD:
        return {"label": "Pay on delivery", "colour": GREY}
    return {"label": "Unpaid", "colour": GREY}


def refund_note(invoice, method):
    """Where the money stands after a paid order was cancelled, or None."""
    if invoice is None or not invoice.refund_status:
        return None
    amount = invoice.refund_amount if invoice.refund_amount is not None else invoice.amount_paid
    amount = f"₱{amount:,.2f}"
    if invoice.refund_status == "refunded":
        return {"icon": "check_circle", "colour": "#2E7D32",
                "text": f"{amount} was refunded to your {method} on {full_date(invoice.refunded_at)}."}
    if invoice.refund_status == "failed":
        return {"icon": "error", "colour": "#C62828",
                "text": f"Your {amount} refund has not gone through yet. The store has been told and will retry it."}
    return {"icon": "hourglass_top", "colour": AWAITING, "text": f"Refund of {amount} to your {method} is on the way."}
