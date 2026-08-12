"""
The four push events from spec section 9.

  new order        -> admin
  order assigned   -> rider
  status change    -> customer
  delivery done    -> customer + admin

Keeping the copy here means the three Flutter apps get consistent wording
and a consistent `data` payload to deep-link from.
"""

import logging

from .fcm import notify_admins, notify_users

logger = logging.getLogger(__name__)

# What the customer is told for each order status. Statuses the customer
# does not need a buzz for are simply absent.
CUSTOMER_STATUS_MESSAGES = {
    "pending": ("Order received", "We have your order {number} and will confirm it shortly."),
    "confirmed": ("Order confirmed", "{number} is confirmed. We are getting started."),
    "preparing": ("Now preparing", "The kitchen is cooking {number}."),
    "ready": ("Ready for pickup", "{number} is ready and waiting for a rider."),
    "out_for_delivery": ("On the way", "Your rider has picked up {number}."),
    "delivered": ("Delivered", "{number} has been delivered. Enjoy!"),
    "cancelled": ("Order cancelled", "{number} has been cancelled."),
}


def _order_data(order, **extra):
    payload = {
        "type": "order",
        "order_id": order.pk,
        "order_number": order.order_number,
        "status": order.status,
    }
    payload.update(extra)
    return payload


def notify_new_order(order):
    """A new order has reached the kitchen — tell the admin."""
    notify_admins(
        "New order",
        f"{order.order_number} — {order.total} via "
        f"{order.get_payment_type_display()}.",
        _order_data(order, event="new_order"),
    )


def notify_rider_assigned(order, rider):
    """A rider has been given a delivery."""
    notify_users(
        rider.user,
        "New delivery assigned",
        f"{order.order_number} to {order.delivery_address}.",
        _order_data(order, event="assigned", rider_id=rider.pk),
    )


def notify_order_status(order):
    """The customer's order moved."""
    message = CUSTOMER_STATUS_MESSAGES.get(order.status)
    if message is None:
        return

    title, template = message
    notify_users(
        order.customer.user,
        title,
        template.format(number=order.order_number),
        _order_data(order, event="status_change"),
    )


def notify_order_cancelled_by_customer(order):
    """
    The customer pulled their own order.

    The kitchen may already have started on it, so the admin needs to know
    without waiting to spot it on the dashboard.
    """
    user = order.customer.user
    who = user.get_full_name() or user.username
    notify_admins(
        "Order cancelled by customer",
        f"{who} cancelled {order.order_number} ({order.total}).",
        _order_data(order, event="cancelled_by_customer"),
    )


def notify_delivery_failed(order, delivery):
    """
    A rider could not complete a delivery.

    The order is deliberately left where it is, so nothing moves until an
    admin reassigns or cancels it — which makes this push the only thing
    telling them to act.
    """
    user = delivery.rider.user
    who = user.get_full_name() or user.username
    reason = (order.cancel_reason or "").strip()
    detail = f" — {reason}" if reason else ""

    notify_admins(
        "Delivery failed",
        f"{who} could not deliver {order.order_number}{detail}. "
        "Reassign or cancel it.",
        _order_data(
            order,
            event="delivery_failed",
            delivery_id=delivery.pk,
            rider_id=delivery.rider_id,
        ),
    )


def notify_rider_order_cancelled(order, rider):
    """
    An order a rider is still carrying has been cancelled.

    Whoever cancelled it, the rider may be mid-route — they have to be told
    to stand down rather than turning up at the door.
    """
    notify_users(
        rider.user,
        "Order cancelled",
        f"{order.order_number} has been cancelled. Stop the delivery.",
        _order_data(order, event="cancelled", rider_id=rider.pk),
    )


def notify_delivery_completed(order):
    """
    Delivery finished — the customer is told by the status push, so this
    covers the admin side of "customer + admin".
    """
    notify_admins(
        "Delivery completed",
        f"{order.order_number} has been delivered.",
        _order_data(order, event="delivered"),
    )
