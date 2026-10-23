"""Admin-site helpers for refunds, shared by the Order and Invoice admins."""

from django.contrib import messages

from .models import RefundStatus
from .refunds import RefundError, retry_refund


def retry_refunds_for(modeladmin, request, orders):
    """
    Retry the refund on each order, reporting what happened to each.

    A refund that is already done, or an order that was never paid online, is
    reported to the admin, not raised as an error.
    """
    done, problems = 0, []
    for order in orders:
        try:
            invoice = retry_refund(order)
        except RefundError as exc:
            problems.append(f"{order.order_number}: {exc}")
            continue
        if invoice.refund_status == RefundStatus.FAILED:
            problems.append(f"{order.order_number}: {invoice.refund_error or 'still failing'}")
        else:
            done += 1

    if done:
        modeladmin.message_user(request, f"{done} refund(s) sent or confirmed.", messages.SUCCESS)
    for line in problems:
        modeladmin.message_user(request, f"Not refunded - {line}", messages.WARNING)
