"""
Reap GCash orders that were never paid for.

An order sitting in `pending_payment` is invisible to the kitchen but still
holds a slot in the customer's order list. If the customer walks away from
the PayMongo page it stays there forever, so this cancels anything older
than PENDING_PAYMENT_TIMEOUT_MINUTES.

    python manage.py cancel_stale_orders
    python manage.py cancel_stale_orders --minutes 60 --dry-run

There is no Celery in this stack (spec section 2), so run it from Windows
Task Scheduler or cron.
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from orders.admin_api import apply_status_change
from orders.constants import OrderStatus
from orders.models import Order


class Command(BaseCommand):
    help = "Cancel GCash orders stuck in pending_payment past the timeout."

    def add_arguments(self, parser):
        parser.add_argument(
            "--minutes",
            type=int,
            default=None,
            help=(
                "Age in minutes before an order is reaped. Defaults to "
                "settings.PENDING_PAYMENT_TIMEOUT_MINUTES."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be cancelled without writing anything.",
        )

    def handle(self, *args, **options):
        minutes = options["minutes"]
        if minutes is None:
            minutes = settings.PENDING_PAYMENT_TIMEOUT_MINUTES
        if minutes < 1:
            raise CommandError("--minutes must be at least 1.")

        cutoff = timezone.now() - timedelta(minutes=minutes)
        reason = f"Payment not completed within {minutes} minutes."

        stale = Order.objects.filter(
            status=OrderStatus.PENDING_PAYMENT, placed_at__lt=cutoff
        ).order_by("placed_at")

        if not stale.exists():
            self.stdout.write(
                f"No orders have been in pending_payment for over {minutes} minutes."
            )
            return

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(f"Dry run — {stale.count()} order(s) would cancel:")
            )
            for order in stale:
                age = int((timezone.now() - order.placed_at).total_seconds() // 60)
                self.stdout.write(f"  {order.order_number}  placed {age} min ago")
            return

        cancelled = 0
        for order in stale:
            with transaction.atomic():
                # Re-read under lock: the webhook may have just paid for it.
                fresh = (
                    Order.objects.select_for_update()
                    .filter(pk=order.pk, status=OrderStatus.PENDING_PAYMENT)
                    .first()
                )
                if fresh is None:
                    continue

                # changed_by stays null — this was the system, not a person.
                apply_status_change(
                    fresh,
                    OrderStatus.CANCELLED,
                    changed_by=None,
                    reason=reason,
                )
                cancelled += 1
                self.stdout.write(f"  cancelled {fresh.order_number}")

        self.stdout.write(
            self.style.SUCCESS(f"Cancelled {cancelled} stale pending_payment order(s).")
        )
