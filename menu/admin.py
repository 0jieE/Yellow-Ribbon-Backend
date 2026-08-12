from django.contrib import admin
from django.utils.html import format_html

from .models import Menu


@admin.register(Menu)
class MenuAdmin(admin.ModelAdmin):
    list_display = ("name", "price", "is_available", "thumbnail", "updated_at")
    list_editable = ("price", "is_available")
    list_filter = ("is_available", "created_at")
    search_fields = ("name", "description")
    ordering = ("name",)
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "updated_at", "preview")

    fieldsets = (
        (None, {"fields": ("name", "description", "price", "is_available")}),
        ("Photo", {"fields": ("photo", "preview")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="photo")
    def thumbnail(self, obj):
        if not obj.photo:
            return "—"
        return format_html('<img src="{}" style="height:40px;" />', obj.photo.url)

    @admin.display(description="preview")
    def preview(self, obj):
        if not obj.photo:
            return "No photo uploaded."
        return format_html('<img src="{}" style="max-height:200px;" />', obj.photo.url)
