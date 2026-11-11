"""Offline checks for bounded, allowlisted provider error diagnostics."""
import contextlib
import io
import json
import unittest
import urllib.error
from unittest import mock

import collaborate as mod


class ErrorClassificationTests(unittest.TestCase):
    def run_error(self, provider, code, body):
        err = urllib.error.HTTPError("https://example.invalid", code, "secret reason",
                                     {"request-id": "req_ok"}, body)
        with mock.patch.object(mod, "request_json", side_effect=err) as request:
            result = mod.run_provider(provider, "task", "model", "fake-key", 5, 20)
        request.assert_called_once()
        return result

    def test_allowlisted_types_without_message_or_key(self):
        for provider in ("claude", "grok"):
            with self.subTest(provider=provider):
                body = io.BytesIO(b'{"error":{"type":"invalid_request_error",'
                                  b'"message":"secret fake-key"},"private":"secret"}')
                result = self.run_error(provider, 400, body)
                self.assertEqual((result["status"], result["error_type"], result["request_id"]),
                                 ("failed", "invalid_request_error", "req_ok"))
                self.assertNotIn("secret", json.dumps(result))
                self.assertNotIn("fake-key", json.dumps(result))

    def test_unknown_malformed_and_oversize_body_are_not_exposed(self):
        cases = [b'{"error":{"type":"fake-key","message":"secret"}}',
                 b'not-json-secret', b'{"error":{"type":[]}}',
                 b'x' * (mod.MAX_ERROR_BYTES + 1)]
        for body in cases:
            with self.subTest(size=len(body)):
                result = self.run_error("claude", 400, io.BytesIO(body))
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("error_type", result)
                self.assertNotIn("secret", json.dumps(result))
                self.assertNotIn("fake-key", json.dumps(result))

    def test_unavailable_body_and_non_400_preserve_status(self):
        class BrokenBody:
            def read(self, *args):
                raise TimeoutError("secret fake-key")
            def close(self):
                pass
        for code, expected in ((400, "failed"), (504, "submission_uncertain")):
            with self.subTest(code=code):
                result = self.run_error("claude", code, BrokenBody())
                self.assertEqual(result["status"], expected)
                self.assertNotIn("error_type", result)
                self.assertNotIn("secret", json.dumps(result))

    def test_help_covers_task_file(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as stopped:
            mod.main(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("Confirm sending the task", out.getvalue())
        self.assertNotIn("sending stdin", out.getvalue())


if __name__ == "__main__":
    unittest.main()
