"""
The public website: the landing page, the staff sign-in, and app downloads.

The customer app is free for anyone to download. Releases built by GitHub
redirect to the GitHub file; an APK uploaded by hand streams from here. The rider app (and the
admin app) only download for someone signed in with that kind of account.
"""

import logging
import re

from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.models import StoreSettings
from menu.models import Menu

from .models import AppKind, AppRelease, can_download

logger = logging.getLogger(__name__)

APK_CONTENT_TYPE = "application/vnd.android.package-archive"
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _releases():
    return {r.app: r for r in AppRelease.ready()}


@require_GET
def home(request):
    store = StoreSettings.load()
    menu = list(Menu.objects.order_by("-is_available", "name"))
    context = {
        "store": store,
        "about_paragraphs": [p.strip() for p in store.about.split("\n\n") if p.strip()],
        "menu": menu,
        "customer_app": _releases().get(AppKind.CUSTOMER),
    }
    return render(request, "website/home.html", context)


def _staff_apps(user):
    """The gated apps this user may download, in display order."""
    releases = _releases()
    apps = []
    for kind in (AppKind.RIDER, AppKind.ADMIN):
        if can_download(user, kind):
            apps.append({"kind": kind, "release": releases.get(kind)})
    return apps


@require_http_methods(["GET", "POST"])
def staff_apps(request):
    """
    Sign in to download the rider app. Only rider and store admin accounts
    are let in: a customer account has nothing to download here.
    """
    form = AuthenticationForm(request, data=request.POST or None)
    form.fields["username"].widget.attrs.update({"autocomplete": "username", "autocapitalize": "none"})
    form.fields["password"].widget.attrs.update({"autocomplete": "current-password"})
    refused = None

    if request.method == "POST":
        if form.is_valid():
            user = form.get_user()
            if _staff_apps(user):
                login(request, user)
                logger.info("%s signed in on the website to download an app.", user.username)
                return redirect("website:staff-apps")
            refused = (
                "That is a customer account. Customers can download the Yellow Ribbon "
                "app from the home page without signing in."
            )

    user = request.user
    apps = _staff_apps(user) if user.is_authenticated else []
    return render(
        request,
        "website/staff_apps.html",
        {"form": form, "apps": apps, "refused": refused},
    )


@require_POST
def sign_out(request):
    logout(request)
    return redirect("website:staff-apps")


def _parse_range(header, size):
    """(start, end) for a single satisfiable bytes range, or None to send it all."""
    match = _RANGE.match(header.strip()) if header else None
    if not match or not any(match.groups()):
        return None
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    else:  # "bytes=-500": the last 500 bytes
        start, end = max(size - int(last), 0), size - 1
    if start > end or start >= size:
        raise ValueError("unsatisfiable")
    return start, end


@require_GET
def download(request, app):
    if app not in AppKind.values:
        raise Http404
    if not can_download(request.user, app):
        return redirect("website:staff-apps")

    release = AppRelease.ready().filter(app=app).first()
    if release is None:
        raise Http404("This app is not available for download yet.")
    if release.download_url:
        # Built and hosted by GitHub; this URL stays the same across versions.
        logger.info("Download of %s %s sent to GitHub.", app, release.version)
        return redirect(release.download_url)

    size = release.size
    try:
        byte_range = _parse_range(request.headers.get("Range"), size)
    except ValueError:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{size}"
        return response

    start, end = byte_range or (0, size - 1)
    response = StreamingHttpResponse(release.iter_bytes(start, end), content_type=APK_CONTENT_TYPE)
    if byte_range:
        response.status_code = 206
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["Content-Length"] = str(end - start + 1)
    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = f'attachment; filename="{release.download_name}"'
    # The same URL serves the next version once it is uploaded.
    response["Cache-Control"] = "private, no-cache" if app != AppKind.CUSTOMER else "no-cache"
    if start == 0:
        logger.info("Download of %s %s started.", app, release.version)
    return response
