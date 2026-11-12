"""Synthetic process and durable-worker recovery tests; no vendor calls."""
import io
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import claude_adapter
import durable_claude
from readonly_ledger import ReadOnlyClaudeLedger


class ConfirmedExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cli = root / "cli"
        self.cli.touch()
        self.profile, self.work, self.state = (root / x for x in ("profile", "work", "state"))
        for path in (self.profile, self.work, self.state):
            path.mkdir()
        self.args = (str(self.cli), str(self.profile), str(self.work),
                     str(self.state), "model-example", "medium")

    def tearDown(self):
        self.tmp.cleanup()

    def _run_receipt(self, receipt, subsequent=None, *, fail_write=False):
        persistent = durable_claude.DurableClaudeAdapter(*self.args, enabled=True, max_concurrency=1)
        with durable_claude._connect(persistent.db) as con:
            con.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", ("job-1", "digest", "reserved", None, None, None))
        message = {"task": "synthetic", "cli_path": str(self.cli), "profile_dir": str(self.profile),
                   "work_dir": str(self.work), "model": "model-example", "effort": "medium", "timeout_seconds": 900}

        class FakeAdapter:
            def __init__(self, *_args, **_kwargs):
                pass
            def submit(self, task, task_id):
                return receipt
            def poll(self, job_id):
                return subsequent

        fake_stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode("utf-8")))
        write_control = (patch.object(durable_claude, "_atomic_json", side_effect=OSError("synthetic write failure"))
                         if fail_write else nullcontext())
        with patch.object(durable_claude, "ClaudeAdapter", FakeAdapter), \
             patch.object(durable_claude.sys, "stdin", fake_stdin), write_control as writer:
            durable_claude._run_worker(str(self.state), "job-1")
        self.write_attempts = writer.call_count if fail_write else None
        return persistent, persistent.poll("job-1")

    def test_uncertain_initial_receipt_retains_original_slot(self):
        adapter, result = self._run_receipt({"status": "submission_uncertain", "job_id": "job-1"})
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertEqual(adapter.status()["local_active"], 1)
        self.assertEqual(adapter.submit("other", "job-2")["reason"], "provider_busy")

    def test_confirmed_local_exit_frees_only_slot_not_request(self):
        adapter, result = self._run_receipt({"status": "queued"},
            {"status": "submission_uncertain", "error": "provider_process_failed", "local_exit_confirmed": True})
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertTrue(result["local_exit_confirmed"])
        self.assertEqual(adapter.status()["local_active"], 0)
        self.assertEqual(adapter.status()["local_finished_unresolved"], 1)
        with patch.object(durable_claude.subprocess, "Popen", side_effect=AssertionError("must not relaunch")):
            with durable_claude._connect(adapter.db) as con:
                fingerprint = hashlib.sha256(json.dumps(["synthetic", "model-example", "medium"],
                    ensure_ascii=True).encode("ascii")).hexdigest()
                con.execute("UPDATE jobs SET fingerprint=? WHERE id='job-1'", (fingerprint,))
            replay = adapter.submit("synthetic", "job-1")
            self.assertEqual(replay["status"], "submission_uncertain")
            self.assertTrue(replay["replayed"])

    def test_unconfirmed_exit_retains_slot(self):
        adapter, result = self._run_receipt({"status": "queued"},
            {"status": "submission_uncertain", "error": "provider_result_unavailable", "local_exit_confirmed": False})
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertEqual(adapter.status()["local_active"], 1)

    def test_contradictory_exit_evidence_retains_slot_and_is_not_reported_as_confirmed(self):
        for error in durable_claude._EXIT_UNCONFIRMED:
            with self.subTest(error=error):
                # Exercise the actual worker transition, not a normal success path.
                adapter, result = self._run_receipt({"status": "queued"},
                    {"status": "submission_uncertain", "error": error, "local_exit_confirmed": True})
                self.assertEqual(result["status"], "submission_uncertain")
                self.assertEqual(adapter.status()["local_active"], 1)
                self.assertNotEqual(result.get("local_exit_confirmed"), True)
                with durable_claude._connect(adapter.db) as con:
                    con.execute("DELETE FROM worker_claims")
                    con.execute("DELETE FROM jobs")

    def test_legacy_conflicting_finished_record_stays_blocking_and_unconfirmed(self):
        adapter = durable_claude.DurableClaudeAdapter(*self.args, enabled=True, max_concurrency=1)
        with durable_claude._connect(adapter.db) as con:
            con.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)",
                        ("legacy", "digest", "finished_uncertain", "process_exit_unconfirmed", None, None))
        readonly = ReadOnlyClaudeLedger(str(self.state))
        for ledger in (adapter, readonly):
            self.assertEqual(ledger.status()["local_active"], 1)
            self.assertEqual(ledger.status()["local_finished_unresolved"], 0)
            self.assertFalse(ledger.poll("legacy")["local_exit_confirmed"])
        self.assertEqual(adapter.submit("other", "new")["reason"], "provider_busy")
        self.assertEqual(durable_claude.operator_release(str(self.state), "legacy"), "identity_unconfirmed")
        self.assertEqual([job["job_id"] for job in durable_claude.blocking_jobs(str(self.state))], ["legacy"])

    def test_write_failure_with_confirmed_exit_frees_slot_without_result_or_relaunch(self):
        adapter, result = self._run_receipt({"status": "queued"},
            {"status": "succeeded", "output": "synthetic", "local_exit_confirmed": True}, fail_write=True)
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertEqual(result["error"], "result_unavailable")
        self.assertEqual(self.write_attempts, 1)
        self.assertTrue(result["local_exit_confirmed"])
        self.assertNotIn("output", result)
        self.assertEqual(adapter.status()["local_active"], 0)
        fingerprint = hashlib.sha256(json.dumps(["synthetic", "model-example", "medium"],
                                                ensure_ascii=True).encode("ascii")).hexdigest()
        with durable_claude._connect(adapter.db) as con:
            con.execute("UPDATE jobs SET fingerprint=? WHERE id='job-1'", (fingerprint,))
        with patch.object(durable_claude.subprocess, "Popen", side_effect=AssertionError("must not relaunch")):
            replay = adapter.submit("synthetic", "job-1")
        self.assertEqual(replay["status"], "submission_uncertain")
        self.assertEqual(replay["job_id"], "job-1")
        self.assertTrue(replay["replayed"])

    def test_success_without_confirmed_exit_stops_before_write_and_retains_slot(self):
        adapter, result = self._run_receipt({"status": "queued"},
            {"status": "succeeded", "output": "synthetic"}, fail_write=True)
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertEqual(result["error"], "invalid_provider_response")
        self.assertEqual(self.write_attempts, 0)
        self.assertNotIn("output", result)
        self.assertEqual(adapter.status()["local_active"], 1)

    def test_operator_confirmed_exit_does_not_free_uncertain_or_failed_job(self):
        for status in ("submission_uncertain", "failed"):
            with self.subTest(status=status):
                adapter, result = self._run_receipt({"status": "queued"},
                    {"status": status, "error": "provider_process_failed", "local_exit_confirmed": True,
                     "release_basis": "operator_confirmed"})
                self.assertEqual(result["status"], "submission_uncertain")
                self.assertNotEqual(result.get("local_exit_confirmed"), True)
                self.assertEqual(adapter.status()["local_active"], 1)
                with durable_claude._connect(adapter.db) as con:
                    con.execute("DELETE FROM worker_claims")
                    con.execute("DELETE FROM jobs")

    def test_operator_confirmed_success_stops_before_failing_write_and_keeps_slot(self):
        adapter, result = self._run_receipt({"status": "queued"},
            {"status": "succeeded", "output": "synthetic", "local_exit_confirmed": True,
             "release_basis": "operator_confirmed"}, fail_write=True)
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertEqual(result["error"], "invalid_provider_response")
        self.assertEqual(self.write_attempts, 0)
        self.assertNotIn("output", result)
        self.assertEqual(adapter.status()["local_active"], 1)
        fingerprint = hashlib.sha256(json.dumps(["synthetic", "model-example", "medium"],
                                                ensure_ascii=True).encode("ascii")).hexdigest()
        with durable_claude._connect(adapter.db) as con:
            con.execute("UPDATE jobs SET fingerprint=? WHERE id='job-1'", (fingerprint,))
        with patch.object(durable_claude.subprocess, "Popen", side_effect=AssertionError("must not relaunch")):
            replay = adapter.submit("synthetic", "job-1")
        self.assertEqual(replay["status"], "submission_uncertain")
        self.assertTrue(replay["replayed"])

    def test_confirmed_and_prelaunch_failures_release_but_unknown_failures_do_not(self):
        cases = (
            ({"status": "failed", "error": "provider_reported_error", "local_exit_confirmed": True}, "failed", 0),
            ({"status": "failed", "error": "provider_reported_error"}, "submission_uncertain", 1),
            ({"status": "failed", "error": "process_exit_unconfirmed", "local_exit_confirmed": True}, "submission_uncertain", 1),
            ({"status": "failed", "error": "preflight_error"}, "failed", 0),
            ({"status": "failed", "error": "preflight_error", "release_basis": "operator_confirmed"}, "submission_uncertain", 1),
        )
        for evidence, expected, slots in cases:
            with self.subTest(evidence=evidence):
                adapter, result = self._run_receipt({"status": "queued"}, evidence)
                self.assertEqual(result["status"], expected)
                self.assertEqual(adapter.status()["local_active"], slots)
                with durable_claude._connect(adapter.db) as con:
                    con.execute("DELETE FROM worker_claims")
                    con.execute("DELETE FROM jobs")

    def test_successful_result_includes_local_exit_evidence(self):
        auth = subprocess.CompletedProcess([], 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                                               "subscriptionType": "max"}), "")
        adapter = claude_adapter.ClaudeAdapter(str(self.cli), str(self.profile), str(self.work),
                                               "model-example", "medium", enabled=True)
        adapter._jobs["job-1"] = {"status": "queued"}
        class FakeProcess:
            returncode = 0
            def communicate(self, **kwargs):
                return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "synthetic"}), ""
        with patch.object(claude_adapter.subprocess, "run", return_value=auth), \
             patch.object(claude_adapter.subprocess, "Popen", return_value=FakeProcess()):
            adapter._run_worker("job-1", "synthetic")
        self.assertEqual(adapter.poll("job-1")["status"], "succeeded")
        self.assertTrue(adapter.poll("job-1")["local_exit_confirmed"])

    def test_only_completed_cli_sets_exit_evidence(self):
        auth = subprocess.CompletedProcess([], 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                                               "subscriptionType": "max"}), "")
        for returncode, response in ((1, "error"), (0, "not-json")):
            adapter = claude_adapter.ClaudeAdapter(str(self.cli), str(self.profile), str(self.work),
                                                   "model-example", "medium", enabled=True)
            adapter._jobs["job-1"] = {"status": "queued"}
            class FakeProcess:
                def __init__(self):
                    self.returncode = returncode
                def communicate(self, **kwargs):
                    return response, ""
            with patch.object(claude_adapter.subprocess, "run", return_value=auth), \
                 patch.object(claude_adapter.subprocess, "Popen", return_value=FakeProcess()):
                adapter._run_worker("job-1", "synthetic")
            self.assertEqual(adapter.poll("job-1")["status"], "submission_uncertain")
            self.assertTrue(adapter.poll("job-1")["local_exit_confirmed"])


if __name__ == "__main__":
    unittest.main()
