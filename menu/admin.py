from django.contrib import admin, messages
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from unfold.decorators import action

from .models import Menu


@admin.register(Menu)
class MenuAdmin(ModelAdmin):
    list_display = ("name", "price", "is_available", "thumbnail", "updated_at")
    list_editable = ("price", "is_available")
    list_filter = ("is_available", "created_at")
    search_fields = ("name", "description")
    ordering = ("name",)
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "updated_at", "preview")
    actions = ["mark_available", "mark_unavailable"]

    fieldsets = (
        (None, {"fields": ("name", "description", "price", "is_available")}),
        ("Photo", {"fields": ("photo", "preview")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    @action(description="Mark selected items available", permissions=["change"])
    def mark_available(self, request, queryset):
        count = queryset.filter(is_available=False).update(is_available=True)
        self.message_user(request, f"{count} item(s) back on the menu.", messages.SUCCESS)

    @action(description="Mark selected items unavailable", permissions=["change"])
    def mark_unavailable(self, request, queryset):
        count = queryset.filter(is_available=True).update(is_available=False)
        self.message_user(request, f"{count} item(s) hidden from customers.", messages.SUCCESS)

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
