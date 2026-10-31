"""Cart pricing: subtotal, discounts, and tax."""

from shop.discounts import apply_discount_code
from shop.tax import tax_for_region


def subtotal(items):
    """Sum of item price times quantity."""
    return sum(item["price"] * item["quantity"] for item in items)


def cart_total(items, region, discount_code=None):
    """Final amount charged for a cart: discounted subtotal plus regional tax."""
    amount = subtotal(items)
    if discount_code:
        amount = apply_discount_code(amount, discount_code)
    return round(amount + tax_for_region(amount, region), 2)
