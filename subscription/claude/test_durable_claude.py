"""Offline durable-job checks; fake local Python child, no Claude or network."""
import json
import hashlib
from contextlib import closing, contextmanager
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import durable_claude as durable


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / "private-state"
        self.profile = root / "profile"
        self.work = root / "work"
        for p in (self.state, self.profile, self.work):
            p.mkdir()
        self.cli = root / "synthetic-cli"
        self.args = (str(self.cli), str(self.profile), str(self.work), str(self.state), "claude-synthetic", "medium")

    def adapter(self, **kwargs):
        return durable.DurableClaudeAdapter(*self.args, enabled=True, **kwargs)

    def insert(self, job_id, status):
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            with con:
                con.execute("INSERT INTO jobs VALUES (?,?,?,NULL,NULL,NULL)", (job_id, "digest", status))

    def test_reserved_or_unknown_job_is_uncertain_and_never_resubmitted(self):
        a = self.adapter()
        self.insert("existing", "reserved")
        self.assertEqual(a.poll("existing")["status"], "submission_uncertain")
        self.assertEqual(a.poll("missing")["status"], "submission_uncertain")

    def test_local_capacity_is_serialized_and_busy_is_not_submitted(self):
        a = self.adapter(max_concurrency=1)
        self.insert("occupied", "submission_uncertain")
        with patch("durable_claude.subprocess.Popen", side_effect=AssertionError("started")):
            self.assertEqual(a.submit("task", "other"), {"status": "not_submitted", "reason": "provider_busy"})

    def test_success_without_result_file_is_uncertain(self):
        a = self.adapter()
        self.insert("missing-output", "succeeded")
        self.assertEqual(a.poll("missing-output")["status"], "submission_uncertain")

    def test_status_distinguishes_unknown_from_recent_heartbeat_without_releasing(self):
        a = self.adapter(max_concurrency=2)
        self.insert("unknown", "submission_uncertain")
        self.insert("tracked", "running")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            with con:
                con.execute("UPDATE jobs SET heartbeat=? WHERE id='tracked'", (time.time(),))
        status = a.status()
        self.assertEqual(status["local_active"], 2)
        self.assertEqual(status["local_uncertain"], 1)
        self.assertEqual(status["local_observed_inflight"], 1)
        self.assertEqual(a.submit("other", "new")["reason"], "provider_busy")

    def test_find_job_is_read_only_and_does_not_launch(self):
        a = self.adapter()
        self.insert("recorded", "submission_uncertain")
        with patch("durable_claude.subprocess.Popen", side_effect=AssertionError("started")):
            self.assertEqual(a.find_job("recorded"), "recorded")
            self.assertIsNone(a.find_job("missing"))
            self.assertIsNone(a.find_job("../bad"))

    def test_setup_failure_before_spawn_releases_only_its_slot(self):
        a = self.adapter(max_concurrency=1)
        with patch("durable_claude.child_environment", side_effect=OSError("synthetic failure")), \
             patch("durable_claude.subprocess.Popen", side_effect=AssertionError("started")):
            result = a.submit("synthetic task", "failed-setup")
            self.assertEqual(result["status"], "not_submitted")
            self.assertEqual(a.poll("failed-setup")["status"], "failed")
            self.assertEqual(a.status()["local_active"], 0)
            replay = a.submit("synthetic task", "failed-setup")
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["status"], "not_submitted")
            self.assertEqual(replay["existing_status"], "not_submitted")

    def test_replay_reports_original_state_without_starting_again(self):
        a = self.adapter()
        fingerprint = hashlib.sha256(json.dumps(["synthetic task", "claude-synthetic", "medium"],
                                                ensure_ascii=True).encode("ascii")).hexdigest()
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            with con:
                for job, state, error, heartbeat in (
                    ("done", "succeeded", None, time.time()),
                    ("failed", "failed", "provider_error", time.time()),
                    ("unknown", "submission_uncertain", None, None),
                    ("reserved", "reserved", None, None),
                    ("running", "running", None, time.time()),
                    ("stale", "running", None, time.time() - 11),
                ):
                    con.execute("INSERT INTO jobs VALUES (?,?,?,?,?,NULL)",
                                (job, fingerprint, state, error, heartbeat))
        with patch("durable_claude.subprocess.Popen", side_effect=AssertionError("started")):
            for job in ("done", "failed"):
                replay = a.submit("synthetic task", job)
                self.assertEqual(replay["status"], "not_submitted")
                self.assertEqual(replay["reason"], "already_submitted")
                self.assertEqual(replay["existing_status"], "succeeded" if job == "done" else "failed")
                self.assertEqual(replay["job_id"], job)
            for job in ("unknown", "reserved", "stale"):
                self.assertEqual(a.submit("synthetic task", job)["status"], "submission_uncertain")
            self.assertEqual(a.submit("synthetic task", "running")["status"], "running")

    def test_spawn_error_retains_original_job_id_and_never_restarts_it(self):
        a = self.adapter(max_concurrency=1)
        with patch("durable_claude.subprocess.Popen", side_effect=OSError("synthetic failure")) as spawn:
            receipt = a.submit("synthetic task", "spawn-unknown")
            self.assertEqual(receipt["status"], "submission_uncertain")
            self.assertEqual(receipt["job_id"], "spawn-unknown")
            self.assertEqual(a.poll("spawn-unknown")["status"], "submission_uncertain")
            self.assertTrue(a.submit("synthetic task", "spawn-unknown")["replayed"])
            self.assertEqual(a.submit("other", "other")["reason"], "provider_busy")
        self.assertEqual(spawn.call_count, 1)

    def test_spawn_error_cannot_overwrite_a_concurrent_terminal_result(self):
        a = self.adapter()
        def spawn(*_args, **_kwargs):
            with durable._connect(a.db) as con:
                con.execute("UPDATE jobs SET status='succeeded',output_file=? WHERE id=?",
                            ("spawn-race.result.json", "spawn-race"))
            raise OSError("synthetic")
        with patch("durable_claude.subprocess.Popen", side_effect=spawn):
            receipt = a.submit("task", "spawn-race")
        self.assertEqual(receipt["status"], "submission_uncertain")
        with durable._connect(a.db) as con:
            row = con.execute("SELECT status,output_file FROM jobs WHERE id='spawn-race'").fetchone()
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["output_file"], "spawn-race.result.json")

    def test_opt_in_intent_is_frozen_and_replay_survives_mode_change(self):
        a = self.adapter(session_evidence=True)
        class Input:
            def write(self, data): self.value = data
            def close(self): pass
        class Process:
            stdin = Input()
            def wait(self): pass
        with patch("durable_claude.subprocess.Popen", return_value=Process()) as popen:
            self.assertEqual(a.submit("synthetic prompt", "opt-in")["status"], "queued")
        message = json.loads(Process.stdin.value)
        self.assertTrue(message["session_evidence"])
        with durable._connect(a.db) as con:
            row = con.execute("SELECT session_id FROM session_evidence_intents WHERE job_id=?", ("opt-in",)).fetchone()
        self.assertEqual(row[0], message["session_id"])
        b = self.adapter(session_evidence=False)
        with patch("durable_claude.subprocess.Popen", side_effect=AssertionError("must not restart")):
            replay = b.submit("synthetic prompt", "opt-in")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["job_id"], "opt-in")
        self.assertEqual(popen.call_count, 1)
        self.assertNotIn("session_id", replay)
        self.assertNotIn(b"synthetic prompt", (self.state / "jobs.sqlite3").read_bytes())

    def test_opt_in_uuid_failure_does_not_reserve_or_launch(self):
        a = self.adapter(session_evidence=True, max_concurrency=1)
        fake_uuid = SimpleNamespace(uuid4=Mock(side_effect=OSError("synthetic-private")))
        with patch("durable_claude.uuid", fake_uuid), \
             patch("durable_claude.subprocess.Popen", side_effect=AssertionError("must not launch")) as popen:
            receipt = a.submit("synthetic task", "new-job")
        self.assertEqual(receipt, {"status": "not_submitted", "reason": "session_evidence_unavailable"})
        self.assertEqual(a.status()["local_active"], 0)
        with durable._connect(a.db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM session_evidence_intents").fetchone()[0], 0)
        popen.assert_not_called()
        self.assertNotIn("synthetic-private", repr(receipt))

    def test_opt_in_existing_or_busy_job_never_generates_uuid(self):
        a = self.adapter(session_evidence=True, max_concurrency=1)
        fingerprint = hashlib.sha256(json.dumps(["synthetic task", "claude-synthetic", "medium"],
                                               ensure_ascii=True).encode("ascii")).hexdigest()
        with durable._connect(a.db) as con:
            con.execute("INSERT INTO jobs VALUES (?,?,'submission_uncertain',NULL,NULL,NULL)", ("old-job", fingerprint))
        fake_uuid = SimpleNamespace(uuid4=Mock(side_effect=OSError("synthetic-private")))
        with patch("durable_claude.uuid", fake_uuid), \
             patch("durable_claude.subprocess.Popen", side_effect=AssertionError("must not launch")):
            replay = a.submit("synthetic task", "old-job")
            busy = a.submit("other", "new-job")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "submission_uncertain")
        self.assertEqual(replay["job_id"], "old-job")
        self.assertEqual(busy, {"status": "not_submitted", "reason": "provider_busy"})
        fake_uuid.uuid4.assert_not_called()

    def test_opt_in_worker_retains_only_safe_session_projection(self):
        a = self.adapter(session_evidence=True)
        self.insert("session-job", "reserved")
        session_id = "319d322a-a582-459c-abd0-e2d7676cad1b"
        with durable._connect(a.db) as con:
            con.execute("INSERT INTO session_evidence_intents VALUES (?,?,NULL)", ("session-job", session_id))
        message = {"task": "synthetic prompt", "cli_path": str(self.cli), "profile_dir": str(self.profile),
                   "work_dir": str(self.work), "model": "claude-synthetic", "effort": "medium",
                   "timeout_seconds": 900, "session_evidence": True, "session_id": session_id}
        class FakeAdapter:
            def __init__(self, *_args, **_kwargs): pass
            def submit(self, task, task_id, **kwargs):
                assert kwargs["_session_id"] == session_id
                return {"status": "queued", "job_id": task_id}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "ok", "local_exit_confirmed": True,
                        "session_bound": True}
        def collect(*args, **kwargs):
            with durable._connect(a.db) as con:
                row = con.execute("SELECT status,output_file FROM jobs WHERE id='session-job'").fetchone()
                pending = json.loads(con.execute("SELECT quality_json FROM session_evidence_intents WHERE job_id='session-job'").fetchone()[0])
            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(json.loads((self.state / row["output_file"]).read_text(encoding="utf-8"))["output"], "ok")
            self.assertEqual(pending["session_cleanup"], "unconfirmed")
            self.assertEqual(pending["client_evidence_status"], "pending")
            self.assertIs(kwargs["durable_result_saved"], True)
            return {"client_evidence_status": "matched", "client_model_matches_requested": True,
                    "client_effort_matches_requested": True, "session_cleanup": "deletion_requested",
                    "private": "synthetic-private"}
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode("utf-8")))
        with patch("durable_claude.ClaudeAdapter", FakeAdapter), patch("durable_claude.sys.stdin", fake_stdin), \
             patch("session_evidence.collect_session_evidence", side_effect=collect) as collector:
            durable._run_worker(str(self.state), "session-job")
        collector.assert_called_once()
        result = a.poll("session-job")
        self.assertTrue(result["quality_verification"]["client_effort_matches_requested"])
        self.assertEqual(result["quality_verification"]["session_cleanup"], "deletion_requested")
        self.assertFalse(result["quality_verification"]["effort_verified"])
        self.assertNotIn(session_id, repr(result))
        self.assertNotIn("synthetic-private", repr(result))

    def test_opt_in_failed_worker_preserves_cleanup_status_without_raw_evidence(self):
        a = self.adapter(session_evidence=True)
        self.insert("failed-session", "reserved")
        session_id = "319d322a-a582-459c-abd0-e2d7676cad1b"
        with durable._connect(a.db) as con:
            con.execute("INSERT INTO session_evidence_intents VALUES (?,?,NULL)", ("failed-session", session_id))
        message = {"task": "synthetic prompt", "cli_path": str(self.cli), "profile_dir": str(self.profile),
                   "work_dir": str(self.work), "model": "claude-synthetic", "effort": "medium",
                   "timeout_seconds": 900, "session_evidence": True, "session_id": session_id}
        class FakeAdapter:
            def __init__(self, *_args, **_kwargs): pass
            def submit(self, task, task_id, **kwargs): return {"status": "queued", "job_id": task_id}
            def poll(self, job_id):
                return {"status": "failed", "error": "provider_reported_error", "local_exit_confirmed": True, "session_quality": {
                    "client_evidence_status": "unavailable", "client_model_matches_requested": None,
                    "client_effort_matches_requested": None, "session_cleanup": "not_attempted",
                    "private": "synthetic-private"}}
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode("utf-8")))
        with patch("durable_claude.ClaudeAdapter", FakeAdapter), patch("durable_claude.sys.stdin", fake_stdin):
            durable._run_worker(str(self.state), "failed-session")
        result = a.poll("failed-session")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["quality_verification"]["session_cleanup"], "not_attempted")
        self.assertIsNone(result["quality_verification"]["client_effort_matches_requested"])
        self.assertNotIn(session_id, repr(result))
        self.assertNotIn("synthetic-private", repr(result))

    def test_opt_in_rejects_conflicting_exit_without_saving_collecting_or_relaunching(self):
        cases = (
            {"local_exit_confirmed": True, "error": "process_exit_unconfirmed"},
            {"local_exit_confirmed": True, "error": "process_still_running"},
            {"local_exit_confirmed": True, "error": "worker_identity_unconfirmed"},
            {"local_exit_confirmed": True, "release_basis": "operator_confirmed"},
            {"local_exit_confirmed": False},
            {"local_exit_confirmed": 1},
            {},
        )
        for evidence in cases:
            with self.subTest(evidence=evidence):
                nested = DurableTests("runTest")
                nested.setUp()
                try:
                    a = nested.adapter(session_evidence=True, max_concurrency=1)
                    nested.insert("conflicting-session", "reserved")
                    session_id = "319d322a-a582-459c-abd0-e2d7676cad1b"
                    fingerprint = hashlib.sha256(json.dumps(
                        ["synthetic prompt", "claude-synthetic", "medium", "client_session_evidence_v1"],
                        ensure_ascii=True).encode("ascii")).hexdigest()
                    with durable._connect(a.db) as con:
                        con.execute("UPDATE jobs SET fingerprint=? WHERE id=?", (fingerprint, "conflicting-session"))
                        con.execute("INSERT INTO session_evidence_intents VALUES (?,?,NULL)",
                                    ("conflicting-session", session_id))
                    message = {"task": "synthetic prompt", "cli_path": str(nested.cli),
                               "profile_dir": str(nested.profile), "work_dir": str(nested.work),
                               "model": "claude-synthetic", "effort": "medium", "timeout_seconds": 900,
                               "session_evidence": True, "session_id": session_id}
                    class FakeAdapter:
                        def __init__(self, *_args, **_kwargs): pass
                        def submit(self, task, task_id, **_kwargs): return {"status": "queued", "job_id": task_id}
                        def poll(self, job_id):
                            return {"status": "succeeded", "output": "synthetic-private",
                                    "session_bound": True, **evidence}
                    fake_stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode("utf-8")))
                    with patch("durable_claude.ClaudeAdapter", FakeAdapter), \
                         patch("durable_claude.sys.stdin", fake_stdin), \
                         patch("durable_claude._atomic_json") as write, \
                         patch("session_evidence.collect_session_evidence") as collector:
                        durable._run_worker(str(nested.state), "conflicting-session")
                    result = a.poll("conflicting-session")
                    self.assertEqual(result["status"], "submission_uncertain")
                    self.assertEqual(result["error"], "invalid_provider_response")
                    self.assertNotIn("output", result)
                    self.assertNotIn("synthetic-private", repr(result))
                    self.assertEqual(a.status()["local_active"], 1)
                    write.assert_not_called()
                    collector.assert_not_called()
                    with patch("durable_claude.subprocess.Popen", side_effect=AssertionError("must not relaunch")):
                        replay = a.submit("synthetic prompt", "conflicting-session")
                    self.assertEqual(replay["status"], "submission_uncertain")
                    self.assertTrue(replay["replayed"])
                finally:
                    nested.doCleanups()

    def _run_session_persistence_case(self, fault):
        a = self.adapter(session_evidence=True)
        self.insert("boundary-job", "reserved")
        session_id = "319d322a-a582-459c-abd0-e2d7676cad1b"
        with durable._connect(a.db) as con:
            con.execute("INSERT INTO session_evidence_intents VALUES (?,?,NULL)", ("boundary-job", session_id))
            if fault in ("pending-fail", "final-fail"):
                target = "unconfirmed" if fault == "pending-fail" else "deletion_requested"
                con.execute("CREATE TRIGGER inject_failure BEFORE UPDATE ON session_evidence_intents "
                            "WHEN NEW.quality_json LIKE '%" + target + "%' "
                            "BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        message = {"task": "synthetic prompt", "cli_path": str(self.cli), "profile_dir": str(self.profile),
                   "work_dir": str(self.work), "model": "claude-synthetic", "effort": "medium",
                   "timeout_seconds": 900, "session_evidence": True, "session_id": session_id}
        class FakeAdapter:
            def __init__(self, *_args, **_kwargs): pass
            def submit(self, task, task_id, **kwargs): return {"status": "queued", "job_id": task_id}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "recoverable response",
                        "local_exit_confirmed": True, "session_bound": True}
        def collected(*_args, **kwargs):
            self.assertIs(kwargs["durable_result_saved"], True)
            if fault == "collector-fail":
                raise OSError("synthetic-private")
            return {"client_evidence_status": "matched", "client_model_matches_requested": True,
                    "client_effort_matches_requested": True, "session_cleanup": "deletion_requested"}
        original_atomic = durable._atomic_json
        def write_result(path, value):
            if fault == "write-fail":
                raise OSError("synthetic-private")
            original_atomic(path, value)
            if fault == "crash-after-write":
                raise OSError("synthetic crash")
            if fault == "terminal-race":
                with durable._connect(a.db) as con:
                    con.execute("UPDATE jobs SET status='failed',error='provider_reported_error' WHERE id='boundary-job'")
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode("utf-8")))
        with patch("durable_claude.ClaudeAdapter", FakeAdapter), patch("durable_claude.sys.stdin", fake_stdin), \
             patch("durable_claude._atomic_json", side_effect=write_result), \
             patch("session_evidence.collect_session_evidence", side_effect=collected) as collector:
            durable._run_worker(str(self.state), "boundary-job")
        return a, a.poll("boundary-job"), collector.call_count

    def test_result_write_failures_never_collect_or_delete_session(self):
        for fault in ("write-fail", "crash-after-write"):
            with self.subTest(fault=fault):
                # Each case needs independent ledger/trigger state.
                nested = DurableTests("runTest")
                nested.setUp()
                try:
                    a, result, calls = nested._run_session_persistence_case(fault)
                    self.assertEqual(result["status"], "submission_uncertain")
                    self.assertEqual(result["quality_verification"]["session_cleanup"], "not_attempted")
                    self.assertNotIn("output", result)
                    self.assertEqual(calls, 0)
                    self.assertEqual((nested.state / "boundary-job.result.json").exists(), fault == "crash-after-write")
                    self.assertNotIn("synthetic-private", repr(result))
                finally:
                    nested.doCleanups()

    def test_cleanup_marker_failure_rolls_back_terminal_commit_and_does_not_collect(self):
        a, result, calls = self._run_session_persistence_case("pending-fail")
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertNotIn("output", result)
        self.assertTrue((self.state / "boundary-job.result.json").exists())
        self.assertEqual(result["quality_verification"]["session_cleanup"], "not_attempted")
        self.assertEqual(calls, 0)

    def test_collection_crash_or_final_state_write_failure_stays_unconfirmed(self):
        for fault in ("collector-fail", "final-fail"):
            with self.subTest(fault=fault):
                nested = DurableTests("runTest")
                nested.setUp()
                try:
                    a, result, calls = nested._run_session_persistence_case(fault)
                    self.assertEqual(result["status"], "succeeded")
                    self.assertEqual(result["output"], "recoverable response")
                    self.assertEqual(result["quality_verification"]["session_cleanup"], "unconfirmed")
                    self.assertIsNone(result["quality_verification"]["client_effort_matches_requested"])
                    self.assertEqual(calls, 1)
                    self.assertNotIn("synthetic-private", repr(result))
                finally:
                    nested.doCleanups()

    def test_concurrent_terminal_prevents_late_success_and_cleanup(self):
        a, result, calls = self._run_session_persistence_case("terminal-race")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "provider_reported_error")
        self.assertEqual(calls, 0)
        self.assertTrue((self.state / "boundary-job.result.json").exists())

    def test_unexpected_exception_after_success_commit_cannot_erase_result(self):
        original_connect = durable._connect
        injected = [False]
        @contextmanager
        def injected_connect(db):
            committed_success = [False]
            with original_connect(db) as con:
                class Connection:
                    def execute(self, sql, params=()):
                        if "UPDATE jobs SET status='succeeded'" in sql:
                            committed_success[0] = True
                        return con.execute(sql, params)
                yield Connection()
            if committed_success[0] and not injected[0]:
                injected[0] = True
                raise RuntimeError("synthetic after commit")
        with patch("durable_claude._connect", side_effect=injected_connect):
            a, result, calls = self._run_session_persistence_case("none")
        self.assertTrue(injected[0])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["output"], "recoverable response")
        self.assertEqual(result["quality_verification"]["client_evidence_status"], "pending")
        self.assertEqual(result["quality_verification"]["session_cleanup"], "unconfirmed")
        self.assertEqual(calls, 0)

    def test_unexpected_quality_write_exception_cannot_erase_result(self):
        original_connect = durable._connect
        @contextmanager
        def injected_connect(db):
            with original_connect(db) as con:
                class Connection:
                    def execute(self, sql, params=()):
                        if ("UPDATE session_evidence_intents SET quality_json" in sql and params
                                and isinstance(params[0], str) and '"deletion_requested"' in params[0]):
                            raise RuntimeError("synthetic-private")
                        return con.execute(sql, params)
                yield Connection()
        with patch("durable_claude._connect", side_effect=injected_connect):
            a, result, calls = self._run_session_persistence_case("none")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["output"], "recoverable response")
        self.assertEqual(result["quality_verification"]["client_evidence_status"], "pending")
        self.assertEqual(result["quality_verification"]["session_cleanup"], "unconfirmed")
        self.assertEqual(calls, 1)
        self.assertNotIn("synthetic-private", repr(result))

    def test_result_is_available_while_client_evidence_is_pending(self):
        a = self.adapter(session_evidence=True)
        self.insert("pending-job", "reserved")
        session_id = "319d322a-a582-459c-abd0-e2d7676cad1b"
        with durable._connect(a.db) as con:
            con.execute("INSERT INTO session_evidence_intents VALUES (?,?,NULL)", ("pending-job", session_id))
        message = {"task": "synthetic prompt", "cli_path": str(self.cli), "profile_dir": str(self.profile),
                   "work_dir": str(self.work), "model": "claude-synthetic", "effort": "medium",
                   "timeout_seconds": 900, "session_evidence": True, "session_id": session_id}
        class FakeAdapter:
            def __init__(self, *_args, **_kwargs): pass
            def submit(self, task, task_id, **kwargs): return {"status": "queued", "job_id": task_id}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "ready response",
                        "local_exit_confirmed": True, "session_bound": True}
        entered, release = threading.Event(), threading.Event()
        def collect(*_args, **_kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("synthetic timeout")
            return {"client_evidence_status": "matched", "client_model_matches_requested": True,
                    "client_effort_matches_requested": True, "session_cleanup": "deletion_requested"}
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode("utf-8")))
        with patch("durable_claude.ClaudeAdapter", FakeAdapter), patch("durable_claude.sys.stdin", fake_stdin), \
             patch("session_evidence.collect_session_evidence", side_effect=collect) as collector:
            thread = threading.Thread(target=durable._run_worker, args=(str(self.state), "pending-job"))
            try:
                thread.start()
                self.assertTrue(entered.wait(3))
                pending = a.poll("pending-job")
                self.assertEqual(pending["status"], "succeeded")
                self.assertEqual(pending["output"], "ready response")
                self.assertEqual(pending["quality_verification"]["client_evidence_status"], "pending")
                self.assertEqual(pending["quality_verification"]["session_cleanup"], "unconfirmed")
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(collector.call_count, 1)
        self.assertEqual(a.poll("pending-job")["quality_verification"]["client_evidence_status"], "matched")

    def test_real_worker_writes_final_result_after_synthetic_slow_poll(self):
        a = self.adapter()
        self.insert("worker-job", "reserved")
        message = {"task": "synthetic prompt", "cli_path": str(self.cli),
                   "profile_dir": str(self.profile), "work_dir": str(self.work),
                   "model": "claude-synthetic", "effort": "medium", "timeout_seconds": 900}
        class FakeAdapter:
            def __init__(self, *_args, **_kwargs):
                self.events = iter([{"status": "running"},
                                    {"status": "submission_uncertain", "error": "provider_slow"},
                                    {"status": "succeeded", "output": "synthetic response", "local_exit_confirmed": True}])
            def submit(self, task, task_id):
                if task != "synthetic prompt" or task_id != "worker-job":
                    raise AssertionError("unexpected worker input")
                return {"status": "queued", "job_id": task_id}
            def poll(self, job_id):
                return next(self.events)
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode("utf-8")))
        with patch("durable_claude.ClaudeAdapter", FakeAdapter), \
             patch("durable_claude.sys.stdin", fake_stdin), patch("durable_claude.time.sleep"):
            durable._run_worker(str(self.state), "worker-job")
        result = a.poll("worker-job")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["output"], "synthetic response")
        self.assertEqual(result["model_verification"], "unverified")
        self.assertNotIn(b"synthetic prompt", (self.state / "jobs.sqlite3").read_bytes())

    def test_parent_exit_then_new_adapter_recovers_fake_worker_result(self):
        fake = Path(self.tmp.name) / "synthetic_worker.py"
        fake.write_text("import json,os,sqlite3,sys,time\n"
                        "state,job=sys.argv[2:4]\n"
                        "request=json.load(sys.stdin)\n"
                        "with open(os.path.join(state,'worker.pid.tmp'),'w',encoding='ascii') as f: f.write(str(os.getpid()))\n"
                        "os.replace(os.path.join(state,'worker.pid.tmp'),os.path.join(state,'worker.pid'))\n"
                        "deadline=time.monotonic()+15\n"
                        "while not os.path.exists(os.path.join(state,'release')) and time.monotonic()<deadline: time.sleep(.01)\n"
                        "name=job+'.result.json'\n"
                        "with open(os.path.join(state,name),'w',encoding='utf-8') as f: json.dump({'output':'synthetic result'},f)\n"
                        "with sqlite3.connect(os.path.join(state,'jobs.sqlite3')) as c: c.execute(\"UPDATE jobs SET status='succeeded',output_file=? WHERE id=?\",(name,job))\n", encoding="utf-8")
        # Launch an intermediate parent, which starts the fake worker and exits.
        module_dir = str(Path(durable.__file__).resolve().parent)
        code = ("import sys,json;sys.path.insert(0,sys.argv[1]);import durable_claude as d;"
                "d._WORKER_SCRIPT=sys.argv[2];a=d.DurableClaudeAdapter(*json.loads(sys.argv[3]),enabled=True);"
                "print(json.dumps(a.submit('synthetic prompt','restart-job')))" )
        # Release the synthetic worker even if an earlier assertion fails.
        self.addCleanup((self.state / "release").write_text, "go", encoding="ascii")
        try:
            result = subprocess.run([sys.executable, "-c", code, module_dir, str(fake), json.dumps(self.args)],
                                    capture_output=True, text=True, encoding="utf-8", timeout=10)
        except subprocess.TimeoutExpired:
            raise AssertionError("synthetic worker launcher timed out") from None
        self.assertEqual(result.returncode, 0, "synthetic worker launcher failed")
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "queued")
        if os.name == "nt":
            # A completed ledger entry can be visible just before this fake
            # detached worker exits. Hold a handle to that exact process so
            # cleanup waits for its natural exit, without terminating it.
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle.restype = wintypes.BOOL
            pid_file = self.state / "worker.pid"
            deadline = time.monotonic() + 10
            pid = None
            while time.monotonic() < deadline:
                try:
                    pid = int(pid_file.read_text(encoding="ascii"))
                    break
                except (OSError, ValueError):
                    time.sleep(.02)
            self.assertIsNotNone(pid, "fake worker did not report its PID")
            self.assertGreater(pid, 0, "invalid fake worker PID")
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            self.assertTrue(handle, "could not open fake worker process")

            def wait_for_fake_worker():
                try:
                    # Also release the fake worker if an earlier assertion
                    # failed; let it exit normally before directory cleanup.
                    (self.state / "release").write_text("go", encoding="ascii")
                    self.assertEqual(kernel.WaitForSingleObject(handle, 10000), 0,
                                     "fake worker did not exit")
                finally:
                    kernel.CloseHandle(handle)

            # addCleanup is LIFO: wait before TemporaryDirectory cleanup.
            self.addCleanup(wait_for_fake_worker)
        a = self.adapter()
        self.assertNotEqual(a.poll("restart-job")["status"], "succeeded")
        replay = a.submit("synthetic prompt", "restart-job")
        self.assertTrue(replay["replayed"])
        (self.state / "release").write_text("go", encoding="ascii")
        for _ in range(500):
            output = a.poll("restart-job")
            if output["status"] == "succeeded":
                break
            time.sleep(.02)
        self.assertEqual(output["status"], "succeeded")
        self.assertEqual(output["output"], "synthetic result")
        self.assertNotIn(b"synthetic prompt", (self.state / "jobs.sqlite3").read_bytes())

    def test_worker_persists_only_narrow_model_usage_evidence_and_old_records_work(self):
        a = self.adapter()
        self.insert("new-result", "reserved")
        message = {"task": "synthetic prompt", "cli_path": str(self.cli),
                   "profile_dir": str(self.profile), "work_dir": str(self.work),
                   "model": "claude-example-1", "effort": "medium", "timeout_seconds": 900}
        class FakeAdapter:
            def __init__(self, *_args, **_kwargs): pass
            def submit(self, task, task_id): return {"status": "queued", "job_id": task_id}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "synthetic response", "local_exit_confirmed": True,
                        "quality_verification": {"verified": True, "effort_verified": True,
                            "model_usage_matches_requested": True, "recorded_model": "claude-example-1",
                            "evidence_source": "cli_result_model_usage", "private": "synthetic-private"}}
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode("utf-8")))
        with patch("durable_claude.ClaudeAdapter", FakeAdapter), patch("durable_claude.sys.stdin", fake_stdin):
            durable._run_worker(str(self.state), "new-result")
        # A new adapter has different current settings: old job evidence is frozen.
        result = self.adapter().poll("new-result")
        self.assertIs(result["local_exit_confirmed"], True)
        self.assertEqual(result["quality_verification"], {"verified": False, "effort_verified": False,
            "model_usage_matches_requested": True, "recorded_model": "claude-example-1",
            "evidence_source": "cli_result_model_usage"})
        self.assertEqual(result["model_verification"], "unverified")
        raw = (self.state / "new-result.result.json").read_text(encoding="utf-8")
        self.assertIs(json.loads(raw)["local_exit_confirmed"], True)
        self.assertNotIn("synthetic-private", raw)
        self.assertNotIn("synthetic prompt", raw)

        self.insert("legacy", "succeeded")
        (self.state / "legacy.result.json").write_text(json.dumps({"output": "legacy"}), encoding="utf-8")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            with con:
                con.execute("UPDATE jobs SET output_file=? WHERE id=?", ("legacy.result.json", "legacy"))
        legacy = a.poll("legacy")
        self.assertEqual(legacy["status"], "succeeded")
        self.assertNotIn("local_exit_confirmed", legacy)
        self.assertFalse(legacy["quality_verification"]["model_usage_matches_requested"])
        self.assertFalse(legacy["quality_verification"]["effort_verified"])

    def test_worker_persists_confirmed_exit_and_blocks_invalid_success_evidence(self):
        a = self.adapter()
        message = {"task": "synthetic prompt", "cli_path": str(self.cli),
                   "profile_dir": str(self.profile), "work_dir": str(self.work),
                   "model": "claude-synthetic", "effort": "medium", "timeout_seconds": 900}
        cases = (
            ("true", {"local_exit_confirmed": True}, True),
            ("false", {"local_exit_confirmed": False}, None),
            ("number", {"local_exit_confirmed": 1}, None),
            ("string", {"local_exit_confirmed": "synthetic-private"}, None),
            ("null", {"local_exit_confirmed": None}, None),
            ("missing", {}, None),
            ("conflict", {"local_exit_confirmed": True, "error": "process_exit_unconfirmed"}, None),
            ("operator", {"local_exit_confirmed": True, "release_basis": "operator_confirmed"}, None),
        )
        for suffix, evidence, expected in cases:
            with self.subTest(case=suffix):
                job = "exit-" + suffix
                self.insert(job, "reserved")
                class FakeAdapter:
                    def __init__(self, *_args, **_kwargs): pass
                    def submit(self, task, task_id): return {"status": "queued", "job_id": task_id}
                    def poll(self, job_id): return {"status": "succeeded", "output": "safe", **evidence}
                fake_stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode("utf-8")))
                with patch("durable_claude.ClaudeAdapter", FakeAdapter), patch("durable_claude.sys.stdin", fake_stdin):
                    durable._run_worker(str(self.state), job)
                projected = a.poll(job)
                if expected is None:
                    self.assertEqual(projected["status"], "submission_uncertain")
                    self.assertEqual(projected["error"], "invalid_provider_response")
                    self.assertFalse((self.state / (job + ".result.json")).exists())
                    self.assertNotIn("local_exit_confirmed", projected)
                    self.assertNotIn("output", projected)
                else:
                    self.assertEqual(projected["status"], "succeeded")
                    saved = json.loads((self.state / (job + ".result.json")).read_text(encoding="utf-8"))
                    self.assertIs(saved["local_exit_confirmed"], expected)
                    self.assertIs(projected["local_exit_confirmed"], expected)
                self.assertNotIn("synthetic-private", repr(projected))

    def test_poll_only_projects_safe_boolean_exit_from_stored_result(self):
        a = self.adapter()
        cases = (
            ("stored-true", {"local_exit_confirmed": True}, True),
            ("stored-false", {"local_exit_confirmed": False}, False),
            ("stored-number", {"local_exit_confirmed": 1}, None),
            ("stored-string", {"local_exit_confirmed": "synthetic-private"}, None),
            ("stored-missing", {}, None),
            ("stored-conflict", {"local_exit_confirmed": True, "error": "process_still_running"}, False),
            ("stored-operator", {"local_exit_confirmed": True, "release_basis": "operator_confirmed"}, False),
            ("stored-row-conflict", {"local_exit_confirmed": True}, False),
        )
        for job, evidence, expected in cases:
            with self.subTest(job=job):
                self.insert(job, "succeeded")
                name = job + ".result.json"
                (self.state / name).write_text(json.dumps({"output": "safe", **evidence}), encoding="utf-8")
                with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
                    with con:
                        row_error = "worker_identity_unconfirmed" if job == "stored-row-conflict" else None
                        con.execute("UPDATE jobs SET output_file=?,error=? WHERE id=?", (name, row_error, job))
                projected = a.poll(job)
                self.assertEqual(projected["status"], "succeeded")
                if expected is None:
                    self.assertNotIn("local_exit_confirmed", projected)
                else:
                    self.assertIs(projected["local_exit_confirmed"], expected)
                self.assertNotIn("synthetic-private", repr(projected))

    def test_unrecognized_stored_model_evidence_is_not_forwarded(self):
        a = self.adapter()
        self.insert("invalid-evidence", "succeeded")
        (self.state / "invalid-evidence.result.json").write_text(json.dumps({"output": "ok",
            "quality_verification": {"model_usage_matches_requested": True, "recorded_model": "synthetic-private",
                                     "evidence_source": "synthetic-private"}}), encoding="utf-8")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            with con:
                con.execute("UPDATE jobs SET output_file=? WHERE id=?", ("invalid-evidence.result.json", "invalid-evidence"))
        result = a.poll("invalid-evidence")
        self.assertFalse(result["quality_verification"]["model_usage_matches_requested"])
        self.assertNotIn("synthetic-private", repr(result))

    def test_worker_refuses_model_evidence_mismatched_with_frozen_request(self):
        a = self.adapter()
        self.insert("mismatch", "reserved")
        message = {"task": "synthetic prompt", "cli_path": str(self.cli),
                   "profile_dir": str(self.profile), "work_dir": str(self.work),
                   "model": "claude-example-1", "effort": "medium", "timeout_seconds": 900}
        class FakeAdapter:
            def __init__(self, *_args, **_kwargs): pass
            def submit(self, task, task_id): return {"status": "queued", "job_id": task_id}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "ok", "local_exit_confirmed": True, "quality_verification": {
                    "model_usage_matches_requested": True, "recorded_model": "claude-example-2",
                    "evidence_source": "cli_result_model_usage"}}
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode("utf-8")))
        with patch("durable_claude.ClaudeAdapter", FakeAdapter), patch("durable_claude.sys.stdin", fake_stdin):
            durable._run_worker(str(self.state), "mismatch")
        result = a.poll("mismatch")
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(result["quality_verification"]["model_usage_matches_requested"])
        self.assertNotIn("recorded_model", result["quality_verification"])

    def test_synthetic_launcher_timeout_does_not_expose_command(self):
        nested = DurableTests("test_parent_exit_then_new_adapter_recovers_fake_worker_result")
        result = unittest.TestResult()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("synthetic-private-path", 10)):
            nested.run(result)
        self.assertEqual(len(result.failures), 1)
        self.assertFalse(result.errors)
        details = result.failures[0][1]
        self.assertIn("synthetic worker launcher timed out", details)
        self.assertNotIn("synthetic-private-path", details)


if __name__ == "__main__":
    unittest.main()
