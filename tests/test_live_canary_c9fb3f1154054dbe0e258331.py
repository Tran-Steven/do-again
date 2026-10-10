import unittest

from canary_live_c9fb3f1154054dbe0e258331 import canonical_label


class CanonicalLabelTests(unittest.TestCase):
    def test_case(self):
        self.assertEqual(canonical_label("HeLLo WoRLD"), "hello-world")

    def test_whitespace(self):
        self.assertEqual(canonical_label("  Hello   World  Again  "), "hello-world-again")
        self.assertEqual(canonical_label(" Hello\tWorld\nAgain "), "hello-world-again")

    def test_empty(self):
        self.assertEqual(canonical_label(""), "")
        self.assertEqual(canonical_label(" \t\n "), "")

    def test_non_ascii_rejected(self):
        for value in ("café", "naïve", "東京", "hello\u00a0world"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    canonical_label(value)
