"""Small helpers shared by the model admins."""

import csv

from django.contrib import admin
from django.http import HttpResponse
from django.utils import timezone

BYTE_ORDER_MARK = chr(0xFEFF)
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value):
    """
    Neutralise spreadsheet formulas. Addresses and names are typed by
    customers, and a cell starting with = + - @ runs as a formula when the
    file is opened in Excel.
    """
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def csv_export_action(basename, columns):
    """
    Build an "Export selected to CSV" admin action.

    `columns` is a list of (header, getter) pairs; each getter takes the
    model instance. Rows are written in the order the changelist shows them.
    """

    @admin.action(description="Export selected to CSV", permissions=["view"])
    def export_selected(modeladmin, request, queryset):
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{basename}-{stamp}.csv"'
        # Excel opens a UTF-8 CSV as ANSI unless it sees a BOM, which turns
        # the peso sign and Filipino names into mojibake.
        response.write(BYTE_ORDER_MARK)
        writer = csv.writer(response)
        writer.writerow([header for header, _ in columns])
        for obj in queryset:
            writer.writerow([_safe_cell(getter(obj)) for _, getter in columns])
        return response

    return export_selected
