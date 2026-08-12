"""Auth endpoints (spec section 7)."""

from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .models import DeviceToken, User
from .serializers import (
    DeviceTokenDeleteSerializer,
    DeviceTokenSerializer,
    LoginSerializer,
    MeSerializer,
    RegisterSerializer,
    UserSerializer,
)


class RegisterView(generics.CreateAPIView):
    """POST /api/auth/register/ — customer self-registration."""

    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class LoginView(TokenObtainPairView):
    """POST /api/auth/login/ — access + refresh + role flags."""

    serializer_class = LoginSerializer
    permission_classes = [AllowAny]


class RefreshView(TokenRefreshView):
    """POST /api/auth/refresh/."""

    permission_classes = [AllowAny]


class LogoutView(APIView):
    """
    POST /api/auth/logout/.

    Deletes the caller's device token so the handset stops receiving their
    notifications. Send `fcm_token` to drop just this device; omit it to
    drop every device on the account.

    Note this does not invalidate the JWT itself — it stays valid until it
    expires. Clients should discard their stored tokens on receiving 204.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        tokens = DeviceToken.objects.filter(user=request.user)
        fcm_token = (request.data or {}).get("fcm_token", "")
        if fcm_token:
            tokens = tokens.filter(fcm_token=fcm_token)
        tokens.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DeviceTokenView(APIView):
    """
    POST   /api/device-tokens/   register an FCM token after login
    DELETE /api/device-tokens/   remove it on logout
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DeviceTokenSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        token = serializer.save()
        return Response(
            DeviceTokenSerializer().to_representation(token),
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request):
        serializer = DeviceTokenDeleteSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)

        tokens = DeviceToken.objects.filter(user=request.user)
        fcm_token = serializer.validated_data.get("fcm_token", "")
        if fcm_token:
            tokens = tokens.filter(fcm_token=fcm_token)

        deleted, _ = tokens.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(generics.RetrieveUpdateAPIView):
    """GET / PATCH /api/auth/me/ — the caller's own profile, never anyone else's."""

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "patch", "head", "options"]

    def get_serializer_class(self):
        return UserSerializer if self.request.method == "GET" else MeSerializer

    def get_object(self):
        return (
            User.objects.select_related("customer", "rider").get(pk=self.request.user.pk)
        )
