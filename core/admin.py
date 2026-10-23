"""
Django admin branding.

Section 13 makes the admin the demo fallback if a Flutter screen misbehaves
during the defense, so it is worth it not looking like a stock install.
"""

from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse
from unfold.admin import ModelAdmin

from .models import StoreSettings

admin.site.site_header = "Yellow Ribbon Pizza"
admin.site.site_title = "Yellow Ribbon Pizza"
admin.site.index_title = "Delivery system administration"



@admin.register(StoreSettings)
class StoreSettingsAdmin(ModelAdmin):
    """One row, edited in place. No list, no add, no delete."""

    # Read by the admin sweep tests: a one-row model has no list to search or filter.
    is_singleton = True

    readonly_fields = ("updated_at",)
    fieldsets = (
        (
            "Delivery fee",
            {
                "fields": ("delivery_fee", "delivery_fee_per_km"),
                "description": (
                    "Leave the per-km rate at 0 for a flat fee. Orders already "
                    "placed keep the fee they were placed with."
                ),
            },
        ),
        (
            "Service area",
            {
                "fields": (
                    "service_area_city",
                    "service_area_center_lat",
                    "service_area_center_lng",
                    "service_area_radius_km",
                )
            },
        ),
        (
            "Payment gateway fees",
            {
                "fields": (
                    "paymongo_fee_percent",
                    "paymongo_fee_fixed",
                    "maya_fee_percent",
                    "maya_fee_fixed",
                ),
                "description": (
                    "Used by the payments report to work out what the store "
                    "keeps. Set these to the rates on your own contracts."
                ),
            },
        ),
        (None, {"fields": ("updated_at",)}),
    )

    def changelist_view(self, request, extra_context=None):
        # There is only one row, so skip the list and open it.
        store = StoreSettings.load()
        return HttpResponseRedirect(
            reverse("admin:core_storesettings_change", args=[store.pk])
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
