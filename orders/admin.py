from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.filters.admin import RangeDateFilter
from unfold.decorators import action, display
from unfold.enums import ActionVariant
from unfold.forms import ActionForm

from accounts.models import Rider
from core.admin_dashboard import (
    ORDER_STATUS_LABELS,
    PAYMENT_STATUS_LABELS,
    peso,
)
from core.admin_tools import csv_export_action
from core.notifications import notify_rider_assigned
from delivery.models import Delivery

from .admin_api import apply_status_change
from .admin_serializers import AssignRiderSerializer
from .constants import OrderStatus, is_valid_transition
from .models import Order, OrderItem, OrderStatusLog


class OrderItemInline(TabularInline):
    model = OrderItem
    extra = 0
    # Prices are snapshots taken at checkout — editing them here would
    # rewrite history (spec section 4).
    readonly_fields = ("menu", "quantity", "unit_price", "line_subtotal")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class OrderStatusLogInline(TabularInline):
    model = OrderStatusLog
    extra = 0
    readonly_fields = ("status", "changed_by", "changed_at")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class RiderChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, rider):
        name = rider.user.get_full_name() or rider.user.username
        vehicle = f" · {rider.vehicle_type}" if rider.vehicle_type else ""
        duty = "on duty" if rider.is_available else "off duty"
        return f"{name}{vehicle} ({duty})"


class OrderActionForm(ActionForm):
    """Extra inputs for the bulk actions that need one."""

    rider = RiderChoiceField(
        label="Rider",
        required=False,
        empty_label="Rider (for Assign)",
        queryset=Rider.objects.select_related("user")
        .filter(user__is_active=True)
        .order_by("-is_available", "user__username"),
    )
    cancel_reason = forms.CharField(
        label="Reason",
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={"placeholder": "Reason (for Cancel)"}),
    )


def _order_columns():
    return [
        ("Order", lambda o: o.order_number),
        ("Placed", lambda o: o.placed_at.strftime("%Y-%m-%d %H:%M")),
        ("Customer", lambda o: o.customer.user.get_full_name() or o.customer.user.username),
        ("Phone", lambda o: o.customer.user.phone),
        ("Status", lambda o: o.get_status_display()),
        ("Payment", lambda o: o.get_payment_type_display()),
        ("Payment status", lambda o: o.get_payment_status_display()),
        ("Subtotal", lambda o: o.subtotal),
        ("Delivery fee", lambda o: o.delivery_fee),
        ("Total", lambda o: o.total),
        ("Address", lambda o: o.delivery_address),
    ]


def _first_error(serializer):
    """The first human-readable message from a DRF serializer's errors."""
    for messages_ in serializer.errors.values():
        if isinstance(messages_, (list, tuple)) and messages_:
            return str(messages_[0])
        return str(messages_)
    return "invalid"


