from shop.pricing import cart_total


def test_cart_total_applies_discount_then_tax():
    items = [{"price": 100.0, "quantity": 1}]
    assert cart_total(items, "us", "WELCOME10") == 96.3
