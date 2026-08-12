"""Mounted at /api/menu/ by config.urls."""

from rest_framework.routers import DefaultRouter

from .views import MenuViewSet

app_name = "menu"

router = DefaultRouter()
router.register(r"", MenuViewSet, basename="menu")

urlpatterns = router.urls
