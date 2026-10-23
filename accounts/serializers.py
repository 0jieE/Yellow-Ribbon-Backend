"""Serializers for registration, login and the /me/ profile endpoint."""

from django.contrib.auth.password_validation import validate_password
from django.db import IntegrityError, transaction
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import Customer, DevicePlatform, DeviceToken, Rider, User


def save_user_or_field_error(user):
    """
    Persist a new user, turning a unique-constraint collision into a field
    error instead of a 500.

    The serializers check for duplicates first, but two registrations racing
    each other — a double-tapped button is enough — can both pass that check
    and only lose at the database. Wrapped in its own atomic block so the
    failed INSERT does not poison the outer transaction.
    """
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError as exc:
        detail = str(exc).lower()
        if "email" in detail:
            raise serializers.ValidationError(
                {"email": "That email is already registered."}
            )
        if "username" in detail:
            raise serializers.ValidationError({"username": "That username is taken."})
        raise serializers.ValidationError(
            {"detail": "Those details are already registered."}
        )
    return user


class CustomerProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = [
            "id",
            "default_address",
            "default_latitude",
            "default_longitude",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class RiderProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rider
        fields = [
            "id",
            "vehicle_type",
            "plate_number",
            "is_available",
            "current_latitude",
            "current_longitude",
            "last_location_update",
            "created_at",
        ]
        # A rider changes availability via PATCH /api/rider/availability/ and
        # coordinates via POST /api/rider/location/ (build step 6), never here.
        read_only_fields = [
            "id",
            "is_available",
            "current_latitude",
            "current_longitude",
            "last_location_update",
            "created_at",
        ]


class UserSerializer(serializers.ModelSerializer):
    """Shape returned by GET /api/auth/me/ and embedded in the login response."""

    role = serializers.CharField(read_only=True)
    customer = CustomerProfileSerializer(read_only=True)
    rider = RiderProfileSerializer(read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "phone",
            "is_admin",
            "is_rider",
            "is_customer",
            "is_active",
            "date_joined",
            "role",
            "customer",
            "rider",
        ]
        read_only_fields = [
            "id",
            "username",
            "is_admin",
            "is_rider",
            "is_customer",
            "is_active",
            "date_joined",
        ]


class MeSerializer(UserSerializer):
    """
    PATCH /api/auth/me/.

    Accepts the customer's saved default address as a nested write so the
    app can update the profile in one call.
    """

    customer = CustomerProfileSerializer(required=False)

    def validate_email(self, value):
        # Blank clears it; only a real address has to be unique.
        if not value:
            return ""
        qs = User.objects.filter(email__iexact=value).exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("That email is already in use.")
        return value

    @transaction.atomic
    def update(self, instance, validated_data):
        customer_data = validated_data.pop("customer", None)

        user = super().update(instance, validated_data)

        if customer_data:
            profile = getattr(user, "customer", None)
            if profile is None:
                raise serializers.ValidationError(
                    {"customer": "This account has no customer profile."}
                )
            for field, value in customer_data.items():
                setattr(profile, field, value)
            profile.save()

        return user


class RegisterSerializer(serializers.ModelSerializer):
    """
    POST /api/auth/register/ — customers only.

    Riders are created by the admin; admins via `createsuperuser`. The role
    flags are never read from the request body.
    """

    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    password_confirm = serializers.CharField(
        write_only=True, style={"input_type": "password"}
    )

    default_address = serializers.CharField(
        max_length=255, required=False, allow_blank=True, write_only=True
    )
    default_latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True, write_only=True
    )
    default_longitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, required=False, allow_null=True, write_only=True
    )

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "password",
            "password_confirm",
            "first_name",
            "last_name",
            "phone",
            "default_address",
            "default_latitude",
            "default_longitude",
        ]

    def validate_email(self, value):
        # Optional: absent or blank is fine, and many users may have none.
        if not value:
            return ""
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("That email is already registered.")
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError(
                {"password_confirm": "Passwords do not match."}
            )
        validate_password(attrs["password"])
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        profile_fields = {
            "default_address": validated_data.pop("default_address", ""),
            "default_latitude": validated_data.pop("default_latitude", None),
            "default_longitude": validated_data.pop("default_longitude", None),
        }
        password = validated_data.pop("password")

        user = User(
            **validated_data,
            is_customer=True,
            is_rider=False,
            is_admin=False,
        )
        user.set_password(password)
        save_user_or_field_error(user)

        Customer.objects.create(user=user, **profile_fields)
        return user


class DeviceTokenSerializer(serializers.Serializer):
    """POST /api/device-tokens/ — register an FCM token after login."""

    fcm_token = serializers.CharField(max_length=255)
    platform = serializers.ChoiceField(
        choices=DevicePlatform.choices, required=False, allow_blank=True, default=""
    )

    def validate_fcm_token(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("fcm_token cannot be blank.")
        return value

    def create(self, validated_data):
        user = self.context["request"].user
        # Keyed on the token, not (user, token): if someone else logs in on
        # this handset the registration moves to them rather than leaving
        # the previous user receiving their notifications.
        token, _ = DeviceToken.objects.update_or_create(
            fcm_token=validated_data["fcm_token"],
            defaults={"user": user, "platform": validated_data.get("platform", "")},
        )
        return token

    def to_representation(self, instance):
        return {
            "id": instance.pk,
            "fcm_token": instance.fcm_token,
            "platform": instance.platform,
            "updated_at": instance.updated_at,
        }


class DeviceTokenDeleteSerializer(serializers.Serializer):
    """DELETE /api/device-tokens/ — drop one token, or all of the caller's."""

    fcm_token = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )


class LoginSerializer(TokenObtainPairSerializer):
    """
    POST /api/auth/login/ — returns access + refresh + role flags.

    Accepts a username *or* an email address, in either `identifier` or the
    original `username` key. All three Flutter apps send `username` today
    and keep working untouched; `identifier` is the name to move to, since
    the value is no longer necessarily a username.

    Emails match case-insensitively — people type Gmail addresses however
    they feel on the day.

    **Precedence.** An exact username match is tried first; only if nothing
    owns that username is the value treated as an email. So if one account
    is named `bob@example.com` and another *has* that email, logging in
    with `bob@example.com` authenticates the account whose **username** it
    is. Usernames are what the account is keyed on and cannot be changed
    through the API, which makes them the stable side of the pair.

    Failures always return SimpleJWT's generic "No active account found
    with the given credentials", so the endpoint never reveals whether a
    username or email exists.
    """

    identifier = serializers.CharField(
        required=False, write_only=True, allow_blank=True
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The parent marks `username` required. Either key may now carry the
        # value, so neither is mandatory on its own.
        self.fields[self.username_field].required = False

    def _resolve_username(self, identifier):
        """
        Map what was typed onto a username for `authenticate()`.

        Returns the identifier unchanged when nothing matches, so a bad
        login fails through the normal path with the normal message rather
        than a distinguishable one.
        """
        if User.objects.filter(username=identifier).exists():
            return identifier

        # order_by("pk") keeps this deterministic even if the case-insensitive
        # email constraint were ever missing: the oldest account wins.
        match = (
            User.objects.filter(email__iexact=identifier)
            .order_by("pk")
            .values_list("username", flat=True)
            .first()
        )
        return match or identifier

    def validate(self, attrs):
        raw = attrs.pop("identifier", "") or attrs.get(self.username_field) or ""
        raw = raw.strip()

        if not raw:
            raise serializers.ValidationError(
                {"identifier": "Enter your username or email address."}
            )

        attrs[self.username_field] = self._resolve_username(raw)

        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data
