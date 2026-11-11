"""Isolated preflight tests: fake configuration, no real network or secrets."""
import io
import json
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

import collaborate as m


class PreflightChecks(unittest.TestCase):
    def run_case(self, env, data, args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdin", io.StringIO(data)), \
             mock.patch.object(m, "request_json") as request, mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = m.main(args)
        return code, out.getvalue(), err.getvalue(), request.call_count

    def test_missing_peer_fails_closed(self):
        env = {"CLAUDE_MODEL": "claude-test", "GROK_MODEL": "grok-test", "COLLAB_ANTHROPIC_KEY": "fake-key"}
        code, out, _, count = self.run_case(env, "example", ["--confirm-paid", "--stdin-utf8"])
        result = json.loads(out)
        self.assertEqual(code, 2)
        self.assertEqual(count, 0)
        self.assertEqual(result["providers"]["grok"]["status"], "not_submitted")
        self.assertEqual(result["providers"]["claude"]["status"], "not_submitted")

    def test_invalid_key_fails_closed(self):
        env = {"CLAUDE_MODEL": "claude-test", "GROK_MODEL": "grok-test",
               "COLLAB_ANTHROPIC_KEY": "fake-key", "COLLAB_XAI_KEY": "line\nbreak"}
        code, out, _, count = self.run_case(env, "example", ["--confirm-paid", "--stdin-utf8"])
        self.assertEqual(code, 2)
        self.assertEqual(count, 0)
        self.assertNotIn("line", out)

    def test_bad_model_fails_before_input_secrets(self):
        env = {"CLAUDE_MODEL": "bad model", "GROK_MODEL": "grok-test"}
        code, out, _, count = self.run_case(env, "example", ["--confirm-paid", "--stdin-utf8", "--ask-keys"])
        self.assertEqual(code, 2)
        self.assertEqual(count, 0)
        self.assertIn("missing_or_invalid_model", out)

    def test_windows_pipe_rejected_by_default(self):
        with mock.patch.object(m.os, "name", "nt"), mock.patch.object(sys, "stdin", io.StringIO("???")):
            with self.assertRaisesRegex(ValueError, "windows_pipe_requires_explicit_utf8"):
                m.read_task(None, False)

    def test_utf8_byte_pipe(self):
        fake = mock.MagicMock()
        fake.isatty.return_value = False
        fake.buffer = io.BytesIO("中文".encode("utf-8"))
        with mock.patch.object(sys, "stdin", fake):
            self.assertEqual(m.read_task(None, True), "中文")

    def test_utf8_file_is_strict(self):
        with mock.patch.object(m.Path, "open", return_value=io.BytesIO("中文".encode("utf-8"))):
            self.assertEqual(m.read_task("ignored", False), "中文")
        with mock.patch.object(m.Path, "open", return_value=io.BytesIO(b"\xff")):
            with self.assertRaises(UnicodeError):
                m.read_task("ignored", False)

    def test_key_format(self):
        for key in ("", None, "a b", "a\nb", "中文"):
            self.assertFalse(m.valid_key(key))
        self.assertTrue(m.valid_key("fake-abc_123+/="))


if __name__ == "__main__":
    unittest.main()
