"""
App update API.

GET  /api/app-version/<app>/      — public. Each app asks this on launch and
                                    offers an update when version_code is
                                    higher than its own build number.
POST /api/app-releases/publish/   — for the GitHub workflow in the mobile
                                    repo, after it attaches a new APK to a
                                    GitHub release. Authenticated by the
                                    shared secret APP_RELEASE_PUBLISH_TOKEN.
"""

import hmac
import logging

from django.conf import settings
from django.urls import reverse
from rest_framework import serializers, status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .models import AppKind, AppRelease

logger = logging.getLogger(__name__)


def release_payload(request, release):
    if release.download_url:
        url = release.download_url
    else:
        url = request.build_absolute_uri(reverse("website:download", args=[release.app]))
    return {
        "app": release.app,
        "version": release.version,
        "version_code": release.version_code,
        "download_url": url,
        "size": release.size or None,
        "sha256": release.sha256 or None,
        "notes": release.notes,
        "published_at": release.uploaded_at,
    }


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def app_version(request, app):
    if app not in AppKind.values:
        return Response({"error": f"Unknown app '{app}'."}, status=status.HTTP_404_NOT_FOUND)
    release = AppRelease.ready().filter(app=app).first()
    if release is None:
        return Response(
            {"error": "No version of this app has been published yet."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(release_payload(request, release))


class PublishSerializer(serializers.Serializer):
    app = serializers.ChoiceField(choices=AppKind.choices)
    version = serializers.RegexField(r"^[0-9A-Za-z.\-]{1,30}$", help_text='"1.0.3"')
    version_code = serializers.IntegerField(min_value=1, max_value=2_100_000_000)
    download_url = serializers.URLField(max_length=500)
    size = serializers.IntegerField(min_value=0, required=False, default=0)
    sha256 = serializers.RegexField(r"^[0-9a-f]{64}$", required=False, allow_blank=True, default="")
    notes = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")

    def validate_download_url(self, value):
        # Even with the token, a release can only point at the mobile repo's
        # own GitHub releases: never at an arbitrary site.
        prefix = settings.APP_RELEASE_URL_PREFIX
        if not value.startswith(prefix) or not value.lower().endswith(".apk"):
            raise serializers.ValidationError(f"Must be an .apk under {prefix}")
        return value


def _check_token(request):
    expected = settings.APP_RELEASE_PUBLISH_TOKEN
    if not expected:
        raise AuthenticationFailed("Publishing is turned off: APP_RELEASE_PUBLISH_TOKEN is not set.")
    header = request.headers.get("Authorization", "")
    given = header[len("Bearer ") :] if header.startswith("Bearer ") else ""
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise AuthenticationFailed("Wrong or missing publish token.")


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def publish_release(request):
    _check_token(request)
    serializer = PublishSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    release = AppRelease.objects.filter(app=data["app"]).first() or AppRelease(app=data["app"])
    if release.pk and data["version_code"] < release.version_code:
        return Response(
            {
                "error": (
                    f"Version code {data['version_code']} is older than the published "
                    f"{release.version_code}. Bump the build number in pubspec.yaml."
                )
            },
            status=status.HTTP_409_CONFLICT,
        )

    release.publish_hosted(
        version=data["version"],
        version_code=data["version_code"],
        download_url=data["download_url"],
        size=data["size"],
        sha256=data["sha256"],
        notes=data["notes"],
        by="github-actions",
    )
    logger.info(
        "Published %s %s (%s) from %s.",
        release.app, release.version, release.version_code, release.download_url,
    )
    return Response(release_payload(request, release), status=status.HTTP_200_OK)
