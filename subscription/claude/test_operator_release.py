"""Offline checks for operator slot release; no client or network is used."""
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import os
import io
import json
from types import SimpleNamespace
import threading

import claude_adapter
import durable_claude as durable


class OperatorReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.state, self.profile, self.work = root / "state", root / "profile", root / "work"
        for path in (self.state, self.profile, self.work):
            path.mkdir()
        self.adapter = durable.DurableClaudeAdapter(
            str(root / "synthetic-cli"), str(self.profile), str(self.work), str(self.state),
            "claude-synthetic", "medium", enabled=True, max_concurrency=1)

    def insert(self, job_id, status, heartbeat=None):
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con, con:
            con.execute("INSERT INTO jobs VALUES (?,?,?,NULL,?,NULL)", (job_id, "digest", status, heartbeat))

    def test_xhigh_effort_is_accepted(self):
        adapter = claude_adapter.ClaudeAdapter(str(Path(self.tmp.name).resolve() / "cli"), str(self.profile),
                                               str(self.work), "claude-synthetic", "xhigh")
        self.assertEqual(adapter.effort, "xhigh")

    def test_invalid_worker_identity_is_unknown(self):
        self.assertEqual(durable._worker_identity_state(os.getpid(), f"{os.getpid()}:bad"), "unknown")

    def test_child_environment_requests_isolation_from_profile_customizations(self):
        env = claude_adapter.child_environment({"PATH": "synthetic", "ANTHROPIC_API_KEY": "synthetic-key"},
                                               str(self.profile), str(self.work))
        for name in ("CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_DISABLE_AUTO_MEMORY"):
            self.assertEqual(env[name], "1")
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_missing_ledger_is_reported_without_creating_one(self):
        empty = Path(self.tmp.name).resolve() / "absent"
        empty.mkdir()
        self.assertIsNone(durable.blocking_jobs(str(empty)))
        self.assertEqual(durable.operator_release(str(empty), "job"), "ledger_unavailable")
        self.assertEqual(list(empty.iterdir()), [])

    def test_reparse_attributes_on_state_or_database_prevent_open(self):
        self.insert("protected", "running", time.time() - 3600)
        original_lstat = Path.lstat
        for suspicious in (self.state, self.state / "jobs.sqlite3"):
            def fake_lstat(path, *args, **kwargs):
                actual = original_lstat(path, *args, **kwargs)
                if path == suspicious:
                    # Preserve the normal file type: a Windows junction can be
                    # a reparse point without reporting is_symlink() true.
                    return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=0x400)
                return actual
            with self.subTest(component=suspicious.name), \
                 mock.patch.object(Path, "lstat", fake_lstat), \
                 mock.patch.object(durable.sqlite3, "connect", side_effect=AssertionError("must not open")):
                self.assertIsNone(durable.blocking_jobs(str(self.state)))
                self.assertEqual(durable.operator_release(str(self.state), "protected"), "ledger_unavailable")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            self.assertEqual(con.execute("SELECT status FROM jobs WHERE id='protected'").fetchone()[0], "running")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_windows_junction_to_state_is_rejected_without_opening_ledger(self):
        self.insert("protected", "running", time.time() - 3600)
        junction = Path(self.tmp.name).resolve() / "synthetic-junction"
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(self.state)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, check=False, shell=False)
        if result.returncode:
            self.skipTest("Windows could not create a synthetic directory junction")
        try:
            self.assertTrue(getattr(junction.lstat(), "st_file_attributes", 0) & 0x400)
            with mock.patch.object(durable.sqlite3, "connect", side_effect=AssertionError("must not open")):
                self.assertIsNone(durable.blocking_jobs(str(junction)))
                self.assertEqual(durable.operator_release(str(junction), "protected"), "ledger_unavailable")
        finally:
            # Remove this exact synthetic junction, never its target or parent.
            os.rmdir(junction)
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            self.assertEqual(con.execute("SELECT status FROM jobs WHERE id='protected'").fetchone()[0], "running")

    def test_stale_job_without_identity_stays_blocked(self):
        self.insert("stale", "running", time.time() - 3600)
        self.assertEqual(self.adapter.submit("another task", "next")["reason"], "provider_busy")
        self.assertEqual([job["job_id"] for job in durable.blocking_jobs(str(self.state))], ["stale"])
        self.assertEqual(durable.operator_release(str(self.state), "stale"), "identity_unconfirmed")
        self.assertEqual(self.adapter.status()["local_active"], 1)
        self.assertEqual(self.adapter.poll("stale")["status"], "submission_uncertain")

    def test_reserved_and_future_heartbeat_are_never_released(self):
        self.insert("reserved", "reserved")
        self.insert("future", "running", time.time() + 3600)
        for job in ("reserved", "future"):
            self.assertEqual(durable.operator_release(str(self.state), job), "identity_unconfirmed")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            self.assertEqual(dict(con.execute("SELECT id,status FROM jobs")),
                             {"reserved": "reserved", "future": "running"})

    def test_claimed_worker_requires_confirmed_exit_and_preserves_uncertain(self):
        self.insert("claimed", "running", time.time() - 3600)
        orphan = self.state / "claimed.result.json"
        orphan.write_text('{"output":"unverified retained"}', encoding="utf-8")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con, con:
            con.execute("INSERT INTO worker_claims VALUES (?,?,?)", ("claimed", 42, "42:creation"))
        with mock.patch.object(durable, "_worker_identity_state", return_value="running"):
            self.assertEqual(durable.operator_release(str(self.state), "claimed"), "worker_running")
        with mock.patch.object(durable, "_worker_identity_state", return_value="unknown"):
            self.assertEqual(durable.operator_release(str(self.state), "claimed"), "identity_unconfirmed")
        with mock.patch.object(durable, "_worker_identity_state", return_value="exited"):
            self.assertEqual(durable.operator_release(str(self.state), "claimed"), "released")
        self.assertEqual(self.adapter.poll("claimed"), {"status":"submission_uncertain", "error":"operator_released",
                                                       "local_exit_confirmed":False, "release_basis":"operator_confirmed"})
        self.assertEqual(self.adapter.status()["local_active"], 0)
        self.assertEqual(self.adapter.status()["local_finished_unresolved"], 1)
        self.assertEqual(orphan.read_text(encoding="utf-8"), '{"output":"unverified retained"}')

    def test_worker_cannot_start_after_release_or_duplicate_claim(self):
        self.insert("released", "operator_released")
        self.insert("claimed", "reserved")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con, con:
            con.execute("INSERT INTO worker_claims VALUES (?,?,?)", ("claimed", 42, "42:creation"))
        for job in ("released", "claimed"):
            fake_stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps({"task":"test"}).encode()))
            with mock.patch.object(durable.sys, "stdin", fake_stdin), \
                 mock.patch.object(durable, "ClaudeAdapter", side_effect=AssertionError("must not launch")):
                durable._run_worker(str(self.state), job)
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            self.assertEqual(dict(con.execute("SELECT id,status FROM jobs")),
                             {"released":"operator_released", "claimed":"reserved"})

    def test_worker_claim_and_release_are_fenced_before_provider_submission(self):
        self.insert("race", "reserved")
        message = {"task":"synthetic", "cli_path":"synthetic", "profile_dir":"synthetic",
                   "work_dir":"synthetic", "model":"synthetic", "effort":"medium", "timeout_seconds":900}
        entered, proceed = threading.Event(), threading.Event()
        calls = []
        class FakeAdapter:
            def __init__(self, *args, **kwargs): pass
            def submit(self, task, task_id):
                calls.append(task_id)
                entered.set()
                if not proceed.wait(5):
                    raise TimeoutError("synthetic")
                return {"status":"not_submitted"}
        stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode()))
        with mock.patch.object(durable.sys, "stdin", stdin), \
             mock.patch.object(durable, "ClaudeAdapter", FakeAdapter), \
             mock.patch.object(durable, "_worker_identity_state", return_value="running"):
            worker = threading.Thread(target=durable._run_worker, args=(str(self.state), "race"))
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(durable.operator_release(str(self.state), "race"), "worker_running")
                durable._run_worker(str(self.state), "race")
            finally:
                proceed.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(calls, ["race"])

    def test_new_worker_handles_old_ledger_without_claim_table(self):
        self.insert("legacy", "reserved")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con, con:
            con.execute("DROP TABLE worker_claims")
        message = {"task":"synthetic", "cli_path":"synthetic", "profile_dir":"synthetic",
                   "work_dir":"synthetic", "model":"synthetic", "effort":"medium", "timeout_seconds":900}
        class FakeAdapter:
            def __init__(self, *args, **kwargs): pass
            def submit(self, task, task_id): return {"status":"not_submitted"}
        with mock.patch.object(durable.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode()))), \
             mock.patch.object(durable, "ClaudeAdapter", FakeAdapter):
            durable._run_worker(str(self.state), "legacy")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            self.assertEqual(con.execute("SELECT status FROM jobs WHERE id='legacy'").fetchone()[0], "failed")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM worker_claims WHERE job_id='legacy'").fetchone()[0], 1)

    def test_recent_heartbeat_and_terminal_rows_are_left_alone(self):
        self.insert("live", "running", time.time())
        self.insert("done", "failed", time.time() - 3600)
        self.assertEqual(durable.operator_release(str(self.state), "live"), "identity_unconfirmed")
        self.assertEqual(durable.operator_release(str(self.state), "done"), "not_blocking")
        self.assertEqual(durable.operator_release(str(self.state), "missing"), "unknown_job")
        self.assertEqual(durable.operator_release(str(self.state), "../bad id"), "unknown_job")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            self.assertEqual(dict(con.execute("SELECT id,status FROM jobs").fetchall()),
                             {"live": "running", "done": "failed"})

    def test_ignore_trigger_is_rejected_before_it_can_change_other_jobs(self):
        self.insert("claimed", "running", time.time() - 3600)
        self.insert("other", "running", time.time() - 3600)
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con, con:
            con.execute("INSERT INTO worker_claims VALUES (?,?,?)", ("claimed", 42, "42:123"))
            con.execute("CREATE TRIGGER ignored BEFORE UPDATE OF status ON jobs "
                        "WHEN OLD.id='claimed' BEGIN "
                        "UPDATE jobs SET error='tampered' WHERE id='other'; "
                        "SELECT RAISE(IGNORE); END")
            before = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        with mock.patch.object(durable, "_worker_identity_state", return_value="exited"):
            self.assertEqual(durable.operator_release(str(self.state), "claimed"), "unsafe_state")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            self.assertEqual(con.execute("SELECT * FROM jobs ORDER BY id").fetchall(), before)

    def test_successful_trigger_that_would_mutate_other_job_is_rejected(self):
        self.insert("claimed", "running", time.time() - 3600)
        self.insert("other", "running", time.time() - 3600)
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con, con:
            con.execute("INSERT INTO worker_claims VALUES (?,?,?)", ("claimed", 42, "42:123"))
            con.execute("CREATE TRIGGER mutating AFTER UPDATE OF status ON jobs "
                        "WHEN OLD.id='claimed' BEGIN "
                        "UPDATE jobs SET error='tampered' WHERE id='other'; END")
            before = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        with mock.patch.object(durable, "_worker_identity_state", return_value="exited"):
            self.assertEqual(durable.operator_release(str(self.state), "claimed"), "unsafe_state")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            self.assertEqual(con.execute("SELECT * FROM jobs ORDER BY id").fetchall(), before)

    def test_uppercase_jobs_table_trigger_is_rejected(self):
        self.insert("claimed", "running", time.time() - 3600)
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con, con:
            con.execute("INSERT INTO worker_claims VALUES (?,?,?)", ("claimed", 42, "42:123"))
            con.execute("ALTER TABLE jobs RENAME TO jobs_tmp")
            con.execute("ALTER TABLE jobs_tmp RENAME TO JOBS")
            con.execute("CREATE TRIGGER mutating AFTER UPDATE ON JOBS BEGIN SELECT 1; END")
            before = con.execute("SELECT * FROM JOBS").fetchall()
        with mock.patch.object(durable, "_worker_identity_state", return_value="exited"):
            self.assertEqual(durable.operator_release(str(self.state), "claimed"), "unsafe_state")
        with closing(sqlite3.connect(self.state / "jobs.sqlite3")) as con:
            self.assertEqual(con.execute("SELECT * FROM JOBS").fetchall(), before)


if __name__ == "__main__":
    unittest.main()
