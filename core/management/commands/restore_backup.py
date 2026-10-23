"""
Load a backup made from the admin's Backup page into the current database.

    python manage.py restore_backup yellowribbon-backup-20261023-1010.json.gz
    python manage.py restore_backup backup.json.gz --flush

Render's free web service has no shell, so run this from your own machine with
DATABASE_URL pointing at the new database's External URL (see DEPLOYMENT.md).
The database must be migrated first.
"""

from django.core.management.base import BaseCommand, CommandError

from core.backup import BackupError, restore_backup


class Command(BaseCommand):
    help = "Restore a database backup (.json.gz or .json) into an empty, migrated database."

    def add_arguments(self, parser):
        parser.add_argument("path", help="The backup file.")
        parser.add_argument(
            "--flush",
            action="store_true",
            help=(
                "Delete everything in the database first. Use it when the new "
                "database already holds an admin you created by hand."
            ),
        )

    def handle(self, *args, **options):
        try:
            count, sequences = restore_backup(options["path"], flush=options["flush"])
        except BackupError as exc:
            raise CommandError(str(exc))

        self.stdout.write(self.style.SUCCESS(f"Restored {count} rows."))
        for sequence, highest in sequences.items():
            self.stdout.write(f"  {sequence}: next number is {highest + 1}")
