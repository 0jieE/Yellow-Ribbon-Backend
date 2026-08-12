"""
Postgres sequence backing `INV-0001` invoice numbers.

Same reasoning as the order-number sequence: nextval is atomic, so two
webhook deliveries landing at once cannot be handed the same number.
"""

from django.db import migrations

from payments.models import INVOICE_NUMBER_SEQUENCE


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=f"CREATE SEQUENCE IF NOT EXISTS {INVOICE_NUMBER_SEQUENCE} START WITH 1;",
            reverse_sql=f"DROP SEQUENCE IF EXISTS {INVOICE_NUMBER_SEQUENCE};",
        ),
    ]
