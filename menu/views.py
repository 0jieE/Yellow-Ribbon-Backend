"""Menu endpoints (spec section 7)."""

from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from core.permissions import IsAdminUser

from .models import Menu
from .serializers import MenuSerializer


class MenuViewSet(viewsets.ModelViewSet):
    """
    GET    /api/menu/        all roles; customers see available items only
    GET    /api/menu/{id}/   same visibility rule
    POST   /api/menu/        admin
    PATCH  /api/menu/{id}/   admin
    DELETE /api/menu/{id}/   admin
    """

    serializer_class = MenuSerializer
    # No PUT — the spec only defines PATCH for updates.
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsAdminUser()]

    def get_queryset(self):
        """
        Visibility is enforced here, not in the serializer, so an
        unavailable item is invisible to customers on detail as well as list.
        """
        queryset = Menu.objects.all()
        user = self.request.user
        if user.is_admin or user.is_rider:
            return queryset
        return queryset.filter(is_available=True)
