"""Guard the exit-evidence error codes that several standalone modules repeat."""
from __future__ import annotations

import unittest

import ai_team  # noqa: F401  (sets up the import paths the entry point uses)
from claude import durable_claude, readonly_ledger
from coordinator import EXIT_UNCONFIRMED, POLL_ERRORS
from grok import durable_grok
import legacy_reconcile


class SharedConstantTests(unittest.TestCase):
    def test_every_copy_matches_the_coordinator(self):
        copies = {"durable_claude": durable_claude._EXIT_UNCONFIRMED,
                  "readonly_ledger": readonly_ledger._EXIT_UNCONFIRMED,
                  "durable_grok": durable_grok._EXIT_UNCONFIRMED,
                  "legacy_reconcile": legacy_reconcile._EXIT_UNCONFIRMED}
        for name, value in copies.items():
            with self.subTest(module=name):
                self.assertEqual(len(value), len(set(value)))
                self.assertEqual(frozenset(value), EXIT_UNCONFIRMED)

    def test_coordinator_can_forward_each_code(self):
        self.assertLessEqual(EXIT_UNCONFIRMED, POLL_ERRORS)


if __name__ == "__main__":
    unittest.main()
