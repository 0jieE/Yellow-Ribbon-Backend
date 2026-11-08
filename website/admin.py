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
            "Only if the app is not built by GitHub: the app-release.apk from "
            "build/app/outputs/flutter-apk/. Uploading replaces the file people "
            "download (and clears the download link). Large files take a minute."
        ),
    )

    class Meta:
        model = AppRelease
        fields = ["app", "version", "version_code", "download_url", "notes"]

    def clean_apk(self):
        upload = self.cleaned_data.get("apk")
        if upload is None:
            return None
        try:
            validate_apk(upload)
        except ApkError as exc:
            raise forms.ValidationError(str(exc))
        return upload

    def clean(self):
        cleaned = super().clean()
        has_upload = cleaned.get("apk") is not None
        has_link = bool(cleaned.get("download_url"))
        had_file = self.instance.pk is not None and self.instance.has_file
        if not (has_upload or has_link or had_file):
            raise forms.ValidationError(
                "Give a download link (the GitHub release's APK) or upload the APK file."
            )
        return cleaned


@admin.register(AppRelease)
class AppReleaseAdmin(ModelAdmin):
    form = AppReleaseForm
    list_display = ("app", "version", "version_code", "source", "uploaded_at", "uploaded_by", "download_link")
    list_filter = ("app",)
    search_fields = ("version",)
    readonly_fields = ("source", "size_display", "sha256", "uploaded_by", "uploaded_at", "download_link")
    fieldsets = (
        (
            None,
            {
                "fields": ("app", "version", "version_code", "download_url", "notes", "apk"),
                "description": (
                    "Normally filled in by GitHub: each release built in the mobile "
                    "repo is announced here, and the apps offer it as an update. "
                    "The customer app can be downloaded by anyone from the website; "
                    "the rider app needs a rider account to sign in first, and the "
                    "admin app a store admin account."
                ),
            },
        ),
        ("Current file", {"fields": ("source", "size_display", "sha256", "uploaded_by", "uploaded_at", "download_link")}),
    )

    def get_readonly_fields(self, request, obj=None):
        # Changing which app a row is would quietly swap two downloads.
        if obj is not None:
            return ("app", *self.readonly_fields)
        return self.readonly_fields

    def save_model(self, request, obj, form, change):
        upload = form.cleaned_data.get("apk")
        if upload or "download_url" in form.changed_data or "version" in form.changed_data:
            obj.uploaded_by = request.user.username
        if obj.pk and obj.download_url and "download_url" in form.changed_data:
            # Now hosted elsewhere: the uploaded copy would never be served again.
            obj.chunks.all().delete()
            obj.size, obj.sha256 = 0, ""
        obj.save()
        if upload is not None:
            obj.replace_file(upload)
            self.message_user(
                request,
                f"Uploaded {obj.download_name} ({obj.size_mb} MB). It is on the website now.",
                messages.SUCCESS,
            )

    @admin.display(description="served from")
    def source(self, obj):
        if obj.download_url:
            return "GitHub" if "github.com/" in obj.download_url else "Link"
        return f"Uploaded file, {obj.size_mb} MB" if obj.size else "Nothing yet"

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
