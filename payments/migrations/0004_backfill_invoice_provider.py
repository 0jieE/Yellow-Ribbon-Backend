"""
Fill in `provider` and `net_amount` on invoices written before Maya and the
payments report existed.

The provider follows the order's payment type. The gateway fee for those old
payments was never recorded and cannot be recovered, so it stays 0 and the net
equals the amount paid: the report understates fees for that period rather
than inventing a figure.
"""

from django.db import migrations
from django.db.models import F

PROVIDER_BY_PAYMENT_TYPE = {"gcash": "paymongo", "maya": "maya", "cod": "cod"}


def backfill(apps, schema_editor):
    Invoice = apps.get_model("payments", "Invoice")

    for payment_type, provider in PROVIDER_BY_PAYMENT_TYPE.items():
        Invoice.objects.filter(provider="", order__payment_type=payment_type).update(
            provider=provider
        )

    # Only settled invoices have an amount worth netting.
    Invoice.objects.filter(paid_at__isnull=False, net_amount=0).update(
        net_amount=F("amount_paid") - F("fee")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0003_maya_and_refunds"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
