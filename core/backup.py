"""
Database backup and restore.

Render's free Postgres is deleted after a month, so the data has to be
exportable and re-importable into a brand-new database. The backup is Django's
own JSON fixture, gzipped: it is independent of the Postgres version and of
Render, and `restore_backup` loads it into any database that has been
migrated with the same code.

What is NOT in it: uploaded photos. With Cloudinary configured they live
there and survive on their own; the backup holds only their names.

What IS in it: every row of every table, including password hashes and push
tokens. Treat the file like a credential.
"""

import gzip
import io
import json
import logging
import os
import tempfile
from itertools import takewhile

from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from orders.models import ORDER_NUMBER_SEQUENCE, Order
from payments.models import INVOICE_NUMBER_SEQUENCE, Invoice

logger = logging.getLogger(__name__)

# Recreated by `migrate`, so restoring them would only fight the fresh copies.
# LogEntry is admin click history, not business data.
EXCLUDED = ["contenttypes", "auth.Permission", "sessions", "admin.LogEntry"]
_EXCLUDED_LABELS = {
    "contenttypes.contenttype",
    "auth.permission",
    "sessions.session",
    "admin.logentry",
}

# (sequence, model, column) — numbers handed out by a Postgres sequence, which
# loaddata does not know about and so leaves at 1.
NUMBER_SEQUENCES = [
    (ORDER_NUMBER_SEQUENCE, Order, "order_number"),
    (INVOICE_NUMBER_SEQUENCE, Invoice, "invoice_number"),
]


class BackupError(Exception):
    """A backup file that cannot be read, or a restore that cannot proceed."""


def backup_filename():
    return f"yellowribbon-backup-{timezone.localtime():%Y%m%d-%H%M}.json.gz"


def _backed_up_models():
    return [
        model
        for model in apps.get_models()
        if model._meta.label_lower not in _EXCLUDED_LABELS
    ]


def record_counts():
    """[(model name, row count)] for every table the backup covers."""
    rows = [
        (str(model._meta.verbose_name_plural).capitalize(), model.objects.count())
        for model in _backed_up_models()
    ]
    return sorted(rows)


def build_backup():
    """The whole database as gzipped JSON bytes."""
    buffer = io.StringIO()
    call_command(
        "dumpdata",
        format="json",
        # Permissions and content types get new ids in a new database, so
        # references to them are written by name, not by number.
        use_natural_foreign_keys=True,
        exclude=EXCLUDED,
        stdout=buffer,
    )
    return gzip.compress(buffer.getvalue().encode("utf-8"))


def database_is_empty():
    return not any(model.objects.exists() for model in _backed_up_models())


def _trailing_number(value):
    """43 from 'YR-0043'; 0 when the text does not end in digits."""
    digits = "".join(reversed(list(takewhile(str.isdigit, reversed(value)))))
    return int(digits) if digits else 0


def reset_number_sequences():
    """
    Point the order and invoice number sequences past the restored rows.

    Without this the next order would be YR-0001 again and collide with a
    restored one. Returns {sequence: last number in use}.
    """
    result = {}
    for sequence, model, column in NUMBER_SEQUENCES:
        highest = max(
            (_trailing_number(v) for v in model.objects.values_list(column, flat=True)),
            default=0,
        )
        with connection.cursor() as cursor:
            if highest:
                cursor.execute("SELECT setval(%s, %s, true)", [sequence, highest])
            else:
                cursor.execute("SELECT setval(%s, 1, false)", [sequence])
        result[sequence] = highest
    return result


def read_backup(path):
    """The fixture text from a .json.gz or plain .json backup file."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise BackupError(f"Cannot read {path}: {exc}")

    try:
        raw = gzip.decompress(raw)
    except (OSError, EOFError):
        pass  # not gzipped: assume plain JSON

    try:
        text = raw.decode("utf-8")
        rows = json.loads(text)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BackupError(f"{path} is not a Yellow Ribbon backup: {exc}")

    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and "model" in row for row in rows
    ):
        raise BackupError(f"{path} is not a Django data dump.")
    return text, len(rows)


def restore_backup(path, flush=False):
    """
    Load a backup into the current database.

    The database must already be migrated. It must also be empty, unless
    `flush` is set, which wipes it first. Returns (rows loaded, sequences).
    """
    text, count = read_backup(path)

    if not database_is_empty():
        if not flush:
            raise BackupError(
                "The database already has data. Restore into a fresh database, "
                "or pass --flush to wipe it first."
            )
        call_command("flush", interactive=False, verbosity=0)

    # loaddata wants a file whose name ends .json.
    handle, tmp_path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            tmp.write(text)
        call_command("loaddata", tmp_path, verbosity=0)
    finally:
        os.remove(tmp_path)

    sequences = reset_number_sequences()
    logger.info("Restored %s rows from %s.", count, path)
    return count, sequences
