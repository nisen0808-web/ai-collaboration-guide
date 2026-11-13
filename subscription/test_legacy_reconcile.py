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
        self.boot = mock.patch.object(lr.boot_evidence, "snapshot", return_value={
            "kind": "linux_boot_id", "boot_id": "12345678-1234-1234-1234-123456789abc"}).start()
        self.real_changed = lr.boot_evidence.changed
        # Existing transaction/ledger tests simulate a restart. Platform marker
        # acquisition and comparison have separate tests in test_boot_evidence.
        self.restart = mock.patch.object(lr.boot_evidence, "changed",
                                         side_effect=lambda saved: lr._uptime_ms() < 100000).start()
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

    def test_uppercase_table_trigger_is_rejected(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("DROP TABLE jobs")
            con.execute("CREATE TABLE JOBS (id TEXT PRIMARY KEY, fingerprint TEXT, status TEXT, "
                        "error TEXT, heartbeat REAL, output_file TEXT)")
            con.execute("INSERT INTO JOBS VALUES ('old-job','digest','running',NULL,NULL,NULL)")
            con.execute("CREATE TRIGGER dangerous AFTER UPDATE ON JOBS BEGIN DELETE FROM JOBS; END")
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(len(self.rows()), 1)

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

    def test_new_ticket_uses_boot_marker_without_uptime_or_wall_clock(self):
        with mock.patch.object(lr, "_uptime_ms", side_effect=AssertionError("no uptime")), \
             mock.patch.object(lr.time, "time", side_effect=AssertionError("no wall clock")):
            lr.prepare("claude", self.db, self.job, self.ticket)
            record = json.loads(self.ticket.read_text())
            self.assertEqual(record["version"], 2)
            self.assertEqual(record["boot_evidence"], self.boot.return_value)
            self.assertNotIn("uptime_ms", record)
            self.restart.side_effect = None
            self.restart.return_value = True
            self.release()
        self.assertEqual(self.rows()[0][2], "operator_released")

    def test_v2_release_uses_real_comparison_with_synthetic_windows_events(self):
        old = {"record_id": 100, "time_created": "2026-01-01T00:00:00Z",
               "start_time": "2026-01-01T00:00:00Z"}
        # A later boot may have an earlier wall-clock timestamp.
        new = {"record_id": 200, "time_created": "2020-01-01T00:00:00Z",
               "start_time": "2020-01-01T00:00:00Z"}
        self.boot.return_value = lr.boot_evidence._windows_marker(old)
        self.restart.side_effect = self.real_changed
        self.prepare()
        before = self.rows()
        with mock.patch.object(lr.boot_evidence, "_kind", return_value="windows_event12"), \
             mock.patch.object(lr.boot_evidence, "_windows_event", side_effect=[old, old]):
            with self.assertRaisesRegex(ValueError, "no corroborating restart"):
                self.release()
        self.assertEqual(self.rows(), before)
        with mock.patch.object(lr.boot_evidence, "_kind", return_value="windows_event12"), \
             mock.patch.object(lr.boot_evidence, "_windows_event", side_effect=[old, new]), \
             mock.patch.object(lr, "_uptime_ms", side_effect=AssertionError("no uptime")):
            self.release()
        self.assertEqual(self.rows()[0][2], "operator_released")
        self.assertEqual(self.rows()[0][1], before[0][1])
        self.assertEqual(self.rows()[0][5], before[0][5])

    def test_v2_release_missing_real_comparison_history_keeps_slot(self):
        old = {"record_id": 100, "time_created": "2026-01-01T00:00:00Z",
               "start_time": "2026-01-01T00:00:00Z"}
        self.boot.return_value = lr.boot_evidence._windows_marker(old)
        self.restart.side_effect = self.real_changed
        self.prepare()
        before = self.rows()
        with mock.patch.object(lr.boot_evidence, "_kind", return_value="windows_event12"), \
             mock.patch.object(lr.boot_evidence, "_windows_event", side_effect=ValueError("boot_evidence_unavailable")):
            with self.assertRaisesRegex(ValueError, "boot_evidence_unavailable"):
                self.release()
        self.assertEqual(self.rows(), before)

    def test_new_ticket_missing_or_unavailable_marker_never_falls_back(self):
        self.prepare()
        before = self.rows()
        record = json.loads(self.ticket.read_text())
        del record["boot_evidence"]
        self.ticket.write_text(json.dumps(record))
        self.restart.side_effect = ValueError("invalid_boot_evidence")
        self.uptime.return_value = 1
        with self.assertRaisesRegex(ValueError, "invalid_boot_evidence"):
            self.release()
        self.restart.assert_called_with(None)
        self.assertEqual(self.rows(), before)
        self.restart.side_effect = ValueError("boot_evidence_unavailable")
        with self.assertRaisesRegex(ValueError, "boot_evidence_unavailable"):
            self.release()
        self.assertEqual(self.rows(), before)

    def test_unavailable_boot_marker_does_not_write_ticket_or_change_job(self):
        before = self.rows()
        self.boot.side_effect = ValueError("boot_evidence_unavailable")
        with self.assertRaisesRegex(ValueError, "boot_evidence_unavailable"):
            self.prepare()
        self.assertFalse(self.ticket.exists())
        self.assertEqual(self.rows(), before)

    def test_old_ticket_preserves_uptime_rule_without_using_new_markers(self):
        self.prepare()
        record = json.loads(self.ticket.read_text())
        record.update(version=1, uptime_ms=100000)
        del record["boot_evidence"]
        self.ticket.write_text(json.dumps(record))
        self.restart.side_effect = AssertionError("old ticket cannot use a new marker")
        self.uptime.return_value = 100001
        with self.assertRaisesRegex(ValueError, "no corroborating restart"):
            self.release()
        self.assertEqual(self.rows()[0][2], "reserved")
        self.uptime.return_value = 50000
        self.release()
        self.assertEqual(self.rows()[0][2], "operator_released")

    def test_fixed_reason_codes_never_include_private_details(self):
        cases = [(ValueError("no corroborating restart"), "no_corroborating_restart"),
                 (ValueError("ticket mismatch"), "ticket_mismatch"),
                 (ValueError("job has a worker identity"), "job_has_a_worker_identity"),
                 (ValueError("boot_evidence_unavailable"), "boot_evidence_unavailable"),
                 (OSError("sensitive-path"), "local_file_error"),
                 (sqlite3.OperationalError("sensitive-sql"), "ledger_unreadable"),
                 (json.JSONDecodeError("sensitive", "{private}", 1), "invalid_record"),
                 (ValueError("sensitive-data"), "not_confirmed")]
        for error, expected in cases:
            with self.subTest(reason=expected):
                out = io.StringIO()
                with mock.patch.object(lr, "prepare", side_effect=error), redirect_stderr(out):
                    self.assertEqual(lr.main(["prepare", "claude", "ledger", "job", "ticket"]), 1)
                self.assertIn("Reason: " + expected, out.getvalue())
                self.assertNotIn("sensitive", out.getvalue())
                self.assertNotIn("private", out.getvalue())

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
                with self.assertRaises(ValueError):
                    lr.prepare("claude", db, self.job, junction / "nested" / "ticket.json")
            lr.prepare("claude", db, self.job, self.ticket)
            self.uptime.return_value = 50000
            with mock.patch.object(lr.sqlite3, "connect", side_effect=AssertionError("must not open")):
                with self.assertRaises(ValueError):
                    lr.release(indirect, self.ticket, same_machine_local=True, stopped_dispatchers=True,
                               full_restart=True, cli_exited=True, updated_only=True)
                indirect_ticket = junction / "nested" / "ticket.json"
                shutil.copyfile(self.ticket, real / "nested" / "ticket.json")
                with self.assertRaises(ValueError):
                    lr.release(db, indirect_ticket, same_machine_local=True, stopped_dispatchers=True,
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
        for state, value in ((state, value) for state in ("submission_uncertain", "finished_uncertain")
                             for value in ("true", 1, None)):
            with self.subTest(state=state, value=value):
                body = json.dumps({"status": "submission_uncertain", "job_id": job,
                                   "local_exit_confirmed": value, "error": "process_still_running"})
                with closing(sqlite3.connect(self.db)) as con, con:
                    con.execute("UPDATE jobs SET state=?,result_json=?", (state, body))
                with self.assertRaises(ValueError):
                    lr.prepare("grok", self.db, job, self.ticket)
                self.assertFalse(self.ticket.exists())

    def test_contradictory_grok_terminal_recovers_without_replaying_request(self):
        from grok import durable_grok as durable
        cli, profile = self.root / "grok-cli", self.root / "profile"
        cli.touch()
        profile.mkdir()
        cases = [("missing", None), (False, None)] + [(True, error) for error in sorted(lr._EXIT_UNCONFIRMED)]
        for index, (flag, error) in enumerate(cases):
            with self.subTest(exit_flag=flag, error=error):
                state_root = self.root / ("grok-state-" + str(index))
                state_root.mkdir()
                adapter = durable.DurableGrokAdapter(str(cli), "synthetic-model", "high", str(profile),
                                                     str(state_root), max_concurrency=1)
                task, task_id = "synthetic task", "old-request"
                outcome, job_id = adapter.ledger.reserve(task_id, hashlib.sha256(task.encode()).hexdigest(), 1)
                self.assertEqual(outcome, "reserved")
                body = {"status": "submission_uncertain", "job_id": job_id, "output": "partial"}
                if flag != "missing":
                    body["local_exit_confirmed"] = flag
                if error is not None:
                    body["error"] = error
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
                with closing(sqlite3.connect(db)) as con:
                    audit = json.loads(con.execute("SELECT prior_control FROM legacy_reconcile_audit").fetchone()[0])
                if flag is True:
                    self.assertTrue(audit["local_exit_confirmed"])
                    self.assertEqual(audit["error"], error)
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
                 json.dumps({"status": "submission_uncertain", "job_id": job, "local_exit_confirmed": True, "error": "other"}),
                 json.dumps({"status": "submission_uncertain", "job_id": job, "local_exit_confirmed": True, "error": []}),
                 json.dumps({"status": "succeeded", "job_id": job}),
                 json.dumps({"status": "submission_uncertain", "job_id": str(uuid.uuid4())}), "{broken"]
        for body in cases:
            with self.subTest(body=body):
                with closing(sqlite3.connect(self.db)) as con, con:
                    con.execute("UPDATE jobs SET result_json=?", (body,))
                with self.assertRaises(ValueError):
                    lr.prepare("grok", self.db, job, self.ticket)
                self.assertFalse(self.ticket.exists())

    def test_grok_nonterminal_true_even_with_conflicting_error_is_rejected(self):
        self.db.unlink()
        job = str(uuid.uuid4())
        body = {"status": "submission_uncertain", "job_id": job,
                "local_exit_confirmed": True, "error": "process_still_running"}
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, task_key TEXT UNIQUE, task_digest TEXT, "
                        "state TEXT, worker_pid INTEGER, worker_identity TEXT, result_json TEXT)")
            con.execute("INSERT INTO jobs VALUES (?,?,?,'submission_uncertain',NULL,NULL,?)",
                        (job, "key", "digest", json.dumps(body)))
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
    @unittest.skipUnless(os.name == "nt", "Windows startup event smoke check")
    def test_real_windows_boot_evidence_refuses_same_boot_synthetic_release(self):
        # Only this temporary synthetic ledger is writable. No real task,
        # restart, cancellation or provider request is involved.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db, ticket = root / "jobs.sqlite3", root / "ticket.json"
            with closing(sqlite3.connect(db)) as con, con:
                con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, fingerprint TEXT, status TEXT, "
                            "error TEXT, heartbeat REAL, output_file TEXT)")
                con.execute("INSERT INTO jobs VALUES ('synthetic','key','reserved',NULL,NULL,NULL)")
            try:
                lr.prepare("claude", db, "synthetic", ticket)
            except ValueError as exc:
                if str(exc) == "boot_evidence_unavailable":
                    self.skipTest("local Windows startup event unavailable")
                raise
            self.assertEqual(json.loads(ticket.read_text())["boot_evidence"]["kind"], "windows_event12")
            with self.assertRaisesRegex(ValueError, "^no corroborating restart$"):
                lr.release(db, ticket, same_machine_local=True, stopped_dispatchers=True,
                           full_restart=True, cli_exited=True, updated_only=True)
            with closing(sqlite3.connect(db)) as con:
                self.assertEqual(con.execute("SELECT status FROM jobs").fetchone()[0], "reserved")
                self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='legacy_reconcile_audit'").fetchone())

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
