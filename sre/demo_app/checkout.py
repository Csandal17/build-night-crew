"""Checkout pricing for the shop: cart subtotal and discount codes."""

# Discount codes and their percentage off. Codes are stored in capitals.
DISCOUNTS = {"WELCOME10": 10, "AUTUMN20": 20}


def subtotal(cart):
    """Total of every item in the cart, before discounts."""
    return round(sum(item["price"] * item["qty"] for item in cart), 2)


def apply_discount(amount, code):
    """The amount after a discount code. No code means full price."""
    if not code:
        return amount
    percent = DISCOUNTS[code]
    return round(amount * (100 - percent) / 100, 2)


def order_total(cart, code=None):
    """What the customer pays."""
    return apply_discount(subtotal(cart), code)
