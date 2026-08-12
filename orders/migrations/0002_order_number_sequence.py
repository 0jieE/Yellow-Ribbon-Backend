"""
Postgres sequence backing `YR-0001` order numbers.

A sequence rather than MAX+1 so concurrent checkouts cannot be handed the
same number. nextval is atomic and is not rolled back, so a failed
placement burns a number rather than reusing one — gaps are acceptable,
duplicates are not.
"""

from django.db import migrations

from orders.models import ORDER_NUMBER_SEQUENCE


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=f"CREATE SEQUENCE IF NOT EXISTS {ORDER_NUMBER_SEQUENCE} START WITH 1;",
            reverse_sql=f"DROP SEQUENCE IF EXISTS {ORDER_NUMBER_SEQUENCE};",
        ),
    ]
