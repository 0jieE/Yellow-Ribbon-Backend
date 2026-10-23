"""
What a payment costs, from the rates in StoreSettings.

COD costs nothing. Online providers charge a percentage plus an optional
fixed amount; both are the store's to set, never hardcoded.
"""

from decimal import ROUND_HALF_UP, Decimal

from core.models import StoreSettings

from .models import Provider

HUNDRED = Decimal("100")
TWO_PLACES = Decimal("0.01")


def compute_fee(provider, amount, store=None):
    """
    The gateway fee on `amount`, rounded to centavos and never more than the
    payment itself. Returns Decimal.
    """
    amount = Decimal(amount)
    if provider == Provider.PAYMONGO:
        percent_name, fixed_name = "paymongo_fee_percent", "paymongo_fee_fixed"
    elif provider == Provider.MAYA:
        percent_name, fixed_name = "maya_fee_percent", "maya_fee_fixed"
    else:
        return Decimal("0.00")

    store = store or StoreSettings.load()
    fee = amount * getattr(store, percent_name) / HUNDRED + getattr(store, fixed_name)
    fee = fee.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return min(fee, amount)


def split(provider, amount, store=None):
    """(fee, net) for a payment of `amount`."""
    fee = compute_fee(provider, amount, store)
    return fee, Decimal(amount) - fee
