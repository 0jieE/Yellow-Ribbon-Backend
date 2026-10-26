"""Custom pages inside the admin that are not tied to a model."""

import logging

from django.contrib import admin, messages
from django.contrib.auth import logout
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.views.decorators.http import require_http_methods

from . import backup
from .backup import BackupError, backup_filename, build_backup, record_counts

CONFIRM_WORD = "RESTORE"

logger = logging.getLogger(__name__)


@require_http_methods(["GET", "POST"])
def backup_view(request):
    """
    GET  shows what a backup covers and how to restore one.
    POST sends the backup as a download.

    Superusers only: the file holds every customer's address and phone number
    and every account's password hash. The download is a POST so a link
    prefetcher or a stray <img> on another site cannot trigger it.
    """
    if not request.user.is_superuser:
        raise PermissionDenied

    if request.method == "POST":
        data = build_backup()
        logger.info(
            "Database backup downloaded by %s (%s bytes).", request.user.username, len(data)
        )
        response = HttpResponse(data, content_type="application/gzip")
        response["Content-Disposition"] = f'attachment; filename="{backup_filename()}"'
        return response

    counts = record_counts()
    context = {
        **admin.site.each_context(request),
        "title": "Database backup",
        "counts_table": {
            "headers": ["Table", "Rows"],
            "rows": [[name, count] for name, count in counts],
        },
        "total_rows": sum(count for _, count in counts),
        "confirm_word": CONFIRM_WORD,
        "max_upload_mb": backup.MAX_UPLOAD_BYTES // (1024 * 1024),
    }
    return TemplateResponse(request, "admin/backup.html", context)


@require_http_methods(["POST"])
def restore_view(request):
    """
    POST — replace everything with an uploaded backup.

    The most destructive thing in the admin, so: superusers only, a typed
    confirmation, and the wipe and the load are one transaction (a bad file
    changes nothing). On success every session is gone, including this one, so
    the answer is a standalone page rather than a redirect into the admin.
    """
    if not request.user.is_superuser:
        raise PermissionDenied

    def refuse(text):
        messages.error(request, text)
        return redirect("backup")

    if (request.POST.get("confirm") or "").strip() != CONFIRM_WORD:
        return refuse(f"Nothing was restored. Type {CONFIRM_WORD} in the box to confirm.")

    upload = request.FILES.get("backup")
    if upload is None:
        return refuse("Nothing was restored. Choose the backup file to upload.")
    if upload.size > backup.MAX_UPLOAD_BYTES:
        return refuse(
            f"Nothing was restored. That file is over {backup.MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    who = request.user.username
    try:
        rows, _ = backup.restore_uploaded_backup(upload.read())
    except BackupError as exc:
        logger.warning("Restore by %s refused: %s", who, exc)
        return refuse(str(exc))

    logger.warning("Database restored from an uploaded backup by %s (%s rows).", who, rows)

    # The restore deleted every session, this one included. Drop the cookie too.
    logout(request)
    context = {
        **admin.site.each_context(request),
        "title": "Backup restored",
        "rows": rows,
        "login_url": "/admin/login/",
    }
    return TemplateResponse(request, "admin/restore_done.html", context)
