"""Discount codes."""

DISCOUNT_CODES = {"WELCOME10": 0.10, "SPRING25": 0.25}


def apply_discount_code(amount, code):
    """Reduce an amount by the percentage attached to a discount code."""
    rate = DISCOUNT_CODES.get(code.upper())
    if rate is None:
        raise ValueError(f"unknown discount code: {code}")
    return amount * (1 - rate)


def is_valid_code(code):
    return code.upper() in DISCOUNT_CODES
