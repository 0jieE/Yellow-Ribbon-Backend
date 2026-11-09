"""
Status enums, defined once (spec section 5).

Exposed over GET /api/constants/ so admin_app, customer_app and rider_app
all read the same values instead of hardcoding their own copies.
"""

from django.db import models


class OrderStatus(models.TextChoices):
    PENDING_PAYMENT = "pending_payment", "Pending payment"
    PENDING = "pending", "Pending"
    CONFIRMED = "confirmed", "Confirmed"
    PREPARING = "preparing", "Preparing"
    READY = "ready", "Ready"
    OUT_FOR_DELIVERY = "out_for_delivery", "Out for delivery"
    DELIVERED = "delivered", "Delivered"
    CANCELLED = "cancelled", "Cancelled"


class DeliveryStatus(models.TextChoices):
    ASSIGNED = "assigned", "Assigned"
    PICKED_UP = "picked_up", "Picked up"
    IN_TRANSIT = "in_transit", "In transit"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"


class PaymentType(models.TextChoices):
    COD = "cod", "Cash on delivery"
    GCASH = "gcash", "GCash"
    MAYA = "maya", "Maya"


class PaymentStatus(models.TextChoices):
    UNPAID = "unpaid", "Unpaid"
    PAID = "paid", "Paid"
    FAILED = "failed", "Failed"
    REFUNDED = "refunded", "Refunded"


# Plain value lists, in the order given by the spec.
ORDER_STATUS = [c.value for c in OrderStatus]
DELIVERY_STATUS = [c.value for c in DeliveryStatus]
PAYMENT_TYPE = [c.value for c in PaymentType]
PAYMENT_STATUS = [c.value for c in PaymentStatus]


# ---------------------------------------------------------------------
# Legal status transitions (spec section 5)
# ---------------------------------------------------------------------

# An order walks the happy path one step at a time. Anything else is a 400.
# Cancellation stays open until the order is out the door and delivered; a
# failed delivery is modelled on Delivery.status, not here.
ORDER_STATUS_TRANSITIONS = {
    OrderStatus.PENDING_PAYMENT: {OrderStatus.PENDING, OrderStatus.CANCELLED},
    OrderStatus.PENDING: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: {OrderStatus.PREPARING, OrderStatus.CANCELLED},
    OrderStatus.PREPARING: {OrderStatus.READY, OrderStatus.CANCELLED},
    OrderStatus.READY: {OrderStatus.OUT_FOR_DELIVERY, OrderStatus.CANCELLED},
    OrderStatus.OUT_FOR_DELIVERY: {OrderStatus.DELIVERED, OrderStatus.CANCELLED},
    # Terminal.
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}

TERMINAL_ORDER_STATUSES = {OrderStatus.DELIVERED, OrderStatus.CANCELLED}

# What the store (admin app, admin API and the Django admin) may do by hand.
# out_for_delivery and delivered are never targets here: they belong to the
# rider, whose pickup and drop-off move the order. If the store could set
# them, the rider's own pickup would then be refused and the delivery would
# be left open on a finished order.
ADMIN_ORDER_STATUS_TRANSITIONS = {
    OrderStatus.PENDING_PAYMENT: {OrderStatus.CANCELLED},
    OrderStatus.PENDING: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: {OrderStatus.PREPARING, OrderStatus.CANCELLED},
    OrderStatus.PREPARING: {OrderStatus.READY, OrderStatus.CANCELLED},
    OrderStatus.READY: {OrderStatus.CANCELLED},
    OrderStatus.OUT_FOR_DELIVERY: {OrderStatus.CANCELLED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}

# A rider is assigned only once the store has accepted the order, and before
# it leaves the store.
RIDER_ASSIGNABLE_STATUSES = {OrderStatus.CONFIRMED, OrderStatus.PREPARING, OrderStatus.READY}

# A rider holding a delivery in any of these is still working it, so they do
# not appear in GET /api/admin/riders/available/.
ACTIVE_DELIVERY_STATUSES = {
    DeliveryStatus.ASSIGNED,
    DeliveryStatus.PICKED_UP,
    DeliveryStatus.IN_TRANSIT,
}

TERMINAL_DELIVERY_STATUSES = {DeliveryStatus.DELIVERED, DeliveryStatus.FAILED}

# The rider walks assigned -> picked_up -> in_transit -> delivered. `failed`
# is for trouble on the road (the customer is not home, the address is wrong),
# so only after pickup; before pickup the store changes the rider instead.
# Cancelling the order also closes an active delivery as failed.
DELIVERY_STATUS_TRANSITIONS = {
    DeliveryStatus.ASSIGNED: {DeliveryStatus.PICKED_UP},
    DeliveryStatus.PICKED_UP: {DeliveryStatus.IN_TRANSIT, DeliveryStatus.FAILED},
    DeliveryStatus.IN_TRANSIT: {DeliveryStatus.DELIVERED, DeliveryStatus.FAILED},
    DeliveryStatus.DELIVERED: set(),
    DeliveryStatus.FAILED: set(),
}


def allowed_delivery_transitions(current):
    return sorted(DELIVERY_STATUS_TRANSITIONS.get(current, set()))


def is_valid_delivery_transition(current, new):
    return new in DELIVERY_STATUS_TRANSITIONS.get(current, set())

# The customer may cancel while the order has not been accepted yet. That
# includes an abandoned GCash checkout sitting in pending_payment.
CUSTOMER_CANCELLABLE_STATUSES = {OrderStatus.PENDING_PAYMENT, OrderStatus.PENDING}


def allowed_transitions(current):
    """The statuses an order in `current` may legally move to."""
    return sorted(ORDER_STATUS_TRANSITIONS.get(current, set()))


def is_valid_transition(current, new):
    return new in ORDER_STATUS_TRANSITIONS.get(current, set())


def admin_allowed_transitions(current):
    """The statuses the store may move an order in `current` to by hand."""
    return sorted(ADMIN_ORDER_STATUS_TRANSITIONS.get(current, set()))


def is_valid_admin_transition(current, new):
    return new in ADMIN_ORDER_STATUS_TRANSITIONS.get(current, set())
