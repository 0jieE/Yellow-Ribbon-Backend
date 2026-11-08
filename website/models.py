"""
The Android apps offered for download on the public website.

Render's free web service has no disk that survives a deploy, and Cloudinary's
free plan refuses files over 10 MB, so an APK is kept in Postgres: split into
1 MB chunks, so a download streams one chunk at a time instead of holding a
50 MB file in the server's memory.

These rows are not in the database backup (they would make it 100+ MB). A
restore leaves them alone; a brand-new database needs the APKs uploaded again.
"""

import hashlib

from django.db import models, transaction

CHUNK_SIZE = 1024 * 1024
MAX_APK_BYTES = 150 * 1024 * 1024
# Every APK is a zip file, and every zip starts with these four bytes.
ZIP_MAGIC = b"PK\x03\x04"


class AppKind(models.TextChoices):
    CUSTOMER = "customer", "Yellow Ribbon (customer app)"
    RIDER = "rider", "Ribbon Rider (rider app)"
    ADMIN = "admin", "Ribbon Admin (store admin app)"


# The name the phone saves the file as.
DOWNLOAD_NAMES = {
    AppKind.CUSTOMER: "YellowRibbon",
    AppKind.RIDER: "RibbonRider",
    AppKind.ADMIN: "RibbonAdmin",
}


class ApkError(ValueError):
    """An upload that is not an APK we can offer."""


def validate_apk(upload):
    """Raise ApkError unless `upload` looks like an APK of a sensible size."""
    if upload.size > MAX_APK_BYTES:
        raise ApkError(f"That file is over {MAX_APK_BYTES // (1024 * 1024)} MB.")
    upload.seek(0)
    head = upload.read(len(ZIP_MAGIC))
    upload.seek(0)
    if head != ZIP_MAGIC:
        raise ApkError("That is not an APK file. Upload the app-release.apk that Flutter built.")


def can_download(user, app):
    """Whether `user` may download `app`. The customer app is public."""
    if app == AppKind.CUSTOMER:
        return True
    if not (user and user.is_authenticated and user.is_active):
        return False
    is_store_admin = user.is_admin or user.is_superuser
    if app == AppKind.RIDER:
        return user.is_rider or is_store_admin
    return is_store_admin


class AppRelease(models.Model):
    """The current APK of one app. Uploading a new one replaces it."""

    app = models.CharField(max_length=20, choices=AppKind.choices, unique=True)
    version = models.CharField(
        max_length=30, help_text='Shown next to the download button, e.g. "1.0.3".'
    )
    size = models.PositiveBigIntegerField(default=0, editable=False)
    sha256 = models.CharField(max_length=64, blank=True, editable=False)
    uploaded_by = models.CharField(max_length=150, blank=True, editable=False)
    uploaded_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "app_releases"
        ordering = ["app"]
        verbose_name = "app download"
        verbose_name_plural = "app downloads"

    def __str__(self):
        return f"{self.get_app_display()} {self.version}"

    @property
    def download_name(self):
        return f"{DOWNLOAD_NAMES[self.app]}-{self.version}.apk"

    @property
    def size_mb(self):
        return round(self.size / (1024 * 1024), 1)

    @property
    def is_ready(self):
        return self.size > 0

    def replace_file(self, upload):
        """
        Store `upload` (a Django UploadedFile) as this app's APK.

        All or nothing: the old chunks are deleted and the new ones written in
        one transaction, so a failed upload leaves the previous APK in place.
        """
        validate_apk(upload)

        digest = hashlib.sha256()
        size = 0
        with transaction.atomic():
            if self.pk is None:
                self.save()
            self.chunks.all().delete()
            # read() rather than upload.chunks(): a small upload held in memory
            # comes back from chunks() as one piece whatever size is asked
            # for, and iter_bytes relies on every chunk but the last being
            # exactly CHUNK_SIZE.
            upload.seek(0)
            for index, data in enumerate(iter(lambda: upload.read(CHUNK_SIZE), b"")):
                digest.update(data)
                size += len(data)
                AppReleaseChunk.objects.create(release=self, index=index, data=data)
            self.size = size
            self.sha256 = digest.hexdigest()
            self.save()

    def iter_bytes(self, start=0, end=None):
        """Yield bytes start..end (inclusive), reading one chunk at a time."""
        end = self.size - 1 if end is None else end
        first, last = start // CHUNK_SIZE, end // CHUNK_SIZE
        for index in range(first, last + 1):
            data = bytes(
                AppReleaseChunk.objects.filter(release=self, index=index)
                .values_list("data", flat=True)
                .get()
            )
            chunk_start = index * CHUNK_SIZE
            yield data[max(start - chunk_start, 0) : end - chunk_start + 1]


class AppReleaseChunk(models.Model):
    """One CHUNK_SIZE slice of an APK."""

    release = models.ForeignKey(AppRelease, on_delete=models.CASCADE, related_name="chunks")
    index = models.PositiveIntegerField()
    data = models.BinaryField()

    class Meta:
        db_table = "app_release_chunks"
        constraints = [
            models.UniqueConstraint(fields=["release", "index"], name="uniq_app_release_chunk")
        ]

    def __str__(self):
        return f"{self.release} part {self.index}"
