"""The checkout endpoint: POST /checkout with a cart and an optional discount code."""

from checkout import order_total


def handle_checkout(body):
    """Returns (status code, response). Any exception here becomes a 500 for the customer."""
    total = order_total(body["cart"], body.get("code"))
    return 200, {"total": total}