@admin.register(Order)
class OrderAdmin(ModelAdmin):
    list_display = (
        "order_number",
        "customer_name",
        "status_badge",
        "payment_type",
        "payment_badge",
        "rider_name",
        "money_total",
        "placed_at",
    )
    list_filter = (
        "status",
        "payment_type",
        "payment_status",
        ("placed_at", RangeDateFilter),
    )
    list_filter_submit = True
    search_fields = (
        "order_number",
        "customer__user__username",
        "customer__user__email",
        "customer__user__first_name",
        "customer__user__last_name",
        "delivery_address",
    )
    date_hierarchy = "placed_at"
    readonly_fields = (
        "order_number",
        # Changed through the actions, never by typing into the form: an edit
        # here would skip the status log and the customer's push notification.
        "status",
        "subtotal",
        "delivery_fee",
        "total",
        "placed_at",
        "eta_minutes",
        "eta_at",
    )
    raw_id_fields = ("customer",)
    inlines = [OrderItemInline, OrderStatusLogInline]
    ordering = ("-placed_at",)
    list_select_related = ("customer__user", "delivery__rider__user")
    action_form = OrderActionForm
    actions = [
        "confirm_orders",
        "start_preparing",
        "mark_ready",
        "assign_rider",
        "cancel_orders",
        csv_export_action("orders", _order_columns()),
    ]
    actions_detail = ["advance_order", "cancel_order"]

    fieldsets = (
        (None, {"fields": ("order_number", "customer", "status")}),
        ("Payment", {"fields": ("payment_type", "payment_status")}),
        (
            "Money",
            {
                "fields": ("subtotal", "delivery_fee", "total"),
                "description": (
                    "Computed server-side from the line items. Read-only so a "
                    "receipt cannot be edited after the fact."
                ),
            },
        ),
        (
            "Delivery",
            {
                "fields": (
                    "delivery_address",
                    "delivery_latitude",
                    "delivery_longitude",
                    "delivery_notes",
                    "eta_minutes",
                    "eta_at",
                )
            },
        ),
        (
            "Timeline",
            {
                "fields": (
                    "placed_at",
                    "confirmed_at",
                    "delivered_at",
                    "cancelled_at",
                    "cancel_reason",
                )
            },
        ),
    )

    # --- columns -------------------------------------------------------

    @display(description="customer", ordering="customer__user__first_name")
    def customer_name(self, obj):
        user = obj.customer.user
        return user.get_full_name() or user.username

    @display(description="status", ordering="status", label=ORDER_STATUS_LABELS)
    def status_badge(self, obj):
        return obj.get_status_display()

    @display(description="payment", ordering="payment_status", label=PAYMENT_STATUS_LABELS)
    def payment_badge(self, obj):
        return obj.get_payment_status_display()

    @display(description="rider")
    def rider_name(self, obj):
        delivery = getattr(obj, "delivery", None)
        if delivery is None:
            return "—"
        return delivery.rider.user.get_full_name() or delivery.rider.user.username

    @display(description="total", ordering="total")
    def money_total(self, obj):
        return peso(obj.total)

    # --- status workflow -----------------------------------------------

    def _move(self, request, queryset, new_status, reason=""):
        """
        Walk each selected order to `new_status` through the same path as the
        API (apply_status_change), so the status log and the customer's push
        notification happen exactly as they do from the mobile apps. Orders
        that may not legally make the move are skipped and named.
        """
        moved, skipped = 0, []
        for pk in list(queryset.values_list("pk", flat=True)):
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=pk)
                if not is_valid_transition(order.status, new_status):
                    skipped.append(f"{order.order_number} ({order.get_status_display().lower()})")
                    continue
                apply_status_change(order, new_status, changed_by=request.user, reason=reason)
                moved += 1

        label = OrderStatus(new_status).label.lower()
        if moved:
            self.message_user(request, f"{moved} order(s) moved to {label}.", messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                f"Skipped, not allowed to move to {label}: {', '.join(skipped)}.",
                messages.WARNING,
            )
        return moved

    @action(description="Confirm selected orders", permissions=["change"])
    def confirm_orders(self, request, queryset):
        self._move(request, queryset, OrderStatus.CONFIRMED)

    @action(description="Start preparing", permissions=["change"])
    def start_preparing(self, request, queryset):
        self._move(request, queryset, OrderStatus.PREPARING)

    @action(description="Mark ready for pickup", permissions=["change"])
    def mark_ready(self, request, queryset):
        self._move(request, queryset, OrderStatus.READY)

    @action(description="Cancel selected orders", permissions=["change"])
    def cancel_orders(self, request, queryset):
        reason = (request.POST.get("cancel_reason") or "").strip()
        self._move(request, queryset, OrderStatus.CANCELLED, reason=reason)

    @action(description="Assign to rider", permissions=["change"])
    def assign_rider(self, request, queryset):
        rider_id = request.POST.get("rider")
        if not rider_id:
            self.message_user(
                request, "Pick a rider in the form next to the action first.", messages.ERROR
            )
            return

        assigned, skipped = 0, []
        for pk in list(queryset.values_list("pk", flat=True)):
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=pk)
                # The same rules as POST /api/admin/orders/{id}/assign/.
                serializer = AssignRiderSerializer(
                    data={"rider_id": rider_id}, context={"order": order}
                )
                if not serializer.is_valid():
                    skipped.append(f"{order.order_number}: {_first_error(serializer)}")
                    continue
                rider = serializer.validated_data["rider"]
                Delivery.objects.create(
                    order=order, rider=rider, rider_earnings=serializer.rider_earnings()
                )
                notify_rider_assigned(order, rider)
                assigned += 1

        if assigned:
            self.message_user(request, f"{assigned} order(s) assigned.", messages.SUCCESS)
        for line in skipped:
            self.message_user(request, f"Not assigned — {line}", messages.WARNING)

    # --- buttons on the order page --------------------------------------

    # Admin-driven steps. out_for_delivery and delivered belong to the rider
    # app, which moves the order when the rider picks up and drops off.
    ADVANCE = {
        OrderStatus.PENDING: OrderStatus.CONFIRMED,
        OrderStatus.CONFIRMED: OrderStatus.PREPARING,
        OrderStatus.PREPARING: OrderStatus.READY,
    }

    @action(description="Advance to next step", url_path="advance", permissions=["change"])
    def advance_order(self, request, object_id):
        order = Order.objects.filter(pk=object_id).first()
        target = self.ADVANCE.get(order.status) if order else None
        if target is None:
            self.message_user(
                request,
                "Nothing to advance — the next step is made by the rider app, or the order is finished.",
                messages.INFO,
            )
        else:
            self._move(request, Order.objects.filter(pk=object_id), target)
        return HttpResponseRedirect(reverse("admin:orders_order_change", args=[object_id]))

    @action(
        description="Cancel order",
        url_path="cancel",
        permissions=["change"],
        variant=ActionVariant.DANGER,
    )
    def cancel_order(self, request, object_id):
        self._move(request, Order.objects.filter(pk=object_id), OrderStatus.CANCELLED)
        return HttpResponseRedirect(reverse("admin:orders_order_change", args=[object_id]))


@admin.register(OrderItem)
class OrderItemAdmin(ModelAdmin):
    list_display = ("order", "menu", "quantity", "unit_price", "line_subtotal")
    list_filter = ("menu", "order__status")
    search_fields = ("order__order_number", "menu__name")
    raw_id_fields = ("order", "menu")
    list_select_related = ("order", "menu")
    # Snapshots taken at checkout (spec section 4) — never editable.
    readonly_fields = ("unit_price", "line_subtotal")


@admin.register(OrderStatusLog)
class OrderStatusLogAdmin(ModelAdmin):
    list_display = ("order", "status", "changed_by", "changed_at")
    list_filter = ("status", "changed_at")
    search_fields = ("order__order_number", "changed_by__username")
    raw_id_fields = ("order", "changed_by")
    date_hierarchy = "changed_at"
    list_select_related = ("order", "changed_by")
    readonly_fields = ("order", "status", "changed_by", "changed_at")

    def has_add_permission(self, request):
        # The audit trail is written by the system, not typed in by hand.
        return False
