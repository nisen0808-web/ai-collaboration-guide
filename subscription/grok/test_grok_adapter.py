"""Synthetic subprocess fixtures; no official client or credentials used."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import grok_adapter as subject


class FakeChild:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        kwargs["stdout"].write(b"synthetic answer")
        kwargs["stdout"].flush()

    def wait(self, timeout):
        return 0


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.cli = base / "fake-grok"
        self.cli.touch()
        self.profile = base / "fake-profile"
        self.profile.mkdir()
        self.instance = subject.GrokAdapter(str(self.cli), "example-model", "high", str(self.profile), 2)
        self.report = {name: [] for name in subject._INSPECT_KEYS}
        original_mkdtemp = tempfile.mkdtemp
        self.temp_patcher = mock.patch.object(subject.tempfile, "mkdtemp",
            side_effect=lambda prefix: original_mkdtemp(prefix=prefix, dir=self.temp.name))
        self.temp_patcher.start()

    def tearDown(self):
        self.temp_patcher.stop()
        self.temp.cleanup()

    def completed(self, job_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.instance.poll(job_id)
            if state["status"] not in {"queued", "running"}:
                return state
            time.sleep(0.01)
        self.fail("fake worker did not complete")

    def available(self, wanted):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.instance.status()["local_available_slots"] == wanted:
                return
            time.sleep(0.01)
        self.fail("local worker slot was not released")

    def test_normal_one_request_never_inherits_api_keys(self):
        seen = []
        def fake_inspect(argv, **kwargs):
            seen.append((argv, kwargs))
            return mock.Mock(returncode=0, stdout=json.dumps(self.report).encode())
        def fake_child(argv, **kwargs):
            seen.append((argv, kwargs))
            return FakeChild(argv, **kwargs)
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "dummy-key", "GROK_CONFIG": "dummy-override"}), \
             mock.patch.object(subject.subprocess, "run", side_effect=fake_inspect), \
             mock.patch.object(subject.subprocess, "Popen", side_effect=fake_child):
            result = self.instance("synthetic question", "task-1")
            self.assertEqual(result["status"], "queued")
            state = self.completed(result["job_id"])
            self.assertEqual(state["status"], "succeeded")
            self.assertEqual(state["output"], "synthetic answer")
            self.assertFalse(state["billing_verified"])
            self.assertFalse(state["quality_verified"])
            self.assertEqual(len(seen), 2)
            for argv, kwargs in seen:
                self.assertFalse(kwargs["shell"])
                self.assertNotIn("XAI_API_KEY", kwargs["env"])
                self.assertNotIn("GROK_CONFIG", kwargs["env"])
                self.assertEqual(kwargs["env"]["GROK_HOME"], str(self.profile))
                self.assertNotIn("synthetic question", repr(argv))
            self.assertEqual(seen[1][0][seen[1][0].index("--model") + 1], "example-model")
            self.assertEqual(self.instance.submit("synthetic question", "task-1")["job_id"], result["job_id"])
            self.assertEqual(self.instance.submit("different", "task-1")["reason"], "task_id_conflict")
            self.assertEqual(len(seen), 2)

    def test_unrecognized_or_nonempty_inspection_never_generates(self):
        for report in ({}, dict(self.report, plugins=["synthetic"])):
            with self.subTest(report=report), \
                 mock.patch.object(subject.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(report).encode())), \
                 mock.patch.object(subject.subprocess, "Popen", side_effect=AssertionError("must not start")):
                task_id = str(len(report)) + str(bool(report))
                result = self.instance.submit("synthetic", task_id)
                state = self.completed(result["job_id"])
                self.assertEqual(state["status"], "failed")
                self.assertFalse(state["submitted"])
                self.available(2)

    def test_timeout_is_uncertain_then_original_result_is_recovered(self):
        waiting_for_original = threading.Event()
        allow_exit = threading.Event()
        self.addCleanup(allow_exit.set)
        class TimedOut(FakeChild):
            def wait(self, timeout=None):
                if timeout is not None:
                    raise subject.subprocess.TimeoutExpired("fake", timeout)
                waiting_for_original.set()
                if not allow_exit.wait(3):
                    raise RuntimeError("test did not release fake child")
                return 0
        with mock.patch.object(subject.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(self.report).encode())), \
             mock.patch.object(subject.subprocess, "Popen", side_effect=TimedOut) as started:
            result = self.instance.submit("synthetic", "timeout")
            self.assertTrue(waiting_for_original.wait(3))
            state = self.instance.poll(result["job_id"])
            self.assertEqual(state["status"], "submission_uncertain")
            self.assertEqual(self.instance.status()["local_active_jobs"], 1)
            self.assertEqual(started.call_count, 1)
            self.assertTrue(self.instance.submit("synthetic", "timeout")["replayed"])
            self.assertEqual(started.call_count, 1)
            allow_exit.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = self.instance.poll(result["job_id"])
                if state["status"] != "submission_uncertain":
                    break
                time.sleep(0.01)
            self.assertEqual(state["status"], "succeeded")
            self.assertEqual(state["output"], "synthetic answer")
            self.assertIsNone(state["error"])
            self.assertEqual(started.call_count, 1)
            self.available(2)
        # Mock wrote files but no real subprocess exists. The workspace is a
        # child of the test-owned temporary directory and is removed by tearDown.

    def test_login_is_explicit_and_uses_official_cli_without_key(self):
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "dummy"}), \
             mock.patch.object(subject.subprocess, "call", return_value=0) as login:
            self.assertEqual(self.instance.login(), 0)
            args, kwargs = login.call_args
            self.assertEqual(args[0], [str(self.cli), "login"])
            self.assertNotIn("XAI_API_KEY", kwargs["env"])

    def test_unknown_job_is_uncertain(self):
        self.assertEqual(self.instance.poll("unknown")["status"], "submission_uncertain")

    def test_invalid_unicode_is_not_submitted(self):
        with mock.patch.object(subject.subprocess, "Popen", side_effect=AssertionError("must not start")):
            self.assertEqual(self.instance.submit("\ud800", "test")["status"], "not_submitted")

    def test_local_two_job_limit_replay_and_released_capacity(self):
        release = threading.Event()
        both_started = threading.Event()
        launched = []
        guard = threading.Lock()
        self.addCleanup(release.set)

        class Controlled(FakeChild):
            def __init__(self, argv, **kwargs):
                super().__init__(argv, **kwargs)
                with guard:
                    launched.append(argv)
                    if len(launched) == 2:
                        both_started.set()

            def wait(self, timeout=None):
                if not release.wait(3):
                    raise RuntimeError("test did not release fake child")
                return 0

        with mock.patch.object(subject.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(self.report).encode())), \
             mock.patch.object(subject.subprocess, "Popen", side_effect=Controlled):
            first = self.instance.submit("first", "first-key")
            second = self.instance.submit("second", "second-key")
            self.assertEqual(first["status"], "queued")
            self.assertEqual(second["status"], "queued")
            self.assertEqual(self.instance.status()["local_active_jobs"], 2)
            self.assertEqual(self.instance.status()["local_available_slots"], 0)
            self.assertFalse(self.instance.status()["official_quota_verified"])
            self.assertEqual(self.instance.submit("third", "third-key"),
                             {"status": "not_submitted", "reason": "provider_busy"})
            self.assertEqual(self.instance.submit("first", "first-key")["job_id"], first["job_id"])
            self.assertTrue(both_started.wait(3))
            self.assertEqual(len(launched), 2)
            release.set()
            self.assertEqual(self.completed(first["job_id"])["status"], "succeeded")
            self.assertEqual(self.completed(second["job_id"])["status"], "succeeded")
            self.available(2)
            third = self.instance.submit("third", "third-key")
            self.assertEqual(third["status"], "queued")
            self.assertEqual(self.completed(third["job_id"])["status"], "succeeded")
            self.available(2)
            self.assertEqual(len(launched), 3)
            replay = self.instance.submit("first", "first-key")
            self.assertEqual(replay["status"], "not_submitted")
            self.assertEqual(replay["reason"], "already_submitted")
            self.assertEqual(replay["job_id"], first["job_id"])
            self.assertEqual(len(launched), 3)

    def test_local_concurrency_range_is_validated(self):
        for invalid in (0, 9, True, "2"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                subject.GrokAdapter(str(self.cli), "example-model", "high", str(self.profile),
                                    max_concurrency=invalid)

    def test_wait_error_keeps_unconfirmed_child_slot_occupied(self):
        self.instance = subject.GrokAdapter(str(self.cli), "example-model", "high", str(self.profile),
                                            timeout_seconds=2, max_concurrency=1)
        class WaitError(FakeChild):
            def wait(self, timeout=None):
                raise OSError("synthetic wait failure")
        with mock.patch.object(subject.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(self.report).encode())), \
             mock.patch.object(subject.subprocess, "Popen", side_effect=WaitError) as started:
            result = self.instance.submit("synthetic", "wait-error")
            state = self.completed(result["job_id"])
            self.assertEqual(state["status"], "submission_uncertain")
            self.assertEqual(self.instance.status()["local_available_slots"], 0)
            self.assertEqual(self.instance.submit("another", "another-key")["reason"], "provider_busy")
            self.assertEqual(started.call_count, 1)


if __name__ == "__main__":
    unittest.main()
