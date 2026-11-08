from django import forms
from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from unfold.widgets import UnfoldAdminFileFieldWidget

from .models import ApkError, AppRelease, validate_apk


class AppReleaseForm(forms.ModelForm):
    apk = forms.FileField(
        label="APK file",
        required=False,
        widget=UnfoldAdminFileFieldWidget(attrs={"accept": ".apk"}),
        help_text=(
            "The app-release.apk from build/app/outputs/flutter-apk/. Uploading "
            "replaces the file people download. Large files take a minute to upload."
        ),
    )

    class Meta:
        model = AppRelease
        fields = ["app", "version"]

    def clean_apk(self):
        upload = self.cleaned_data.get("apk")
        if upload is None:
            if self.instance.pk is None or not self.instance.is_ready:
                raise forms.ValidationError("Choose the APK file to upload.")
            return None
        try:
            validate_apk(upload)
        except ApkError as exc:
            raise forms.ValidationError(str(exc))
        return upload


@admin.register(AppRelease)
class AppReleaseAdmin(ModelAdmin):
    form = AppReleaseForm
    list_display = ("app", "version", "size_display", "uploaded_at", "uploaded_by", "download_link")
    list_filter = ("app",)
    search_fields = ("version",)
    readonly_fields = ("size_display", "sha256", "uploaded_by", "uploaded_at", "download_link")
    fieldsets = (
        (
            None,
            {
                "fields": ("app", "version", "apk"),
                "description": (
                    "The customer app can be downloaded by anyone from the website. "
                    "The rider app needs a rider account to sign in first, and the "
                    "admin app a store admin account."
                ),
            },
        ),
        ("Current file", {"fields": ("size_display", "sha256", "uploaded_by", "uploaded_at", "download_link")}),
    )

    def get_readonly_fields(self, request, obj=None):
        # Changing which app a row is would quietly swap two downloads.
        if obj is not None:
            return ("app", *self.readonly_fields)
        return self.readonly_fields

    def save_model(self, request, obj, form, change):
        upload = form.cleaned_data.get("apk")
        obj.uploaded_by = request.user.username if upload else obj.uploaded_by
        obj.save()
        if upload is not None:
            obj.replace_file(upload)
            self.message_user(
                request,
                f"Uploaded {obj.download_name} ({obj.size_mb} MB). It is on the website now.",
                messages.SUCCESS,
            )

    @admin.display(description="size")
    def size_display(self, obj):
        return f"{obj.size_mb} MB" if obj.is_ready else "No file yet"

    @admin.display(description="download")
    def download_link(self, obj):
        if not obj.pk or not obj.is_ready:
            return "—"
        return format_html(
            '<a href="{}" class="text-primary-600">{}</a>',
            reverse("website:download", args=[obj.app]),
            obj.download_name,
        )
