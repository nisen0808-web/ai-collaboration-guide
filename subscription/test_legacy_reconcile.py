"""Isolated tests: attestations are simulated, no live jobs are touched."""
import json
import hashlib
from contextlib import closing, redirect_stderr
import io
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

import legacy_reconcile as lr


class LegacyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "jobs.sqlite3"
        self.ticket = self.root / "ticket.json"
        self.job = "old-job"
        self.uptime = mock.patch.object(lr, "_uptime_ms", return_value=100000).start()
        self.addCleanup(mock.patch.stopall)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                        "status TEXT NOT NULL, error TEXT, heartbeat REAL, output_file TEXT)")
            con.execute("INSERT INTO jobs VALUES (?, ?, 'reserved', ?, NULL, ?)",
                        (self.job, "original-key-fingerprint", "old-error", "partial.txt"))

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self):
        self.uptime.return_value = 100000
        lr.prepare("claude", self.db, self.job, self.ticket)
        self.uptime.return_value = 50000

    def release(self, **override):
        flags = dict(same_machine_local=True, stopped_dispatchers=True, full_restart=True, cli_exited=True, updated_only=True)
        flags.update(override)
        return lr.release(self.db, self.ticket, **flags)

    def rows(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            return con.execute("SELECT * FROM jobs ORDER BY id").fetchall()

    def test_claude_release_preserves_key_result_other_rows_and_audit(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO jobs VALUES ('other','other-key','running',NULL,1,NULL)")
        before = self.rows()
        self.prepare()
        self.release()
        rows = self.rows()
        self.assertEqual(rows[0][1], "original-key-fingerprint")
        self.assertEqual(rows[0][2:4], ("operator_released", "operator_released"))
        self.assertEqual(rows[0][5], "partial.txt")
        self.assertEqual(rows[1], before[1])
        with closing(sqlite3.connect(self.db)) as con, con:
            audit = con.execute("SELECT prior_control FROM legacy_reconcile_audit").fetchone()[0]
        self.assertEqual(json.loads(audit), {"status": "reserved", "error": "old-error"})
        with self.assertRaises(ValueError):
            self.release()

    def test_each_attestation_is_mandatory(self):
        self.prepare()
        before = self.rows()
        for flag in ("same_machine_local", "stopped_dispatchers", "full_restart", "cli_exited", "updated_only"):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                self.release(**{flag: False})
            self.assertEqual(self.rows(), before)

    def test_changed_complete_row_refuses_release(self):
        self.prepare()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE jobs SET output_file='different' WHERE id=?", (self.job,))
        before = self.rows()
        with self.assertRaises(ValueError):
            self.release()
        self.assertEqual(self.rows(), before)

    def test_wrong_database_or_ticket_job_rejected(self):
        self.prepare()
        second = self.root / "second.db"
        with closing(sqlite3.connect(second)) as con, con:
            con.execute("CREATE TABLE jobs (id TEXT)")
        with self.assertRaises(ValueError):
            lr.release(second, self.ticket, same_machine_local=True, stopped_dispatchers=True,
                       full_restart=True, cli_exited=True, updated_only=True)
        ticket = json.loads(self.ticket.read_text())
        ticket["job_id"] = "other-job"
        self.ticket.write_text(json.dumps(ticket))
        with self.assertRaises(ValueError):
            self.release()

    def test_known_worker_claim_refuses_prepare(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE worker_claims (job_id TEXT PRIMARY KEY, pid INTEGER, identity TEXT)")
            con.execute("INSERT INTO worker_claims VALUES (?, 123, '123:456')", (self.job,))
        with self.assertRaises(ValueError):
            self.prepare()

    def test_trigger_rejected_and_job_unchanged(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TRIGGER dangerous AFTER UPDATE ON jobs BEGIN DELETE FROM jobs; END")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(len(self.rows()), 1)

    def test_existing_audit_trigger_cannot_change_other_jobs(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE legacy_reconcile_audit (operation_id TEXT)")
            con.execute("CREATE TRIGGER dangerous AFTER INSERT ON legacy_reconcile_audit BEGIN DELETE FROM jobs; END")
        before = self.rows()
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.rows(), before)

    def test_unexpected_audit_schema_aborts_transaction(self):
        self.prepare()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE legacy_reconcile_audit (operation_id TEXT)")
        before = self.rows()
        with self.assertRaises(ValueError):
            self.release()
        self.assertEqual(self.rows(), before)

    def test_sqlite_write_lock_prevents_concurrent_change(self):
        self.prepare()
        lock = sqlite3.connect(self.db)
        try:
            lock.execute("BEGIN IMMEDIATE")
            lock.execute("UPDATE jobs SET heartbeat=7 WHERE id=?", (self.job,))
            with self.assertRaises(sqlite3.OperationalError):
                self.release()
            lock.commit()
            with self.assertRaises(ValueError):
                self.release()
        finally:
            lock.close()

    def test_grok_retains_partial_body_and_task_key(self):
        self.db.unlink()
        job = str(uuid.uuid4())
        prior = {"status": "submission_uncertain", "job_id": job, "error": "old", "partial": "private"}
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_key TEXT UNIQUE, task_digest TEXT, "
                        "state TEXT, worker_pid INTEGER, worker_identity TEXT, result_json TEXT)")
            con.execute("INSERT INTO jobs VALUES (?,?,?,'dispatching',NULL,NULL,?)",
                        (job, "same-request-key", "same-digest", json.dumps(prior)))
        lr.prepare("grok", self.db, job, self.ticket)
        self.uptime.return_value = 50000
        self.release()
        with closing(sqlite3.connect(self.db)) as con, con:
            row = con.execute("SELECT task_key,task_digest,state,result_json FROM jobs").fetchone()
            audit = con.execute("SELECT prior_control FROM legacy_reconcile_audit").fetchone()[0]
        self.assertEqual(row[:3], ("same-request-key", "same-digest", "operator_released"))
        result = json.loads(row[3])
        self.assertEqual(result["partial"], "private")
        self.assertFalse(result["local_exit_confirmed"])
        self.assertEqual(result["release_basis"], "operator_confirmed")
        self.assertNotIn("private", audit)

    def test_grok_known_identity_rejected(self):
        self.db.unlink()
        job = str(uuid.uuid4())
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_key TEXT UNIQUE, task_digest TEXT, "
                        "state TEXT, worker_pid INTEGER, worker_identity TEXT, result_json TEXT)")
            con.execute("INSERT INTO jobs VALUES (?,?,?,'running',1,'1:2',?)",
                        (job, "key", "digest", json.dumps({"job_id": job})))
        with self.assertRaises(ValueError):
            lr.prepare("grok", self.db, job, self.ticket)

    def test_same_boot_and_uptime_unavailable_refuse_release(self):
        self.prepare()
        self.uptime.return_value = 100001
        with self.assertRaises(ValueError):
            self.release()
        self.uptime.side_effect = ValueError("unavailable")
        with self.assertRaises(ValueError):
            self.release()
        self.assertEqual(self.rows()[0][2], "reserved")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_windows_nested_junction_refuses_prepare_and_release(self):
        real = self.root / "real"
        state = real / "nested" / "state"
        state.mkdir(parents=True)
        db = state / "jobs.sqlite3"
        shutil.copyfile(self.db, db)
        junction = self.root / "junction"
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(real)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, check=False, shell=False)
        if result.returncode:
            self.skipTest("Windows could not create synthetic junction")
        try:
            indirect = junction / "nested" / "state" / "jobs.sqlite3"
            with mock.patch.object(lr.sqlite3, "connect", side_effect=AssertionError("must not open")):
                with self.assertRaises(ValueError):
                    lr.prepare("claude", indirect, self.job, self.ticket)
            lr.prepare("claude", db, self.job, self.ticket)
            self.uptime.return_value = 50000
            with mock.patch.object(lr.sqlite3, "connect", side_effect=AssertionError("must not open")):
                with self.assertRaises(ValueError):
                    lr.release(indirect, self.ticket, same_machine_local=True, stopped_dispatchers=True,
                               full_restart=True, cli_exited=True, updated_only=True)
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute("SELECT status FROM jobs WHERE id=?", (self.job,)).fetchone()[0], "reserved")
        finally:
            os.rmdir(junction)

    def test_generated_column_is_not_accepted(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("ALTER TABLE jobs ADD COLUMN generated TEXT GENERATED ALWAYS AS (status) VIRTUAL")
        with self.assertRaises(ValueError):
            self.prepare()

    def test_grok_contradictory_result_is_not_overwritten(self):
        self.db.unlink()
        job = str(uuid.uuid4())
        body = json.dumps({"status": "succeeded", "job_id": job, "result": "valuable"})
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_key TEXT UNIQUE, task_digest TEXT, "
                        "state TEXT, worker_pid INTEGER, worker_identity TEXT, result_json TEXT)")
            con.execute("INSERT INTO jobs VALUES (?,?,?,'running',NULL,NULL,?)", (job, "key", "digest", body))
        with self.assertRaises(ValueError):
            lr.prepare("grok", self.db, job, self.ticket)
        with closing(sqlite3.connect(self.db)) as con, con:
            self.assertEqual(con.execute("SELECT result_json FROM jobs").fetchone()[0], body)

    def test_grok_non_boolean_or_null_exit_confirmation_is_rejected(self):
        self.db.unlink()
        job = str(uuid.uuid4())
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_key TEXT UNIQUE, task_digest TEXT, "
                        "state TEXT, worker_pid INTEGER, worker_identity TEXT, result_json TEXT)")
            con.execute("INSERT INTO jobs VALUES (?,?,?,'submission_uncertain',NULL,NULL,'{}')",
                        (job, "key", "digest"))
        for value in ("true", 1, None):
            with self.subTest(value=value):
                body = json.dumps({"status": "submission_uncertain", "job_id": job,
                                   "local_exit_confirmed": value})
                with closing(sqlite3.connect(self.db)) as con, con:
                    con.execute("UPDATE jobs SET result_json=?", (body,))
                with self.assertRaises(ValueError):
                    lr.prepare("grok", self.db, job, self.ticket)
                self.assertFalse(self.ticket.exists())

    def test_contradictory_grok_terminal_recovers_without_replaying_request(self):
        from grok import durable_grok as durable
        cli, profile = self.root / "grok-cli", self.root / "profile"
        cli.touch()
        profile.mkdir()
        for index, flag in enumerate(("missing", False)):
            with self.subTest(exit_flag=flag):
                state_root = self.root / ("grok-state-" + str(index))
                state_root.mkdir()
                adapter = durable.DurableGrokAdapter(str(cli), "synthetic-model", "high", str(profile),
                                                     str(state_root), max_concurrency=1)
                task, task_id = "synthetic task", "old-request"
                outcome, job_id = adapter.ledger.reserve(task_id, hashlib.sha256(task.encode()).hexdigest(), 1)
                self.assertEqual(outcome, "reserved")
                body = {"status": "submission_uncertain", "job_id": job_id, "output": "partial"}
                if flag is False:
                    body["local_exit_confirmed"] = False
                db, ticket = state_root / "grok-jobs.sqlite3", state_root / "ticket.json"
                with closing(sqlite3.connect(db)) as con, con:
                    con.execute("UPDATE jobs SET state='finished_uncertain',result_json=? WHERE job_id=?",
                                (json.dumps(body), job_id))
                self.assertEqual(adapter.status()["local_available_slots"], 0)
                self.uptime.return_value = 100000
                lr.prepare("grok", db, job_id, ticket)
                self.uptime.return_value = 50000
                lr.release(db, ticket, same_machine_local=True, stopped_dispatchers=True,
                           full_restart=True, cli_exited=True, updated_only=True)
                self.assertEqual(adapter.status()["local_available_slots"], 1)
                result = adapter.poll(job_id)
                self.assertEqual(result["status"], "submission_uncertain")
                self.assertFalse(result["local_exit_confirmed"])
                self.assertEqual(result["output"], "partial")
                with mock.patch.object(durable.subprocess, "Popen", side_effect=AssertionError("must not spawn")):
                    replay = adapter.submit(task, task_id)
                self.assertEqual(replay, {"status": "submission_uncertain", "job_id": job_id, "replayed": True})

    def test_confirmed_or_corrupt_grok_terminal_cannot_be_reconciled(self):
        self.db.unlink()
        job = str(uuid.uuid4())
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_key TEXT UNIQUE, task_digest TEXT, "
                        "state TEXT, worker_pid INTEGER, worker_identity TEXT, result_json TEXT)")
            con.execute("INSERT INTO jobs VALUES (?,?,?,'finished_uncertain',NULL,NULL,'{}')",
                        (job, "key", "digest"))
        cases = [json.dumps({"status": "submission_uncertain", "job_id": job, "local_exit_confirmed": True}),
                 json.dumps({"status": "succeeded", "job_id": job}),
                 json.dumps({"status": "submission_uncertain", "job_id": str(uuid.uuid4())}), "{broken"]
        for body in cases:
            with self.subTest(body=body):
                with closing(sqlite3.connect(self.db)) as con, con:
                    con.execute("UPDATE jobs SET result_json=?", (body,))
                with self.assertRaises(ValueError):
                    lr.prepare("grok", self.db, job, self.ticket)
                self.assertFalse(self.ticket.exists())

    def test_released_claude_replay_never_spawns_and_new_task_gets_slot(self):
        from claude import durable_claude as durable
        task, model, effort = "synthetic task", "claude-synthetic", "medium"
        fingerprint = hashlib.sha256(json.dumps([task, model, effort], ensure_ascii=True).encode("ascii")).hexdigest()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE jobs SET fingerprint=? WHERE id=?", (fingerprint, self.job))
        profile, work = self.root / "profile", self.root / "work"
        profile.mkdir()
        work.mkdir()
        adapter = durable.DurableClaudeAdapter(str(self.root / "cli"), str(profile), str(work),
                                              str(self.root), model, effort, enabled=True, max_concurrency=1)
        self.assertEqual(adapter.status()["local_active"], 1)
        self.prepare()
        self.release()
        with mock.patch.object(durable.subprocess, "Popen", side_effect=OSError("simulated")) as spawn:
            replay = adapter.submit(task, self.job)
            self.assertEqual(replay, {"status": "submission_uncertain", "job_id": self.job, "replayed": True})
            spawn.assert_not_called()
            polled = adapter.poll(self.job)
            self.assertEqual(polled["status"], "submission_uncertain")
            self.assertEqual(polled["release_basis"], "operator_confirmed")
            self.assertFalse(polled["local_exit_confirmed"])
            new = adapter.submit("different task", "new-job")
            self.assertEqual(new["status"], "submission_uncertain")
            self.assertEqual(new["job_id"], "new-job")
            spawn.assert_called_once()

    def test_contradictory_legacy_claude_terminal_can_be_reconciled_without_replay(self):
        from claude import durable_claude as durable
        task, model, effort = "synthetic task", "claude-synthetic", "medium"
        fingerprint = hashlib.sha256(json.dumps([task, model, effort], ensure_ascii=True).encode("ascii")).hexdigest()
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE jobs SET fingerprint=?,status='finished_uncertain',error='process_exit_unconfirmed' WHERE id=?",
                        (fingerprint, self.job))
        profile, work = self.root / "profile", self.root / "work"
        profile.mkdir()
        work.mkdir()
        adapter = durable.DurableClaudeAdapter(str(self.root / "cli"), str(profile), str(work),
                                              str(self.root), model, effort, enabled=True, max_concurrency=1)
        self.assertEqual(adapter.status()["local_active"], 1)
        self.assertFalse(adapter.poll(self.job)["local_exit_confirmed"])
        self.prepare()
        self.release()
        self.assertEqual(adapter.status()["local_active"], 0)
        self.assertEqual(adapter.poll(self.job)["status"], "submission_uncertain")
        with mock.patch.object(durable.subprocess, "Popen", side_effect=AssertionError("must not spawn")):
            replay = adapter.submit(task, self.job)
        self.assertEqual(replay, {"status": "submission_uncertain", "job_id": self.job, "replayed": True})

    def test_confirmed_claude_terminal_is_not_legacy_blocking_job(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE jobs SET status='finished_uncertain',error='result_unavailable' WHERE id=?", (self.job,))
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse(self.ticket.exists())

    def test_released_grok_replay_never_spawns_and_new_task_gets_slot(self):
        from grok import durable_grok as durable
        self.db.unlink()
        cli, profile = self.root / "grok-cli", self.root / "profile"
        cli.touch()
        profile.mkdir()
        adapter = durable.DurableGrokAdapter(str(cli), "synthetic-model", "high", str(profile),
                                            str(self.root), max_concurrency=1)
        task, task_id = "synthetic task", "original-request"
        digest = hashlib.sha256(task.encode("utf-8")).hexdigest()
        state, job_id = adapter.ledger.reserve(task_id, digest, 1)
        self.assertEqual(state, "reserved")
        db = self.root / "grok-jobs.sqlite3"
        lr.prepare("grok", db, job_id, self.ticket)
        self.uptime.return_value = 50000
        lr.release(db, self.ticket, same_machine_local=True, stopped_dispatchers=True,
                   full_restart=True, cli_exited=True, updated_only=True)
        with mock.patch.object(durable.subprocess, "Popen", side_effect=OSError("simulated")) as spawn:
            replay = adapter.submit(task, task_id)
            self.assertEqual(replay, {"status": "submission_uncertain", "job_id": job_id, "replayed": True})
            spawn.assert_not_called()
            polled = adapter.poll(job_id)
            self.assertEqual(polled["status"], "submission_uncertain")
            self.assertEqual(polled["release_basis"], "operator_confirmed")
            self.assertFalse(polled["local_exit_confirmed"])
            new = adapter.submit("different task", "new-request")
            self.assertEqual(new["status"], "submission_uncertain")
            self.assertNotEqual(new["job_id"], job_id)
            spawn.assert_called_once()

    def test_release_error_reports_unconfirmed_without_retry(self):
        error_output = io.StringIO()
        argv = ["release", "ledger", "ticket", "--attest-same-machine-local",
                "--attest-dispatchers-stopped", "--attest-full-restart",
                "--attest-cli-exited", "--attest-new-version-only"]
        with mock.patch.object(lr, "release", side_effect=sqlite3.OperationalError("private")) as call, \
             redirect_stderr(error_output):
            self.assertNotEqual(lr.main(argv), 0)
        call.assert_called_once()
        message = error_output.getvalue().lower()
        self.assertIn("operation not confirmed", message)
        self.assertIn("do not retry automatically", message)
        self.assertNotIn("nothing changed", message)
        self.assertNotIn("private", message)

    def test_prepare_write_error_reports_unconfirmed_without_retry(self):
        error_output = io.StringIO()
        with mock.patch.object(lr, "prepare", side_effect=OSError("private")) as call, \
             redirect_stderr(error_output):
            self.assertNotEqual(lr.main(["prepare", "claude", "ledger", "job", "ticket"]), 0)
        call.assert_called_once()
        message = error_output.getvalue().lower()
        self.assertIn("operation not confirmed", message)
        self.assertIn("do not retry automatically", message)
        self.assertNotIn("nothing changed", message)
        self.assertNotIn("private", message)


class UptimeSmokeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt" or sys.platform.startswith("linux"), "unsupported platform")
    def test_real_local_uptime_is_positive_and_nondecreasing(self):
        # Read-only check of the real platform API. No reboot is attempted or proved.
        first = lr._uptime_ms()
        second = lr._uptime_ms()
        self.assertIs(type(first), int)
        self.assertGreater(first, 0)
        self.assertGreaterEqual(second, first)


if __name__ == "__main__":
    unittest.main()
