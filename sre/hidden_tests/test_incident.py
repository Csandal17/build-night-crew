"""Hidden tests: the tester adds these to a clean copy of the project after the fix.
The fixer never sees them, so passing them means the fix works, not that it was written to the test."""
import unittest

from app import handle_checkout
from checkout import order_total

CART = [{"price": 40.0, "qty": 2}, {"price": 20.0, "qty": 1}]  # 100.00


class IncidentTests(unittest.TestCase):
    def test_lowercase_code_still_gets_its_discount(self):
        # The new mobile app sends codes exactly as typed. A valid code must not lose its discount.
        self.assertEqual(order_total(CART, "welcome10"), 90.0)

    def test_code_with_spaces_still_gets_its_discount(self):
        self.assertEqual(order_total(CART, " Autumn20 "), 80.0)

    def test_unknown_code_does_not_crash_checkout(self):
        self.assertEqual(handle_checkout({"cart": CART, "code": "EXPIRED5"}), (200, {"total": 100.0}))

    def test_known_codes_unchanged(self):
        self.assertEqual(order_total(CART, "WELCOME10"), 90.0)
        self.assertEqual(order_total(CART), 100.0)


if __name__ == "__main__":
    unittest.main()
