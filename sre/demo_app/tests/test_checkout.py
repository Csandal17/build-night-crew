import unittest

from app import handle_checkout
from checkout import order_total, subtotal

CART = [{"price": 40.0, "qty": 2}, {"price": 20.0, "qty": 1}]  # 100.00


class CheckoutTests(unittest.TestCase):
    def test_subtotal(self):
        self.assertEqual(subtotal(CART), 100.0)

    def test_no_code_is_full_price(self):
        self.assertEqual(order_total(CART), 100.0)

    def test_welcome_code(self):
        self.assertEqual(order_total(CART, "WELCOME10"), 90.0)

    def test_autumn_code(self):
        self.assertEqual(order_total(CART, "AUTUMN20"), 80.0)

    def test_endpoint(self):
        self.assertEqual(handle_checkout({"cart": CART, "code": "WELCOME10"}), (200, {"total": 90.0}))


if __name__ == "__main__":
    unittest.main()
