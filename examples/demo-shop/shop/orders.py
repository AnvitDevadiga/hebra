"""Order checkout."""

from shop.payments import charge_with_retry
from shop.pricing import cart_total


def checkout(cart, gateway, region, discount_code=None):
    """Price a cart and charge the customer for it."""
    amount = cart_total(cart["items"], region, discount_code)
    receipt = charge_with_retry(gateway, amount)
    return {"amount": amount, "receipt": receipt}
