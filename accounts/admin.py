from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db.models import Count, Q
from unfold.admin import ModelAdmin
from unfold.decorators import action, display
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from core import fcm
from core.admin_tools import csv_export_action
from orders.constants import ACTIVE_DELIVERY_STATUSES

from .models import Customer, DeviceToken, Rider, User


MAX_REPORT_LINES = 15


def _token_preview(token):
    return f"{token[:8]}...({len(token)})"


def report_test_push(modeladmin, request, targets, users_without_tokens=()):
    """
    Send a test push to `targets` ([(label, token)]) and tell the admin exactly
    what happened, in the message bar: how many Firebase accepted, and the
    error class for each that it did not.
    """
    for name in users_without_tokens:
        modeladmin.message_user(
            request,
            f"{name} has no device token registered, so nothing can be sent to them. "
            "They need to sign in on their phone (and allow notifications).",
            messages.WARNING,
        )

    if not targets:
        if not users_without_tokens:
            modeladmin.message_user(request, "Nothing selected to send to.", messages.WARNING)
        return

    error, results = fcm.send_test_push(targets)
    if error:
        modeladmin.message_user(request, f"No push was sent. {error}", messages.ERROR)
        return

    sent = [r for r in results if r[1]]
    failed = [r for r in results if not r[1]]
    if sent:
        modeladmin.message_user(
            request,
            f"{len(sent)} test push(es) accepted by Firebase. Check the phone: if nothing "
            "shows, the problem is on the app side (permission, channel or foreground handling).",
            messages.SUCCESS,
        )
    for label, _, detail in failed[:MAX_REPORT_LINES]:
        modeladmin.message_user(request, f"{label}: {detail}", messages.WARNING)
    if len(failed) > MAX_REPORT_LINES:
        modeladmin.message_user(
            request, f"...and {len(failed) - MAX_REPORT_LINES} more failures.", messages.WARNING
        )


@admin.register(User)
class UserAdmin(DjangoUserAdmin, ModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm

    list_display = ("username", "email", "phone", "role_badge", "is_active", "date_joined")
    list_filter = ("is_admin", "is_rider", "is_customer", "is_active", "is_staff")
    search_fields = ("username", "email", "first_name", "last_name", "phone")
    ordering = ("username",)
    readonly_fields = ("date_joined", "last_login")
    actions = ["activate_users", "deactivate_users", "send_test_push"]

    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Yellow Ribbon", {"fields": ("phone", "is_admin", "is_rider", "is_customer")}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        (
            "Yellow Ribbon",
            {"fields": ("email", "phone", "is_admin", "is_rider", "is_customer")},
        ),
    )

    @display(
        description="role",
        label={"admin": "danger", "rider": "info", "customer": "success"},
    )
    def role_badge(self, obj):
        return obj.role

    @action(description="Send a test push to selected users", permissions=["change"])
    def send_test_push(self, request, queryset):
        targets, missing = [], []
        for user in queryset.prefetch_related("device_tokens"):
            tokens = list(user.device_tokens.all())
            if not tokens:
                missing.append(user.username)
            targets += [
                (f"{user.username} ({t.platform or '?'}) {_token_preview(t.fcm_token)}", t.fcm_token)
                for t in tokens
            ]
        report_test_push(self, request, targets, missing)

    @action(description="Activate selected accounts", permissions=["change"])
    def activate_users(self, request, queryset):
        count = queryset.filter(is_active=False).update(is_active=True)
        self.message_user(request, f"{count} account(s) activated.", messages.SUCCESS)

    @action(description="Deactivate selected accounts", permissions=["change"])
    def deactivate_users(self, request, queryset):
        # Locking yourself out of the admin mid-demo is not an undo-able typo.
        queryset = queryset.exclude(pk=request.user.pk)
        count = queryset.filter(is_active=True).update(is_active=False)
        self.message_user(
            request,
            f"{count} account(s) deactivated. Your own account is never deactivated here.",
            messages.SUCCESS,
        )


