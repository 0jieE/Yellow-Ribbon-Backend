"""
django-unfold configuration — the look and navigation of /admin/.

Kept out of settings.py so the theme can be read and changed in one place.
Callbacks are dotted paths into core.admin_dashboard, resolved lazily.
"""

from django.templatetags.static import static
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

# Brand: the gold of the logo (admin_app icon) with the near-black of its
# storefront. Unfold buttons and links use the 500-700 range with white text,
# so those steps are the deeper end of the amber scale rather than the
# logo's exact yellow, which white text cannot sit on.
PRIMARY = {
    "50": "oklch(98.7% .022 95.277)",
    "100": "oklch(96.2% .059 95.617)",
    "200": "oklch(92.4% .12 95.746)",
    "300": "oklch(87.9% .169 91.605)",
    "400": "oklch(82.8% .189 84.429)",
    "500": "oklch(76.9% .188 70.08)",
    "600": "oklch(66.6% .179 58.318)",
    "700": "oklch(55.5% .163 48.4)",
    "800": "oklch(47.3% .137 46.201)",
    "900": "oklch(41.4% .112 45.904)",
    "950": "oklch(27.9% .077 45.635)",
}


def _logo(_request):
    return static("core/img/logo.png")


def _stylesheet(_request):
    return static("core/css/admin.css")


UNFOLD = {
    "SITE_TITLE": "Yellow Ribbon Pizza",
    "SITE_HEADER": "Yellow Ribbon Pizza",
    "SITE_SUBHEADER": "Delivery administration",
    # None hides "Return to site" on the login page, whose target would only
    # redirect straight back to the login form.
    "SITE_URL": None,
    "SITE_ICON": {"light": _logo, "dark": _logo},
    "SITE_FAVICONS": [
        {
            "rel": "icon",
            "sizes": "32x32",
            "type": "image/png",
            "href": lambda request: static("core/img/favicon-32.png"),
        },
        {
            "rel": "apple-touch-icon",
            "sizes": "180x180",
            "type": "image/png",
            "href": lambda request: static("core/img/favicon-180.png"),
        },
    ],
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
    "ENVIRONMENT": "core.admin_dashboard.environment_callback",
    "DASHBOARD_CALLBACK": "core.admin_dashboard.dashboard_callback",
    "STYLES": [_stylesheet],
    "COLORS": {"primary": PRIMARY},
    "LOGIN": {"image": None},
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": _("Overview"),
                "separator": False,
                "items": [
                    {
                        "title": _("Dashboard"),
                        "icon": "dashboard",
                        "link": reverse_lazy("admin:index"),
                    },
                ],
            },
            {
                "title": _("Operations"),
                "separator": True,
                "collapsible": False,
                "items": [
                    {
                        "title": _("Orders"),
                        "icon": "receipt_long",
                        "link": reverse_lazy("admin:orders_order_changelist"),
                        "badge": "core.admin_dashboard.pending_orders_badge",
                        "badge_variant": "warning",
                    },
                    {
                        "title": _("Deliveries"),
                        "icon": "local_shipping",
                        "link": reverse_lazy("admin:delivery_delivery_changelist"),
                    },
                    {
                        "title": _("Invoices"),
                        "icon": "payments",
                        "link": reverse_lazy("admin:payments_invoice_changelist"),
                    },
                ],
            },
            {
                "title": _("Catalog"),
                "separator": True,
                "collapsible": False,
                "items": [
                    {
                        "title": _("Menu"),
                        "icon": "local_pizza",
                        "link": reverse_lazy("admin:menu_menu_changelist"),
                    },
                ],
            },
            {
                "title": _("People"),
                "separator": True,
                "collapsible": False,
                "items": [
                    {
                        "title": _("Customers"),
                        "icon": "group",
                        "link": reverse_lazy("admin:accounts_customer_changelist"),
                    },
                    {
                        "title": _("Riders"),
                        "icon": "two_wheeler",
                        "link": reverse_lazy("admin:accounts_rider_changelist"),
                    },
                    {
                        "title": _("Users"),
                        "icon": "badge",
                        "link": reverse_lazy("admin:accounts_user_changelist"),
                    },
                ],
            },
            {
                "title": _("System"),
                "separator": True,
                "collapsible": False,
                "items": [
                    {
                        "title": _("Database backup"),
                        "icon": "backup",
                        "link": reverse_lazy("backup"),
                        # The file holds password hashes and customer data.
                        "permission": lambda request: request.user.is_superuser,
                    },
                ],
            },
            {
                "title": _("Records"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {
                        "title": _("Order items"),
                        "icon": "list_alt",
                        "link": reverse_lazy("admin:orders_orderitem_changelist"),
                    },
                    {
                        "title": _("Status history"),
                        "icon": "history",
                        "link": reverse_lazy("admin:orders_orderstatuslog_changelist"),
                    },
                    {
                        "title": _("Device tokens"),
                        "icon": "notifications",
                        "link": reverse_lazy("admin:accounts_devicetoken_changelist"),
                    },
                ],
            },
        ],
    },
}
