"""Synthetic interruption and direct-call checks; no provider is contacted."""
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import collaborate as app


class InterruptTests(unittest.TestCase):
    def test_direct_provider_rejects_other_prefix_before_any_request(self):
        for provider, key in (("claude", "xai-fake-secret"), ("grok", "sk-ant-fake-secret")):
            with self.subTest(provider=provider), mock.patch.object(app, "request_json") as request:
                result = app.run_provider(provider, "task", "model", key, 1, 1)
            self.assertEqual(result, {"status": "not_submitted", "reason": "credential_for_other_provider"})
            request.assert_not_called()
            self.assertNotIn("fake-secret", json.dumps(result))

    def test_direct_provider_rejects_invalid_key_before_any_request(self):
        for provider in ("claude", "grok"):
            with self.subTest(provider=provider), mock.patch.object(app, "request_json") as request:
                result = app.run_provider(provider, "task", "model", "bad key", 1, 1)
            self.assertEqual(result, {"status": "not_submitted", "reason": "missing_or_invalid_credential"})
            request.assert_not_called()

    def test_interrupt_keeps_completed_result_and_leaves_other_unknown(self):
        env = {"CLAUDE_MODEL": "fake-model", "GROK_MODEL": "fake-model",
               "COLLAB_ANTHROPIC_KEY": "sk-ant-synthetic", "COLLAB_XAI_KEY": "xai-synthetic"}
        pending, release, returned = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def provider(name, *_args):
            calls.append(name)
            if name == "claude":
                return {"status": "complete_unreviewed", "output": "hello sk-ant-synthetic"}
            pending.set()
            try:
                release.wait(5)
                return {"status": "complete_unreviewed", "output": "late"}
            finally:
                returned.set()

        def interrupt_after_claude(workers):
            workers[0].join(timeout=3)
            self.assertFalse(workers[0].is_alive())
            self.assertTrue(pending.wait(3))
            raise KeyboardInterrupt

        out = io.StringIO()
        try:
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdin", io.StringIO("task")), \
                 mock.patch.object(app, "run_provider", side_effect=provider), \
                 mock.patch.object(app, "_wait_for_workers", side_effect=interrupt_after_claude), \
                 mock.patch("sys.stdout", out):
                code = app.main(["--confirm-paid", "--stdin-utf8"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 130)
            self.assertEqual(payload["providers"]["claude"]["status"], "complete_unreviewed")
            self.assertNotIn("sk-ant-synthetic", out.getvalue())
            self.assertEqual(payload["providers"]["grok"]["status"], "submission_uncertain")
            self.assertEqual(payload["providers"]["grok"]["retry"], "not_attempted")
            self.assertIn("remote work may continue", payload["note"])
            self.assertCountEqual(calls, ["claude", "grok"])
        finally:
            release.set()
            self.assertTrue(returned.wait(3))

    def test_interrupted_cli_exits_without_waiting_for_slow_worker(self):
        # The child simulates a KeyboardInterrupt in the main thread; its
        # synthetic Grok worker sleeps for 15 seconds. No network is used.
        script = r'''
import io, os, sys, time
import collaborate as m
os.environ.update(CLAUDE_MODEL="fake", GROK_MODEL="fake", COLLAB_ANTHROPIC_KEY="sk-ant-fake", COLLAB_XAI_KEY="xai-fake")
sys.stdin = io.StringIO("task")
def fake(name, *args):
    if name == "claude": return {"status": "complete_unreviewed", "output": "done"}
    time.sleep(15)
    return {"status": "complete_unreviewed", "output": "late"}
def interrupt(workers):
    workers[0].join(3)
    raise KeyboardInterrupt
m.run_provider = fake
m._wait_for_workers = interrupt
sys.exit(m.main(["--confirm-paid", "--stdin-utf8"]))
'''
        started = time.monotonic()
        child = subprocess.run([sys.executable, "-B", "-c", script], cwd=str(Path(__file__).parent),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(child.returncode, 130)
        result = json.loads(child.stdout.decode("ascii"))
        self.assertEqual(result["providers"]["claude"]["output"], "done")
        self.assertEqual(result["providers"]["grok"]["status"], "submission_uncertain")
        self.assertEqual(child.stderr, b"")

    def test_late_worker_cannot_change_interruption_snapshot(self):
        env = {"CLAUDE_MODEL": "fake", "GROK_MODEL": "fake",
               "COLLAB_ANTHROPIC_KEY": "sk-ant-synthetic", "COLLAB_XAI_KEY": "xai-synthetic"}
        pending, release = threading.Event(), threading.Event()
        workers_seen = []
        real_redact = app.redact_keys

        def provider(name, *_args):
            if name == "claude":
                return {"status": "complete_unreviewed", "output": "first"}
            pending.set()
            if not release.wait(3):
                raise TimeoutError("synthetic late worker")
            return {"status": "complete_unreviewed", "output": "late"}

        def interrupt(workers):
            workers_seen.extend(workers)
            workers[0].join(3)
            self.assertFalse(workers[0].is_alive())
            self.assertTrue(pending.wait(3))
            raise KeyboardInterrupt

        def release_after_snapshot(payload, secrets):
            release.set()
            workers_seen[1].join(3)
            self.assertFalse(workers_seen[1].is_alive())
            return real_redact(payload, secrets)

        out = io.StringIO()
        try:
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdin", io.StringIO("task")), \
                 mock.patch.object(app, "run_provider", side_effect=provider), \
                 mock.patch.object(app, "_wait_for_workers", side_effect=interrupt), \
                 mock.patch.object(app, "redact_keys", side_effect=release_after_snapshot), \
                 mock.patch("sys.stdout", out):
                code = app.main(["--confirm-paid", "--stdin-utf8"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 130)
            self.assertEqual(payload["providers"]["claude"]["output"], "first")
            self.assertEqual(payload["providers"]["grok"]["status"], "submission_uncertain")
            self.assertNotIn("late", out.getvalue())
        finally:
            release.set()

    def test_second_thread_start_failure_preserves_completed_first_result(self):
        env = {"CLAUDE_MODEL": "fake", "GROK_MODEL": "fake",
               "COLLAB_ANTHROPIC_KEY": "sk-ant-synthetic", "COLLAB_XAI_KEY": "xai-synthetic"}
        real_start = threading.Thread.start
        starts = []

        def controlled_start(worker):
            starts.append(worker)
            if len(starts) == 2:
                raise RuntimeError("private startup detail")
            real_start(worker)
            worker.join(3)
            self.assertFalse(worker.is_alive())

        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdin", io.StringIO("task")), \
             mock.patch.object(app, "run_provider", return_value={"status": "complete_unreviewed", "output": "first"}) as run, \
             mock.patch.object(threading.Thread, "start", controlled_start), mock.patch("sys.stdout", out):
            code = app.main(["--confirm-paid", "--stdin-utf8"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["providers"]["claude"]["output"], "first")
        self.assertEqual(payload["providers"]["grok"]["status"], "submission_uncertain")
        self.assertEqual(payload["providers"]["grok"]["retry"], "not_attempted")
        self.assertNotIn("private", out.getvalue())
        run.assert_called_once()

    def test_second_start_failure_waits_for_first_inflight_result(self):
        env = {"CLAUDE_MODEL": "fake", "GROK_MODEL": "fake",
               "COLLAB_ANTHROPIC_KEY": "sk-ant-synthetic", "COLLAB_XAI_KEY": "xai-synthetic"}
        real_start, real_wait = threading.Thread.start, app._wait_for_workers
        started, release = threading.Event(), threading.Event()
        starts, calls, first_started = [], [], []

        def provider(name, *_args):
            calls.append(name)
            started.set()
            if not release.wait(3):
                raise TimeoutError("synthetic")
            return {"status": "complete_unreviewed", "output": "first result"}

        def start(worker):
            starts.append(worker)
            if len(starts) == 2:
                first_started.append(started.wait(3))
                raise RuntimeError("private startup detail")
            real_start(worker)

        def wait(workers):
            release.set()
            real_wait(workers)

        out = io.StringIO()
        try:
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdin", io.StringIO("task")), \
                 mock.patch.object(app, "run_provider", side_effect=provider), \
                 mock.patch.object(threading.Thread, "start", start), \
                 mock.patch.object(app, "_wait_for_workers", side_effect=wait), mock.patch("sys.stdout", out):
                code = app.main(["--confirm-paid", "--stdin-utf8"])
            payload = json.loads(out.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["providers"]["claude"]["output"], "first result")
            self.assertEqual(payload["providers"]["grok"]["status"], "submission_uncertain")
            self.assertEqual(calls, ["claude"])
            self.assertEqual(first_started, [True])
            self.assertNotIn("private", out.getvalue())
            self.assertNotIn("synthetic", out.getvalue())
        finally:
            release.set()
            if starts:
                starts[0].join(3)

    def test_interrupt_while_waiting_after_start_failure(self):
        env = {"CLAUDE_MODEL": "fake", "GROK_MODEL": "fake",
               "COLLAB_ANTHROPIC_KEY": "sk-ant-synthetic", "COLLAB_XAI_KEY": "xai-synthetic"}
        real_start = threading.Thread.start
        started, release = threading.Event(), threading.Event()
        starts, first_started = [], []

        def provider(*_args):
            started.set()
            release.wait(3)
            return {"status": "complete_unreviewed", "output": "late"}

        def start(worker):
            starts.append(worker)
            if len(starts) == 2:
                first_started.append(started.wait(3))
                raise RuntimeError("private")
            real_start(worker)

        out = io.StringIO()
        try:
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdin", io.StringIO("task")), \
                 mock.patch.object(app, "run_provider", side_effect=provider), \
                 mock.patch.object(threading.Thread, "start", start), \
                 mock.patch.object(app, "_wait_for_workers", side_effect=KeyboardInterrupt), mock.patch("sys.stdout", out):
                code = app.main(["--confirm-paid", "--stdin-utf8"])
            self.assertEqual(code, 130)
            self.assertEqual(first_started, [True])
            self.assertEqual(json.loads(out.getvalue())["providers"]["claude"]["status"], "submission_uncertain")
            self.assertNotIn("private", out.getvalue())
        finally:
            release.set()
            if starts:
                starts[0].join(3)

    def test_start_exception_after_thread_started_preserves_both_results(self):
        env = {"CLAUDE_MODEL": "fake", "GROK_MODEL": "fake",
               "COLLAB_ANTHROPIC_KEY": "sk-ant-synthetic", "COLLAB_XAI_KEY": "xai-synthetic"}
        real_start = threading.Thread.start
        starts, calls = [], []

        def start(worker):
            starts.append(worker)
            real_start(worker)
            if len(starts) == 2:
                raise RuntimeError("synthetic failure after launch")

        def provider(name, *_args):
            calls.append(name)
            return {"status": "complete_unreviewed", "output": name}

        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdin", io.StringIO("task")), \
             mock.patch.object(app, "run_provider", side_effect=provider), \
             mock.patch.object(threading.Thread, "start", start), mock.patch("sys.stdout", out):
            code = app.main(["--confirm-paid", "--stdin-utf8"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["providers"]["claude"]["output"], "claude")
        self.assertEqual(payload["providers"]["grok"]["output"], "grok")
        self.assertCountEqual(calls, ["claude", "grok"])

    def test_interrupt_during_second_start_preserves_completed_first_result(self):
        env = {"CLAUDE_MODEL": "fake", "GROK_MODEL": "fake",
               "COLLAB_ANTHROPIC_KEY": "sk-ant-synthetic", "COLLAB_XAI_KEY": "xai-synthetic"}
        real_start = threading.Thread.start
        starts = []

        def interrupted_start(worker):
            starts.append(worker)
            if len(starts) == 2:
                raise KeyboardInterrupt
            real_start(worker)
            worker.join(3)
            self.assertFalse(worker.is_alive())

        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdin", io.StringIO("task")), \
             mock.patch.object(app, "run_provider", return_value={"status": "complete_unreviewed", "output": "first"}) as run, \
             mock.patch.object(threading.Thread, "start", interrupted_start), mock.patch("sys.stdout", out):
            code = app.main(["--confirm-paid", "--stdin-utf8"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 130)
        self.assertEqual(payload["providers"]["claude"]["output"], "first")
        self.assertEqual(payload["providers"]["grok"]["status"], "submission_uncertain")
        self.assertEqual(payload["providers"]["grok"]["retry"], "not_attempted")
        run.assert_called_once()

    def test_unexpected_worker_error_is_not_exposed_or_retried(self):
        env = {"CLAUDE_MODEL": "fake", "GROK_MODEL": "fake",
               "COLLAB_ANTHROPIC_KEY": "sk-ant-synthetic", "COLLAB_XAI_KEY": "xai-synthetic"}
        calls = []

        def provider(name, *_args):
            calls.append(name)
            if name == "grok":
                raise RuntimeError("private worker detail")
            return {"status": "complete_unreviewed", "output": "first"}

        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdin", io.StringIO("task")), \
             mock.patch.object(app, "run_provider", side_effect=provider), mock.patch("sys.stdout", out):
            code = app.main(["--confirm-paid", "--stdin-utf8"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["providers"]["grok"]["status"], "submission_uncertain")
        self.assertEqual(payload["providers"]["grok"]["reason"], "worker_error")
        self.assertNotIn("private", out.getvalue())
        self.assertCountEqual(calls, ["claude", "grok"])

    @unittest.skipIf(os.name == "nt", "POSIX SIGINT test")
    def test_real_sigint_returns_completed_json_without_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = str(Path(tmp) / "ready")
            script = r'''
import io, os, sys, threading, time
import collaborate as m
os.environ.update(CLAUDE_MODEL="fake", GROK_MODEL="fake", COLLAB_ANTHROPIC_KEY="sk-ant-fake", COLLAB_XAI_KEY="xai-fake")
sys.stdin = io.StringIO("task")
grok_started = threading.Event()
def fake(name, *args):
    if name == "claude": return {"status": "complete_unreviewed", "output": "done"}
    grok_started.set()
    time.sleep(15)
    return {"status": "complete_unreviewed", "output": "late"}
real_wait = m._wait_for_workers
def ready_then_wait(workers):
    workers[0].join(5)
    if workers[0].is_alive() or not grok_started.wait(5): raise RuntimeError("synthetic workers not ready")
    with open(os.environ["SYNTHETIC_MARKER"], "w") as stream: stream.write("ready")
    real_wait(workers)
m.run_provider = fake
m._wait_for_workers = ready_then_wait
sys.exit(m.main(["--confirm-paid", "--stdin-utf8"]))
'''
            env = dict(os.environ, SYNTHETIC_MARKER=marker)
            proc = subprocess.Popen([sys.executable, "-B", "-c", script], cwd=str(Path(__file__).parent),
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while not Path(marker).exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(Path(marker).exists())
                proc.send_signal(signal.SIGINT)
                out, err = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 130)
                data = json.loads(out.decode("ascii"))
                self.assertEqual(data["providers"]["claude"]["output"], "done")
                self.assertEqual(data["providers"]["grok"]["status"], "submission_uncertain")
                self.assertEqual(err, b"")
            finally:
                if proc.poll() is None:
                    proc.kill()  # only the synthetic child created above
                    proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
