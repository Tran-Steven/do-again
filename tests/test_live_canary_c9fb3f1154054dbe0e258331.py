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