@admin.register(Customer)
class CustomerAdmin(ModelAdmin):
    list_display = (
        "user",
        "full_name",
        "phone",
        "default_address",
        "order_count",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = (
        "user__username",
        "user__email",
        "user__first_name",
        "user__last_name",
        "user__phone",
        "default_address",
    )
    raw_id_fields = ("user",)
    readonly_fields = ("created_at",)
    ordering = ("user__username",)
    # Without this the changelist runs one query per row for the username.
    list_select_related = ("user",)
    actions = [
        csv_export_action(
            "customers",
            [
                ("Username", lambda c: c.user.username),
                ("Name", lambda c: c.user.get_full_name()),
                ("Email", lambda c: c.user.email),
                ("Phone", lambda c: c.user.phone),
                ("Default address", lambda c: c.default_address),
                ("Joined", lambda c: c.created_at.strftime("%Y-%m-%d")),
            ],
        )
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_orders=Count("orders"))

    @admin.display(description="name")
    def full_name(self, obj):
        return obj.user.get_full_name() or "—"

    @admin.display(description="phone")
    def phone(self, obj):
        return obj.user.phone or "—"

    @admin.display(description="orders", ordering="_orders")
    def order_count(self, obj):
        return obj._orders


@admin.register(DeviceToken)
class DeviceTokenAdmin(ModelAdmin):
    list_display = ("user", "short_token", "platform", "updated_at")
    list_filter = ("platform", "updated_at")
    search_fields = ("user__username", "user__email", "fcm_token")
    raw_id_fields = ("user",)
    readonly_fields = ("updated_at",)
    list_select_related = ("user",)
    actions = ["send_test_push"]

    @action(description="Send a test push to selected devices", permissions=["change"])
    def send_test_push(self, request, queryset):
        targets = [
            (f"{t.user.username} ({t.platform or '?'}) {_token_preview(t.fcm_token)}", t.fcm_token)
            for t in queryset.select_related("user")
        ]
        report_test_push(self, request, targets)

    @admin.display(description="token")
    def short_token(self, obj):
        return f"{obj.fcm_token[:24]}…" if len(obj.fcm_token) > 24 else obj.fcm_token


@admin.register(Rider)
class RiderAdmin(ModelAdmin):
    list_display = (
        "user",
        "full_name",
        "vehicle_type",
        "plate_number",
        "is_available",
        "active_deliveries",
        "last_location_update",
    )
    list_filter = ("is_available", "vehicle_type", "created_at")
    search_fields = (
        "user__username",
        "user__email",
        "user__first_name",
        "user__last_name",
        "plate_number",
    )
    raw_id_fields = ("user",)
    readonly_fields = (
        "current_latitude",
        "current_longitude",
        "last_location_update",
        "created_at",
    )
    ordering = ("user__username",)
    list_select_related = ("user",)
    actions = ["set_on_duty", "set_off_duty"]

    @action(description="Mark selected riders on duty", permissions=["change"])
    def set_on_duty(self, request, queryset):
        count = queryset.filter(is_available=False).update(is_available=True)
        self.message_user(request, f"{count} rider(s) put on duty.", messages.SUCCESS)

    @action(description="Mark selected riders off duty", permissions=["change"])
    def set_off_duty(self, request, queryset):
        count = queryset.filter(is_available=True).update(is_available=False)
        self.message_user(request, f"{count} rider(s) taken off duty.", messages.SUCCESS)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                _active=Count(
                    "deliveries",
                    filter=Q(deliveries__status__in=ACTIVE_DELIVERY_STATUSES),
                )
            )
        )

    @admin.display(description="name")
    def full_name(self, obj):
        return obj.user.get_full_name() or "—"

    @admin.display(description="on delivery", ordering="_active")
    def active_deliveries(self, obj):
        return obj._active
