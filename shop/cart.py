"""
The shopping cart, kept in the Django session.

Only menu ids and quantities are stored. Names and prices are always read
from the database, so a cart can never carry a stale or tampered price: the
order itself is priced again by OrderCreateSerializer when it is placed.
"""

from decimal import Decimal

from menu.models import Menu

SESSION_KEY = "shop_cart"
MAX_QUANTITY = 50


class Cart:
    def __init__(self, request):
        self.session = request.session
        raw = self.session.get(SESSION_KEY) or {}
        # Session JSON keys are strings; keep only sane entries.
        self.lines = {
            str(k): int(v)
            for k, v in raw.items()
            if str(k).isdigit() and isinstance(v, int) and 0 < v <= MAX_QUANTITY
        }

    def _save(self):
        self.session[SESSION_KEY] = self.lines
        self.session.modified = True

    def add(self, menu_id, quantity=1):
        key = str(menu_id)
        self.lines[key] = min(self.lines.get(key, 0) + quantity, MAX_QUANTITY)
        self._save()

    def set(self, menu_id, quantity):
        key = str(menu_id)
        if not (key.isascii() and key.isdigit()):
            return
        if quantity <= 0:
            self.lines.pop(key, None)
        else:
            self.lines[key] = min(quantity, MAX_QUANTITY)
        self._save()

    def clear(self):
        self.lines = {}
        self._save()

    @property
    def count(self):
        return sum(self.lines.values())

    def items(self):
        """[{menu, quantity, line_total}] for items that still exist, in name order."""
        menus = Menu.objects.filter(pk__in=[int(k) for k in self.lines]).order_by("name")
        return [
            {"menu": m, "quantity": self.lines[str(m.pk)], "line_total": m.price * self.lines[str(m.pk)]}
            for m in menus
        ]

    def subtotal(self, items=None):
        items = self.items() if items is None else items
        return sum((i["line_total"] for i in items if i["menu"].is_available), Decimal("0.00"))

    def as_order_items(self, items=None):
        """The `items` list OrderCreateSerializer expects."""
        items = self.items() if items is None else items
        return [{"menu_id": i["menu"].pk, "quantity": i["quantity"]} for i in items]


def cart_count(request):
    """Template context processor: the badge on the Cart tab."""
    if not request.path.startswith("/order/"):
        return {}
    return {"cart_count": Cart(request).count}
