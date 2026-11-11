import io
import contextlib
import json
import os
import threading
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
import urllib.error
from unittest import mock

import collaborate as mod

class APITests(unittest.TestCase):
    def run_response(self, provider, response):
        with mock.patch.object(mod, "request_json", return_value=response):
            return mod.run_provider(provider, "test", "valid-model", "key", 1, 10)

    def test_claude_truncated_preserves_text_id_and_usage(self):
        result = self.run_response("claude", ({"id": "msg_1", "stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}], "usage": {"input_tokens": 5, "output_tokens": 10, "unknown": "secret"}}, "req_1"))
        self.assertEqual((result["status"], result["output"], result["response_id"], result["request_id"]), ("truncated", "partial", "msg_1", "req_1"))
        self.assertEqual(result["usage"], {"input_tokens": 5, "output_tokens": 10})

    def test_claude_refusal_and_missing_status(self):
        result = self.run_response("claude", {"stop_reason": "refusal", "content": [{"type": "text", "text": "cannot"}]})
        self.assertEqual((result["status"], result["output"]), ("refused", "cannot"))
        self.assertEqual(self.run_response("claude", {"content": []})["status"], "invalid_response")

    def test_grok_truncated_usage_and_refusal(self):
        response = {"id": "resp_1", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}], "usage": {"output_tokens_details": {"reasoning_tokens": 9, "private": 77}, "cost_in_usd_ticks": 12}}
        result = self.run_response("grok", response)
        self.assertEqual((result["status"], result["output"]), ("truncated", "partial"))
        self.assertEqual(result["usage"], {"output_tokens_details": {"reasoning_tokens": 9}, "cost_in_usd_ticks": 12})
        response["status"] = "completed"
        response["output"][0]["content"] = [{"type": "refusal", "refusal": "no"}]
        self.assertEqual(self.run_response("grok", response)["status"], "refused")

    def test_failure_does_not_reveal_error_body(self):
        error = urllib.error.HTTPError("https://example.invalid", 504, "do not expose", {"x-request-id": "req-42"}, io.BytesIO(b"secret error body"))
        with mock.patch.object(mod, "request_json", side_effect=error):
            result = mod.run_provider("grok", "test", "model", "key", 1, 10)
        self.assertEqual((result["status"], result["request_id"]), ("submission_uncertain", "req-42"))
        self.assertNotIn("secret", str(result))
        error.code = 400
        error.headers = {"request-id": "bad header with whitespace"}
        with mock.patch.object(mod, "request_json", side_effect=error):
            result = mod.run_provider("grok", "test", "model", "key", 1, 10)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["request_id"])

    def test_real_request_json_and_invalid_json_keep_request_id(self):
        class Response:
            headers = {"request-id": "req_1"}
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return self.body[:size]
        opener = mock.Mock()
        opener.open.return_value = Response(b'{"id":"msg_1"}')
        with mock.patch.object(mod.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(mod.request_json("https://example.invalid", {}, {}, 1), ({"id": "msg_1"}, "req_1"))
            build.assert_called_once_with(mod.NoRedirect)
            opener.open.return_value = Response(b"not-json-secret")
            with self.assertRaises(mod.InvalidProviderResponse) as raised:
                mod.request_json("https://example.invalid", {}, {}, 1)
            self.assertEqual((raised.exception.reason, raised.exception.request_id), ("invalid_json", "req_1"))

    def test_usage_allowlist_and_strict_compatibility(self):
        self.assertIsNone(mod.sanitized_usage({"input_tokens": True, "output_tokens": -1, "arbitrary": 4}))
        self.assertEqual(mod.parse_response("claude", {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}), "ok")
        with self.assertRaises(ValueError):
            mod.parse_response("claude", {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]})

    def test_no_confirmation_does_not_read_stdin(self):
        class NoRead(io.StringIO):
            def read(self, *args): raise AssertionError("stdin was read")
            def readline(self, *args): raise AssertionError("stdin was read")
        with mock.patch.object(mod.sys, "stdin", NoRead("unused")), \
             mock.patch.object(mod, "request_json") as request, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mod.main([]), 2)
        request.assert_not_called()

    def test_too_long_task_never_reaches_network(self):
        task = "a" * (mod.MAX_TASK_CHARS + 1)
        with mock.patch.object(mod.sys, "stdin", io.StringIO(task)), \
             mock.patch.object(mod, "request_json") as request, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mod.main(["--confirm-paid", "--stdin-utf8"]), 2)
        request.assert_not_called()

    def test_invalid_json_reports_request_id_without_body(self):
        err = mod.InvalidProviderResponse("invalid_json", "req_1")
        with mock.patch.object(mod, "request_json", side_effect=err) as request:
            result = mod.run_provider("claude", "task", "model", "fake-key", 5, 20)
        request.assert_called_once()
        self.assertEqual(result["status"], "invalid_response")
        self.assertEqual(result["request_id"], "req_1")
        self.assertNotIn("fake-key", json.dumps(result))

    def test_actual_http_307_never_forwards_header(self):
        # This tests a local fake server only; nothing leaves loopback.
        events = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                events.append((self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/source":
                    self.send_response(307)
                    self.send_header("Location", "http://127.0.0.1:%s/sink" % self.server.server_port)
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
            def log_message(self, *args): pass
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError as exc:
            self.skipTest("Loopback unavailable: " + type(exc).__name__)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(mod.urllib.request, "getproxies", return_value={}):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    mod.request_json("http://127.0.0.1:%s/source" % server.server_port,
                                     {"Authorization": "Bearer fake-test-only"}, {"task": "synthetic"}, 3)
            self.assertEqual(raised.exception.code, 307)
            self.assertEqual(events, [("/source", "Bearer fake-test-only")])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_utf8_task_file_main_sends_same_task_to_both_mocks(self):
        expected = "中文任务\n第二行 🧪\n"
        env = {"COLLAB_ANTHROPIC_KEY": "fake-a", "COLLAB_XAI_KEY": "fake-b",
               "CLAUDE_MODEL": "mock-model", "GROK_MODEL": "mock-model"}
        received = []
        def fake_request(url, headers, payload, timeout):
            received.append(payload.get("messages", payload.get("input"))[0]["content"])
            if "anthropic" in url:
                return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}
            return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            task_file = os.path.join(tmp, "task.txt")
            with open(task_file, "wb") as handle:
                handle.write(expected.encode("utf-8-sig"))
            output = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(mod, "request_json", side_effect=fake_request), \
                 contextlib.redirect_stdout(output):
                code = mod.main(["--confirm-paid", "--task-file", task_file])
        self.assertEqual(code, 0)
        self.assertEqual(received, [expected, expected])
        self.assertTrue(all(x["status"] == "complete_unreviewed" for x in json.loads(output.getvalue())["providers"].values()))

    def test_invalid_timeout_and_token_limits_never_read_or_submit(self):
        class NoRead(io.StringIO):
            def read(self, *args): raise AssertionError("stdin was read")
            def readline(self, *args): raise AssertionError("stdin was read")
        for flag, value in (("--timeout", "0"), ("--timeout", "301"),
                            ("--max-tokens", "0"), ("--max-tokens", "4097")):
            with self.subTest(flag=flag, value=value), \
                 mock.patch.object(mod.sys, "stdin", NoRead("unused")), \
                 mock.patch.object(mod, "request_json") as request, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(mod.main(["--confirm-paid", flag, value]), 2)
                request.assert_not_called()

    def test_oversized_response_preserves_request_id_without_body(self):
        class Response:
            headers = {"request-id": "req_test"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b"x" * size
        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch.object(mod.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(mod.InvalidProviderResponse) as raised:
                mod.request_json("https://example.invalid", {}, {}, 1)
        self.assertEqual((raised.exception.reason, raised.exception.request_id), ("response_too_large", "req_test"))
        with mock.patch.object(mod, "request_json", side_effect=raised.exception):
            result = mod.run_provider("claude", "task", "model", "fake-key", 5, 20)
        self.assertEqual((result["status"], result["reason"], result["request_id"]),
                         ("invalid_response", "response_too_large", "req_test"))
        self.assertNotIn("fake-key", json.dumps(result))

if __name__ == "__main__":
    unittest.main()
