"""Offline checks for operator slot release; no client or network is used."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from unittest import mock

import durable_grok as subject


class OperatorReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.root, self.profile, self.cli = base / "state", base / "profile", base / "cli"
        self.root.mkdir()
        self.profile.mkdir()
        self.cli.touch()
        self.adapter = subject.DurableGrokAdapter(str(self.cli), "example-model", "high",
                                                  str(self.profile), str(self.root), 2, 1)

    def insert(self, state, result_status, pid=None, identity=None):
        job_id = str(uuid.uuid4())
        result = {"status": result_status, "job_id": job_id}
        with closing(sqlite3.connect(self.root / "grok-jobs.sqlite3")) as con, con:
            con.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                        (job_id, hashlib.sha256(job_id.encode()).hexdigest(), "digest", state,
                         pid, identity, json.dumps(result)))
        return job_id

    def test_invalid_worker_identity_is_unknown(self):
        self.assertEqual(subject._worker_identity_state(os.getpid(), f"{os.getpid()}:bad"), "unknown")

    def test_missing_ledger_is_reported_without_creating_one(self):
        empty = Path(self.temp.name).resolve() / "absent"
        empty.mkdir()
        self.assertIsNone(subject.blocking_jobs(str(empty)))
        self.assertEqual(subject.operator_release(str(empty), str(uuid.uuid4())), "ledger_unavailable")
        self.assertEqual(list(empty.iterdir()), [])

    def test_dead_worker_job_is_released_and_stays_uncertain(self):
        job_id = self.insert("running", "running", pid=1, identity="1:never-matches")
        with mock.patch.object(subject, "_worker_identity_state", return_value="exited"):
            self.assertEqual(self.adapter.submit("another task", "next")["reason"], "provider_busy")
            self.assertEqual(subject.blocking_jobs(str(self.root)),
                             [{"job_id": job_id, "state": "running", "worker_state": "exited"}])
            self.assertEqual(subject.operator_release(str(self.root), job_id), "released")
            self.assertEqual(subject.blocking_jobs(str(self.root)), [])
            self.assertEqual(self.adapter.poll(job_id),
                             {"status": "submission_uncertain", "job_id": job_id,
                              "error": "operator_released", "local_exit_confirmed": False,
                              "release_basis": "operator_confirmed"})
            self.assertEqual(self.adapter.status()["local_available_slots"], 1)
            self.assertEqual(subject.operator_release(str(self.root), job_id), "not_blocking")

    def test_running_worker_terminal_and_unknown_rows_are_left_alone(self):
        live = self.insert("running", "running", pid=os.getpid(), identity="synthetic-live")
        done = self.insert("failed", "failed")
        with mock.patch.object(subject, "_worker_identity_state", return_value="running"):
            self.assertEqual(subject.operator_release(str(self.root), live), "worker_running")
        self.assertEqual(subject.operator_release(str(self.root), done), "not_blocking")
        self.assertEqual(subject.operator_release(str(self.root), str(uuid.uuid4())), "unknown_job")
        self.assertEqual(subject.operator_release(str(self.root), "not-a-uuid"), "unknown_job")
        with closing(sqlite3.connect(self.root / "grok-jobs.sqlite3")) as con:
            self.assertEqual(dict(con.execute("SELECT job_id,state FROM jobs").fetchall()),
                             {live: "running", done: "failed"})

    def test_submit_reasons_are_codes_the_coordinator_forwards(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
        try:
            from coordinator import Coordinator, SUBMIT_REASONS
        finally:
            sys.path.pop(0)
        self.assertEqual(self.adapter.submit("x" * 100_001, "big")["reason"], "invalid_task")
        self.cli.unlink()
        receipt = self.adapter.submit("task", "no-client")
        self.assertEqual(receipt, {"status": "not_submitted", "reason": "cli_or_profile_unavailable"})
        self.cli.touch()
        core = Coordinator(self.root / "requests.sqlite3", {"grok": self.adapter})
        with mock.patch.object(subject.subprocess, "Popen", side_effect=OSError("private path")):
            result = core.submit("grok", "task", "key")
        self.assertEqual((result["status"], result["reason"]), ("submission_uncertain", "worker_start_unconfirmed"))
        self.assertIn("worker_start_unconfirmed", SUBMIT_REASONS)

    def test_invalid_spawn_arguments_are_uncertain_and_never_retried(self):
        for exception in (ValueError("private value"), TypeError("private type")):
            with self.subTest(exception=type(exception).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                cli, profile = root / "cli", root / "profile"
                cli.touch()
                profile.mkdir()
                adapter = subject.DurableGrokAdapter(str(cli), "example-model", "high", str(profile), str(root), 2, 1)
                with mock.patch.object(subject.subprocess, "Popen", side_effect=exception) as spawn:
                    first = adapter.submit("synthetic task", "stable-key")
                    second = adapter.submit("synthetic task", "stable-key")
                self.assertEqual(first["status"], "submission_uncertain")
                self.assertEqual(first["reason"], "worker_start_unconfirmed")
                self.assertEqual(second["status"], "submission_uncertain")
                self.assertEqual(spawn.call_count, 1)
                self.assertNotIn("private", repr(first) + repr(second))

    def test_legacy_not_submitted_row_cannot_be_safely_released(self):
        job_id = self.insert("not_submitted", "not_submitted")
        self.assertEqual(subject.operator_release(str(self.root), job_id), "unsafe_state")
        self.assertEqual(len(subject.blocking_jobs(str(self.root))), 1)

    def test_unknown_identity_and_unclaimed_dispatch_remain_blocking(self):
        unknown = self.insert("running", "running", pid=1, identity="1:unknown")
        dispatching = self.insert("dispatching", "submission_uncertain")
        with mock.patch.object(subject, "_worker_identity_state", return_value="unknown"):
            self.assertEqual(subject.operator_release(str(self.root), unknown), "identity_unconfirmed")
            self.assertEqual(subject.operator_release(str(self.root), dispatching), "unsafe_state")

    def test_release_preserves_partial_result_and_rejects_late_update(self):
        job_id = self.insert("submission_uncertain", "submission_uncertain", pid=1, identity="1:gone")
        with closing(sqlite3.connect(self.root / "grok-jobs.sqlite3")) as con, con:
            result = {"status": "submission_uncertain", "job_id": job_id, "output": "partial answer"}
            con.execute("UPDATE jobs SET result_json=? WHERE job_id=?", (json.dumps(result), job_id))
        with mock.patch.object(subject, "_worker_identity_state", return_value="exited"):
            self.assertEqual(subject.operator_release(str(self.root), job_id), "released")
        self.assertEqual(self.adapter.poll(job_id)["output"], "partial answer")
        self.assertFalse(self.adapter.ledger.store(job_id, {"status":"succeeded", "job_id":job_id, "output":"late"}))
        self.assertEqual(self.adapter.poll(job_id)["output"], "partial answer")

    def test_listing_existing_ledger_does_not_create_job_directory(self):
        root = Path(self.temp.name).resolve() / "list-only"
        root.mkdir()
        db = root / "grok-jobs.sqlite3"
        with closing(sqlite3.connect(db)) as con, con:
            con.execute(subject._SCHEMA)
        self.assertEqual(subject.blocking_jobs(str(root)), [])
        self.assertFalse((root / "grok-jobs").exists())


if __name__ == "__main__":
    unittest.main()
