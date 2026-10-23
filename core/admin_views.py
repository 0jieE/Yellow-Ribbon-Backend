"""Custom pages inside the admin that are not tied to a model."""

import logging

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.template.response import TemplateResponse
from django.views.decorators.http import require_http_methods

from .backup import backup_filename, build_backup, record_counts

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
    }
    return TemplateResponse(request, "admin/backup.html", context)
