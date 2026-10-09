import unittest

from examples.add_integers import add_integers


class AddIntegersTests(unittest.TestCase):
    def test_positive_integers(self):
        self.assertEqual(add_integers(2, 3), 5)

    def test_negative_integers(self):
        self.assertEqual(add_integers(-4, -5), -9)

    def test_mixed_sign_integers(self):
        self.assertEqual(add_integers(-7, 12), 5)

    def test_zero(self):
        self.assertEqual(add_integers(0, 8), 8)
