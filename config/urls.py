"""
Root URL configuration.

App routes are mounted here as each build step lands. Everything the
Flutter apps consume lives under /api/.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from accounts import views as account_views
from orders import views as order_views


def health(_request):
    """Cheap liveness probe — confirms the server and settings loaded."""
    return JsonResponse({"status": "ok", "service": "yellowribbon-backend"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", health, name="health"),
    path("api/auth/", include("accounts.urls")),
    path("api/menu/", include("menu.urls")),
    path("api/orders/", include("orders.urls")),
    path("api/constants/", order_views.constants, name="constants"),
    path("api/device-tokens/", account_views.DeviceTokenView.as_view(), name="device-tokens"),
    path("api/admin/", include("config.admin_api_urls")),
    path("api/rider/", include("delivery.urls")),
    path("api/payments/", include("payments.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
