"""Offline behavioral tests; no network or real provider credentials."""
import contextlib
import importlib.util
import io
import getpass
import json
import os
from pathlib import Path
import unittest
from unittest import mock
import urllib.error
import warnings

SCRIPT = Path(__file__).with_name("collaborate.py")
spec = importlib.util.spec_from_file_location("collab_public_mvp", SCRIPT)
mvp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mvp)


class OfflineTests(unittest.TestCase):
    def test_unicode_response_survives_legacy_stdout_encoding(self):
        env = {"COLLAB_ANTHROPIC_KEY": "fake-a", "COLLAB_XAI_KEY": "fake-b",
               "CLAUDE_MODEL": "test-model", "GROK_MODEL": "test-model"}
        def fake_request(url, headers, payload, timeout):
            if "anthropic" in url:
                return {"id": "c1", "model": "m1", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "答案 😀"}]}
            return {"id": "g1", "model": "m2", "status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "答案 😀"}]}]}
        raw = io.BytesIO()
        legacy = io.TextIOWrapper(raw, encoding="cp1252", write_through=True)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mvp, "request_json", side_effect=fake_request), \
             mock.patch.object(sys_module(), "stdin", io.StringIO("same task")), contextlib.redirect_stdout(legacy):
            code = mvp.main(["--confirm-paid", "--stdin-utf8"])
        legacy.flush()
        parsed = json.loads(raw.getvalue().decode("cp1252"))
        self.assertEqual(code, 0)
        self.assertEqual(parsed["providers"]["claude"]["output"], "答案 😀")
        self.assertEqual(parsed["providers"]["grok"]["output"], "答案 😀")

    def test_offline_demo_has_explicit_partial_result(self):
        import demo_offline
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = demo_offline.main()
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result["providers"]["claude"]["status"], "complete_unreviewed")
        self.assertEqual(result["providers"]["grok"]["status"], "submission_uncertain")

    def test_build_requests_target_official_hosts_and_correct_auth(self):
        c = mvp.build_request("claude", "sample", "test-model", "fake-claude", 10)
        g = mvp.build_request("grok", "sample", "test-model", "fake-grok", 10)
        self.assertEqual(c[0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(c[1]["x-api-key"], "fake-claude")
        self.assertIn("anthropic-version", c[1])
        self.assertEqual(g[0], "https://api.x.ai/v1/responses")
        self.assertEqual(g[1]["Authorization"], "Bearer fake-grok")
        self.assertFalse(g[2]["store"])

    def test_complete_and_incomplete_responses_are_distinct(self):
        claude = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "answer"}]}
        grok = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}]}
        self.assertEqual(mvp.parse_response("claude", claude), "answer")
        self.assertEqual(mvp.parse_response("grok", grok), "answer")
        for provider, body in (("claude", {**claude, "stop_reason": "max_tokens"}),
                               ("grok", {**grok, "status": "incomplete"})):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                mvp.parse_response(provider, body)

    def test_network_failure_is_uncertain_and_not_retried(self):
        with mock.patch.object(mvp, "request_json", side_effect=TimeoutError("secret")) as request:
            result = mvp.run_provider("claude", "sample", "test-model", "fake-credential", 5, 10)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertNotIn("secret", str(result))

    def test_http_error_body_is_not_printed(self):
        error = urllib.error.HTTPError("https://example.test", 401, "secret", {}, io.BytesIO(b"secret"))
        with mock.patch.object(mvp, "request_json", side_effect=error) as request:
            result = mvp.run_provider("grok", "sample", "test-model", "fake-credential", 5, 10)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["http_status"], 401)
        self.assertNotIn("secret", str(result))

    def test_missing_config_is_not_submitted(self):
        with mock.patch.object(mvp, "request_json") as request:
            result = mvp.run_provider("claude", "sample", None, None, 5, 10)
        request.assert_not_called()
        self.assertEqual(result["status"], "not_submitted")

    def test_no_confirmation_never_reads_task_or_calls_network(self):
        with mock.patch.object(mvp, "request_json") as request, \
             mock.patch.object(mvp.getpass, "getpass") as ask, \
             mock.patch.object(sys_module(), "stdin") as input_stream, contextlib.redirect_stderr(io.StringIO()):
            result = mvp.main(["--ask-keys"])
        request.assert_not_called()
        ask.assert_not_called()
        input_stream.read.assert_not_called()
        input_stream.readline.assert_not_called()
        self.assertEqual(result, 2)

    def test_no_redirect_forwarding(self):
        self.assertIsNone(mvp.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test"))

    def test_two_independent_requests_and_partial_failure(self):
        calls = []
        def fake_request(url, headers, payload, timeout):
            calls.append((url, payload))
            if "anthropic" in url:
                return {"id": "c1", "model": "m1", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "claude answer"}]}
            raise TimeoutError("do not display")
        env = {"COLLAB_ANTHROPIC_KEY": "fake-claude", "COLLAB_XAI_KEY": "fake-grok",
               "CLAUDE_MODEL": "test-model", "GROK_MODEL": "test-model"}
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mvp, "request_json", side_effect=fake_request), \
             mock.patch.object(sys_module(), "stdin", io.StringIO("same task")), contextlib.redirect_stdout(stdout):
            code = mvp.main(["--confirm-paid", "--stdin-utf8"])
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["providers"]["claude"]["status"], "complete_unreviewed")
        self.assertEqual(result["providers"]["grok"]["status"], "submission_uncertain")
        self.assertNotIn("fake-claude", stdout.getvalue())
        self.assertNotIn("fake-grok", stdout.getvalue())
        payloads = [item[1] for item in calls]
        self.assertEqual([p.get("messages", p.get("input"))[0]["content"] for p in payloads], ["same task", "same task"])
        self.assertNotIn("claude answer", str(payloads))

    def test_standard_api_env_never_used_as_fallback(self):
        env = {"ANTHROPIC_API_KEY": "old-claude-value", "XAI_API_KEY": "old-grok-value",
               "CLAUDE_MODEL": "test-model", "GROK_MODEL": "test-model"}
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mvp, "request_json") as request, \
             mock.patch.object(sys_module(), "stdin", io.StringIO("same task")), contextlib.redirect_stdout(stdout):
            code = mvp.main(["--confirm-paid", "--stdin-utf8"])
        self.assertEqual(code, 2)
        request.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue())["providers"]["claude"]["status"], "not_submitted")
        self.assertNotIn("old-claude-value", stdout.getvalue())

    def test_dedicated_keys_take_precedence_and_do_not_change_standard_env(self):
        env = {"ANTHROPIC_API_KEY": "old-claude-value", "XAI_API_KEY": "old-grok-value",
               "COLLAB_ANTHROPIC_KEY": "new-claude-value", "COLLAB_XAI_KEY": "new-grok-value",
               "CLAUDE_MODEL": "test-model", "GROK_MODEL": "test-model"}
        calls = []
        def fake_request(url, headers, payload, timeout):
            calls.append(headers)
            if "anthropic" in url:
                return {"id": "c1", "model": "m1", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "new-grok-value"}]}
            return {"id": "g1", "model": "m2", "status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "new-claude-value"}]}]}
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mvp, "request_json", side_effect=fake_request), \
             mock.patch.object(sys_module(), "stdin", io.StringIO("same task")), contextlib.redirect_stdout(stdout):
            code = mvp.main(["--confirm-paid", "--stdin-utf8"])
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "old-claude-value")
            self.assertEqual(os.environ["XAI_API_KEY"], "old-grok-value")
        self.assertEqual(code, 0)
        self.assertEqual({h.get("x-api-key", h.get("Authorization")) for h in calls},
                         {"new-claude-value", "Bearer new-grok-value"})
        self.assertNotIn("new-claude-value", stdout.getvalue())
        self.assertNotIn("new-grok-value", stdout.getvalue())
        self.assertIn("[REDACTED]", stdout.getvalue())

    def test_task_containing_configured_key_sends_nothing(self):
        env = {"COLLAB_ANTHROPIC_KEY": "fake-secret-credential", "COLLAB_XAI_KEY": "another-secret",
               "CLAUDE_MODEL": "test-model", "GROK_MODEL": "test-model"}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mvp, "request_json") as request, \
             mock.patch.object(sys_module(), "stdin", io.StringIO("please analyze fake-secret-credential")), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = mvp.main(["--confirm-paid", "--stdin-utf8"])
        self.assertEqual(code, 2)
        request.assert_not_called()
        self.assertNotIn("fake-secret-credential", stdout.getvalue() + stderr.getvalue())

    def test_redaction_prefers_longest_overlapping_credential(self):
        self.assertEqual(mvp.redact_keys("prefix-abcdef-suffix", ["abc", "abcdef"]),
                         "prefix-[REDACTED]-suffix")

    def test_ask_keys_no_tty_fails_closed_before_network(self):
        env = {"CLAUDE_MODEL": "test-model", "GROK_MODEL": "test-model"}
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mvp, "request_json") as request, \
             mock.patch.object(mvp.getpass, "getpass") as ask, \
             mock.patch.object(sys_module(), "stdin", io.StringIO("same task")), contextlib.redirect_stderr(stderr):
            code = mvp.main(["--confirm-paid", "--stdin-utf8", "--ask-keys"])
        self.assertEqual(code, 2)
        ask.assert_not_called()
        request.assert_not_called()

    def test_ask_keys_warning_or_missing_input_fails_closed(self):
        env = {"CLAUDE_MODEL": "test-model", "GROK_MODEL": "test-model"}
        def emit_warning(*args):
            warnings.warn("private", getpass.GetPassWarning)
        for side_effect in (getpass.GetPassWarning("private"), EOFError("private"), emit_warning):
            stderr = io.StringIO()
            with self.subTest(error=type(side_effect).__name__), mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(mvp, "request_json") as request, \
                 mock.patch.object(mvp.getpass, "getpass", side_effect=side_effect), \
                 mock.patch.object(stderr, "isatty", return_value=True), \
                 mock.patch.object(sys_module(), "stdin", io.StringIO("same task")), contextlib.redirect_stderr(stderr):
                code = mvp.main(["--confirm-paid", "--stdin-utf8", "--ask-keys"])
            self.assertEqual(code, 2)
            request.assert_not_called()
            self.assertNotIn("private", stderr.getvalue())

    def test_ask_keys_does_not_use_or_mutate_environment(self):
        env = {"COLLAB_ANTHROPIC_KEY": "env-value-one", "COLLAB_XAI_KEY": "env-value-two",
               "CLAUDE_MODEL": "test-model", "GROK_MODEL": "test-model"}
        calls, stdout, stderr = [], io.StringIO(), io.StringIO()
        def fake_request(url, headers, payload, timeout):
            calls.append(headers)
            if "anthropic" in url:
                return {"id": "c1", "model": "m1", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "ok"}]}
            return {"id": "g1", "model": "m2", "status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mvp, "request_json", side_effect=fake_request), \
             mock.patch.object(mvp.getpass, "getpass", side_effect=["typed-one", "typed-two"]) as ask, \
             mock.patch.object(stderr, "isatty", return_value=True), \
             mock.patch.object(sys_module(), "stdin", io.StringIO("same task")), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = mvp.main(["--confirm-paid", "--stdin-utf8", "--ask-keys"])
            self.assertEqual(os.environ["COLLAB_ANTHROPIC_KEY"], "env-value-one")
        self.assertEqual(code, 0)
        self.assertEqual(ask.call_count, 2)
        self.assertEqual({h.get("x-api-key", h.get("Authorization")) for h in calls},
                         {"typed-one", "Bearer typed-two"})
        self.assertNotIn("typed-one", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("typed-two", stdout.getvalue() + stderr.getvalue())

    def test_interactive_task_single_line_proceeds_to_hidden_keys(self):
        class InteractiveInput(io.StringIO):
            def isatty(self): return True
            def read(self, *args): raise AssertionError("interactive input must not wait for EOF")
        env = {"CLAUDE_MODEL": "test-model", "GROK_MODEL": "test-model"}
        calls, stdout, stderr = [], io.StringIO(), io.StringIO()
        def fake_request(url, headers, payload, timeout):
            calls.append(payload)
            if "anthropic" in url:
                return {"id": "c1", "model": "m1", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "ok"}]}
            return {"id": "g1", "model": "m2", "status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mvp, "request_json", side_effect=fake_request), \
             mock.patch.object(mvp.getpass, "getpass", side_effect=["typed-one", "typed-two"]) as ask, \
             mock.patch.object(stderr, "isatty", return_value=True), \
             mock.patch.object(sys_module(), "stdin", InteractiveInput("task text\n")), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = mvp.main(["--confirm-paid", "--stdin-utf8", "--ask-keys"])
        self.assertEqual(code, 0)
        self.assertEqual(ask.call_count, 2)
        self.assertEqual([p.get("messages", p.get("input"))[0]["content"] for p in calls],
                         ["task text\n", "task text\n"])
        self.assertIn("one line", stderr.getvalue())
        self.assertNotIn("typed-one", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("typed-two", stdout.getvalue() + stderr.getvalue())

    def test_piped_task_preserves_multiple_lines(self):
        class PipedInput(io.StringIO):
            def isatty(self): return False
            def readline(self, *args): raise AssertionError("piped input should use read")
        env = {"COLLAB_ANTHROPIC_KEY": "fake-claude", "COLLAB_XAI_KEY": "fake-grok",
               "CLAUDE_MODEL": "test-model", "GROK_MODEL": "test-model"}
        calls, stdout = [], io.StringIO()
        def fake_request(url, headers, payload, timeout):
            calls.append(payload)
            if "anthropic" in url:
                return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}
            return {"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(mvp, "request_json", side_effect=fake_request), \
             mock.patch.object(sys_module(), "stdin", PipedInput("first\nsecond\n")), contextlib.redirect_stdout(stdout):
            code = mvp.main(["--confirm-paid", "--stdin-utf8"])
        self.assertEqual(code, 0)
        self.assertEqual([p.get("messages", p.get("input"))[0]["content"] for p in calls],
                         ["first\nsecond\n", "first\nsecond\n"])


def sys_module():
    import sys
    return sys


if __name__ == "__main__":
    unittest.main()

