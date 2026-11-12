"""Synthetic jobs only; one test starts a local fake worker, never Grok."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import durable_grok as subject


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "tool-state"
        self.root.mkdir()
        self.profile = self.base / "synthetic-profile"
        self.profile.mkdir()
        self.cli = self.base / "synthetic-cli"
        self.cli.touch()

    def tearDown(self):
        self.temp.cleanup()

    def adapter(self, max_concurrency=1):
        return subject.DurableGrokAdapter(str(self.cli), "example-model", "high", str(self.profile),
                                          str(self.root), 2, max_concurrency)

    def test_worker_passes_synthetic_network_settings_but_not_api_key(self):
        synthetic = {"HTTPS_PROXY": "http://example.invalid:8080",
                     "NODE_EXTRA_CA_CERTS": "synthetic-ca-path",
                     "XAI_API_KEY": "synthetic-key"}
        with mock.patch.dict(os.environ, synthetic, clear=True):
            env = subject._worker_env()
        self.assertEqual(env.get("HTTPS_PROXY"), synthetic["HTTPS_PROXY"])
        self.assertEqual(env.get("NODE_EXTRA_CA_CERTS"), synthetic["NODE_EXTRA_CA_CERTS"])
        self.assertNotIn("XAI_API_KEY", env)

    def test_detached_worker_disables_bytecode_writes(self):
        command = subject._worker_command(self.root, "synthetic-job")
        self.assertEqual(command[1], "-B")
        self.assertEqual(command[-2:], [str(self.root), "synthetic-job"])

    def test_real_worker_command_does_not_create_bytecode(self):
        source_dir = Path(subject.__file__).resolve().parent
        isolated_dir = self.base / "isolated-source"
        isolated_dir.mkdir()
        for source in source_dir.glob("*.py"):
            shutil.copyfile(source, isolated_dir / source.name)
        with mock.patch.object(subject, "__file__", str(isolated_dir / "durable_grok.py")):
            command = subject._worker_command(self.root, "not-a-job")
        completed = subprocess.run(command, capture_output=True, timeout=20, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(list(isolated_dir.rglob("__pycache__")), [])

    def test_worker_omits_invalid_synthetic_network_value(self):
        with mock.patch.object(subject.os, "environ", {"HTTPS_PROXY": "invalid\0value"}):
            env = subject._worker_env()
        self.assertNotIn("HTTPS_PROXY", env)

    def test_atomic_reservation_survives_new_adapter_and_no_key_is_passed(self):
        first = self.adapter()
        second = self.adapter()
        with mock.patch.dict(os.environ, {"XAI_API_KEY": "synthetic-secret", "GROK_CONFIG": "synthetic-config"}), \
             mock.patch.object(subject.subprocess, "Popen", return_value=mock.Mock(pid=123)) as launch:
            result = first.submit("synthetic task", "task-1")
            self.assertEqual(result["status"], "queued")
            args, kwargs = launch.call_args
            self.assertNotIn("synthetic task", repr(args))
            self.assertNotIn("XAI_API_KEY", kwargs["env"])
            self.assertNotIn("GROK_CONFIG", kwargs["env"])
            self.assertFalse(kwargs["shell"])
            job_id = result["job_id"]
            request = json.loads((self.root / "grok-jobs" / job_id / "request.json").read_text())
            self.assertEqual(request["task"], "synthetic task")
            self.assertNotIn("synthetic-secret", repr(request))
            replay = second.submit("synthetic task", "task-1")
            self.assertEqual(replay["job_id"], job_id)
            self.assertTrue(replay["replayed"])
            self.assertEqual(second.submit("different", "task-1")["reason"], "task_id_conflict")
            self.assertEqual(second.submit("another task", "task-2")["reason"], "provider_busy")
            self.assertEqual(second.poll(job_id)["status"], "submission_uncertain")
            self.assertEqual(launch.call_count, 1)

    def test_start_exception_cannot_overwrite_worker_result(self):
        adapter = self.adapter()
        def racing_worker(argv, **kwargs):
            job_id = argv[-1]
            self.assertTrue(adapter.ledger.claim(job_id))
            adapter.ledger.store(job_id, {"status": "succeeded", "job_id": job_id,
                                          "output": "synthetic result"})
            raise OSError("simulated ambiguous parent error")
        with mock.patch.object(subject.subprocess, "Popen", side_effect=racing_worker) as launch:
            result = adapter.submit("synthetic task", "task-1")
            self.assertEqual(result["status"], "submission_uncertain")
            self.assertEqual(adapter.poll(result["job_id"])["status"], "succeeded")
            self.assertEqual(launch.call_count, 1)

    def test_detached_fake_worker_result_survives_new_adapter_instance(self):
        fake = self.base / "fake_worker.py"
        fake.write_text('''import sys, time\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\nimport durable_grok\ndef fake_execute(self, job_id, task):\n    self._update(job_id, status="running", phase="synthetic")\n    self._update(job_id, status="submission_uncertain", error="process_still_running")\n    deadline = time.monotonic() + 8\n    while not Path(sys.argv[4]).exists() and time.monotonic() < deadline:\n        time.sleep(0.01)\n    self._update(job_id, status="succeeded", output="synthetic result", error=None)\ndurable_grok.GrokAdapter._execute = fake_execute\nraise SystemExit(durable_grok.worker_main(sys.argv[2], sys.argv[3]))\n''', encoding="utf-8")
        module_dir = str(Path(subject.__file__).resolve().parent)
        release = self.base / "release-fake-worker"
        def fake_command(root, job_id):
            return [sys.executable, str(fake), module_dir, str(root), job_id, str(release)]
        first = self.adapter()
        spawned = []
        real_popen = subprocess.Popen
        def start_process(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            spawned.append(child)
            return child
        with mock.patch.object(subject, "_worker_command", side_effect=fake_command), \
             mock.patch.object(subject.subprocess, "Popen", side_effect=start_process) as launch:
            result = first.submit("synthetic private task", "task-1")
            self.assertEqual(result["status"], "queued")
            self.assertNotIn("synthetic private task", repr(launch.call_args.args))
            job_id = result["job_id"]
            second = self.adapter()
            deadline = time.monotonic() + 8
            saw_worker_uncertain = False
            while time.monotonic() < deadline:
                state = second.poll(job_id)
                if state.get("error") == "process_still_running" and not saw_worker_uncertain:
                    saw_worker_uncertain = True
                    self.assertEqual(second.status()["local_available_slots"], 0)
                    replay = second.submit("synthetic private task", "task-1")
                    self.assertEqual(replay["job_id"], job_id)
                    release.touch()
                if state["status"] == "succeeded":
                    break
                time.sleep(0.03)
            self.assertTrue(saw_worker_uncertain)
            self.assertEqual(state["status"], "succeeded")
            self.assertEqual(state["output"], "synthetic result")
            self.assertEqual(launch.call_count, 1)
            self.assertFalse((self.root / "grok-jobs" / job_id / "request.json").exists())
            self.assertEqual(second.status()["local_available_slots"], 1)
            replay = second.submit("synthetic private task", "task-1")
            self.assertEqual(replay["reason"], "already_submitted")
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(subject.worker_main(str(self.root), job_id), 2)
            self.assertEqual(spawned[0].wait(timeout=5), 0)

    def test_unknown_job_is_uncertain(self):
        self.assertEqual(self.adapter().poll("not-a-job")["status"], "submission_uncertain")

    def test_find_job_reads_existing_mapping_without_starting(self):
        adapter = self.adapter()
        with mock.patch.object(subject.subprocess, "Popen", return_value=mock.Mock(pid=123)) as launch:
            result = adapter.submit("synthetic task", "task-1")
            self.assertEqual(adapter.find_job("task-1"), result["job_id"])
            self.assertIsNone(adapter.find_job("missing"))
            self.assertIsNone(adapter.find_job("\ud800"))
            self.assertEqual(launch.call_count, 1)

    def test_status_distinguishes_live_identity_from_unknown_without_releasing(self):
        adapter = self.adapter(max_concurrency=2)
        with mock.patch.object(subject.subprocess, "Popen", return_value=mock.Mock(pid=123)):
            first = adapter.submit("task one", "task-1")
            second = adapter.submit("task two", "task-2")
        with adapter.ledger._connect() as con:
            con.execute("UPDATE jobs SET state='running',worker_pid=?,worker_identity=? WHERE job_id=?",
                        (123, "confirmed-identity", first["job_id"]))
        with mock.patch.object(subject, "_process_identity", return_value="confirmed-identity"):
            status = adapter.status()
        self.assertEqual(status["local_active_jobs"], 2)
        self.assertEqual(status["local_observed_inflight"], 1)
        self.assertEqual(status["local_uncertain"], 1)
        self.assertEqual(status["local_available_slots"], 0)
        self.assertEqual(adapter.submit("third", "task-3")["reason"], "provider_busy")

    def test_request_write_failure_before_spawn_releases_slot(self):
        adapter = self.adapter()
        with mock.patch.object(adapter.ledger, "write_request", side_effect=OSError("synthetic failure")), \
             mock.patch.object(subject.subprocess, "Popen", side_effect=AssertionError("started")) as launch:
            result = adapter.submit("task", "task-1")
            replay = adapter.submit("task", "task-1")
            replay_after_restart = self.adapter().submit("task", "task-1")
            conflict = adapter.submit("different task", "task-1")
            launch.assert_not_called()
        self.assertEqual(result["status"], "not_submitted")
        self.assertEqual(result["reason"], "state_write_failed")
        self.assertNotIn("job_id", result)
        for item in (replay, replay_after_restart):
            self.assertEqual(item["status"], "not_submitted")
            self.assertEqual(item["reason"], "state_write_failed")
            self.assertTrue(item["replayed"])
            self.assertNotIn("job_id", item)
        self.assertEqual(conflict["reason"], "task_id_conflict")
        self.assertEqual(adapter.status()["local_available_slots"], 1)

    def test_transient_double_write_failure_recovers_slot_without_dispatch(self):
        adapter = self.adapter()
        with mock.patch.object(adapter.ledger, "write_request", side_effect=OSError("request failure")), \
             mock.patch.object(subject.subprocess, "Popen", side_effect=AssertionError("started")) as launch:
            with mock.patch.object(adapter.ledger, "finish_known_unstarted", side_effect=OSError("still unavailable")):
                result = adapter.submit("task", "task-1")
                self.assertEqual(result["status"], "submission_uncertain")
                self.assertTrue(result.get("job_id"))
                self.assertEqual(adapter.status()["local_available_slots"], 0)
            # State storage has recovered: a read reconciles the known
            # pre-launch failure without dispatching or changing task keys.
            self.assertEqual(adapter.status()["local_available_slots"], 1)
            replay = adapter.submit("task", "task-1")
            self.assertEqual(replay["status"], "not_submitted")
            self.assertEqual(replay["reason"], "state_write_failed")
            self.assertTrue(replay["replayed"])
            self.assertEqual(self.adapter().submit("task", "task-1")["status"], "not_submitted")
            launch.assert_not_called()

    def test_recovery_does_not_overwrite_a_claimed_worker(self):
        adapter = self.adapter()
        with mock.patch.object(adapter.ledger, "write_request", side_effect=OSError("request failure")), \
             mock.patch.object(subject.subprocess, "Popen") as launch:
            with mock.patch.object(adapter.ledger, "finish_known_unstarted", side_effect=OSError("ledger failure")):
                result = adapter.submit("task", "task-1")
            self.assertEqual(result["status"], "submission_uncertain")
            job_id = adapter.ledger.find_job("task-1")
            with adapter.ledger._connect() as con:
                con.execute("UPDATE jobs SET state='running', worker_pid=? WHERE job_id=?", (123, job_id))
            self.assertEqual(adapter.status()["local_available_slots"], 0)
            self.assertEqual(adapter.ledger.get(job_id).state, "running")
            launch.assert_not_called()

    def test_lost_process_memory_does_not_guess_unstarted(self):
        adapter = self.adapter()
        with mock.patch.object(adapter.ledger, "write_request", side_effect=OSError("request failure")), \
             mock.patch.object(subject.subprocess, "Popen") as launch:
            with mock.patch.object(adapter.ledger, "finish_known_unstarted", side_effect=OSError("ledger failure")):
                result = adapter.submit("task", "task-1")
            self.assertEqual(result["status"], "submission_uncertain")
            restarted = self.adapter()
            self.assertEqual(restarted.status()["local_available_slots"], 0)
            replay = restarted.submit("task", "task-1")
            self.assertEqual(replay["status"], "submission_uncertain")
            launch.assert_not_called()

    def test_request_write_race_with_claim_never_overwrites_worker(self):
        adapter = self.adapter()
        def claimed_then_write_fails(job_id, request):
            with adapter.ledger._connect() as con:
                con.execute("UPDATE jobs SET state='running',worker_pid=?,result_json=? WHERE job_id=?",
                            (123, json.dumps({"status": "running", "job_id": job_id}), job_id))
            raise OSError("request failure with competing claim")
        with mock.patch.object(adapter.ledger, "write_request", side_effect=claimed_then_write_fails), \
             mock.patch.object(subject.subprocess, "Popen") as launch:
            result = adapter.submit("task", "task-1")
            self.assertEqual(result["status"], "submission_uncertain")
            self.assertTrue(result.get("job_id"))
            job = adapter.ledger.get(result["job_id"])
            self.assertEqual(job.state, "running")
            self.assertEqual(job.result["status"], "running")
            self.assertEqual(adapter.status()["local_available_slots"], 0)
            replay = adapter.submit("task", "task-1")
            self.assertEqual(replay["status"], "submission_uncertain")
            self.assertEqual(replay["job_id"], result["job_id"])
            launch.assert_not_called()

    def test_legacy_unconfirmed_terminal_is_not_promoted_on_poll_or_replay(self):
        adapter = self.adapter()
        with mock.patch.object(adapter.ledger, "write_request", side_effect=OSError("request failure")), \
             mock.patch.object(subject.subprocess, "Popen") as launch:
            self.assertEqual(adapter.submit("task", "task-1")["status"], "not_submitted")
            job_id = adapter.find_job("task-1")
            with adapter.ledger._connect() as con:
                # Old generic rows cannot prove that another worker had not
                # already been launched by an earlier version.
                con.execute("UPDATE jobs SET state='not_submitted' WHERE job_id=?", (job_id,))
            self.assertEqual(adapter.poll(job_id)["status"], "submission_uncertain")
            self.assertEqual(adapter.submit("task", "task-1")["status"], "submission_uncertain")
            self.assertEqual(adapter.ledger.capacity(), 1)
            self.assertEqual(adapter.status()["local_available_slots"], 0)
            self.assertEqual(adapter.submit("another", "task-2")["reason"], "provider_busy")
            self.assertEqual(adapter.ledger.get(job_id).state, "not_submitted")
            # Both a genuinely unstarted legacy record and one with a worker
            # identity stay conservative; neither is rewritten automatically.
            with adapter.ledger._connect() as con:
                con.execute("UPDATE jobs SET worker_pid=123,worker_identity=? WHERE job_id=?",
                            ("old-worker", job_id))
            self.assertEqual(adapter.status()["local_available_slots"], 0)
            self.assertEqual(adapter.submit("another", "task-2")["reason"], "provider_busy")
            self.assertEqual(adapter.ledger.get(job_id).worker_identity, "old-worker")
            with adapter.ledger._connect() as con:
                con.execute("UPDATE jobs SET state=?,worker_pid=NULL,worker_identity=? WHERE job_id=?",
                            (subject._PRELAUNCH_ABORTED, "unknown-identity", job_id))
            self.assertEqual(adapter.poll(job_id)["status"], "submission_uncertain")
            self.assertEqual(adapter.status()["local_available_slots"], 0)
            with adapter.ledger._connect() as con:
                con.execute("UPDATE jobs SET worker_identity=NULL,result_json=? WHERE job_id=?",
                            ("invalid-json", job_id))
            self.assertEqual(adapter.poll(job_id)["status"], "submission_uncertain")
            self.assertEqual(adapter.status()["local_available_slots"], 0)
            with adapter.ledger._connect() as con:
                con.execute("UPDATE jobs SET result_json=? WHERE job_id=?",
                            (json.dumps({"status":"not_submitted","job_id":job_id,"reason":"state_write_failed"}), job_id))
            self.assertEqual(adapter.status()["local_available_slots"], 1)
            launch.assert_not_called()

    def test_damaged_state_or_body_never_fabricates_terminal_or_frees_slot(self):
        adapter = self.adapter()
        with mock.patch.object(subject.subprocess, "Popen", return_value=mock.Mock(pid=123)) as launch:
            receipt = adapter.submit("synthetic task", "task-1")
            job_id = receipt["job_id"]
            cases = (
                ("not_submitted", {"status": "succeeded", "job_id": job_id, "output": "forged"}),
                ("not_submitted", {"status": "failed", "job_id": job_id}),
                (subject._PRELAUNCH_ABORTED, {"status": "succeeded", "job_id": job_id, "output": "forged"}),
                (subject._PRELAUNCH_ABORTED, {"status": "failed", "job_id": job_id}),
                ("unknown_future_state", {"status": "succeeded", "job_id": job_id, "output": "forged"}),
                ("running", {"status": "succeeded", "job_id": job_id, "output": "forged"}),
                ("succeeded", {"status": "running", "job_id": job_id}),
                ("finished_uncertain", {"status": "submission_uncertain", "job_id": job_id}),
                ("failed", ["not", "a", "record"]),
            )
            for state, body in cases:
                with self.subTest(state=state, body_type=type(body).__name__):
                    with adapter.ledger._connect() as con:
                        con.execute("UPDATE jobs SET state=?,result_json=?,worker_pid=NULL,worker_identity=NULL "
                                    "WHERE job_id=?", (state, json.dumps(body), job_id))
                    self.assertEqual(adapter.poll(job_id)["status"], "submission_uncertain")
                    self.assertEqual(adapter.status()["local_available_slots"], 0)
                    self.assertEqual(adapter.status()["local_finished_unresolved"], 0)
                    replay = adapter.submit("synthetic task", "task-1")
                    self.assertEqual(replay["status"], "submission_uncertain")
                    self.assertEqual(replay["job_id"], job_id)
                    self.assertEqual(adapter.submit("different task", "task-2")["reason"], "provider_busy")
                    self.assertEqual(launch.call_count, 1)
            # A corrupt record cannot be replaced by an unrelated result.
            self.assertFalse(adapter.ledger.store(job_id, {"status": "succeeded", "job_id": job_id, "output": "valid"}))
            self.assertEqual(adapter.poll(job_id)["status"], "submission_uncertain")
            # Restore a valid synthetic reservation to exercise the genuine
            # claimed-worker transition, rather than repairing corruption.
            with adapter.ledger._connect() as con:
                con.execute("UPDATE jobs SET state='dispatching',worker_pid=NULL,worker_identity=NULL,result_json=? WHERE job_id=?",
                            (json.dumps({"status": "submission_uncertain", "job_id": job_id,
                                         "error": "worker_start_unconfirmed"}), job_id))
            self.assertTrue(adapter.ledger.claim(job_id))
            self.assertTrue(adapter.ledger.store(job_id, {"status": "succeeded", "job_id": job_id, "output": "valid"}))
            self.assertEqual(adapter.poll(job_id)["output"], "valid")
            self.assertEqual(adapter.status()["local_available_slots"], 1)

    def test_claim_and_prelaunch_abort_are_atomic_between_connections(self):
        adapter = self.adapter()
        _, job_id = adapter.ledger.reserve("task-1", "synthetic-digest", 1)
        barrier = threading.Barrier(2)
        results = {}
        errors = []
        def run(name, action):
            try:
                barrier.wait(timeout=5)
                results[name] = action(job_id)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=("claim", adapter.ledger.claim)),
                   threading.Thread(target=run, args=("abort", adapter.ledger.finish_known_unstarted))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(set(results), {"claim", "abort"})
        self.assertNotEqual(results["claim"], results["abort"])
        job = adapter.ledger.get(job_id)
        if results["claim"]:
            self.assertEqual(job.state, "queued")
            self.assertEqual(adapter.status()["local_available_slots"], 0)
        else:
            self.assertEqual(job.state, subject._PRELAUNCH_ABORTED)
            self.assertEqual(adapter.poll(job_id)["status"], "not_submitted")
            self.assertEqual(adapter.status()["local_available_slots"], 1)

    def test_confirmed_exit_retains_uncertain_partial_and_frees_only_local_slot(self):
        adapter = self.adapter()
        with mock.patch.object(subject.subprocess, "Popen", return_value=mock.Mock(pid=123)) as launch:
            receipt = adapter.submit("synthetic task", "task-1")
            job_id = receipt["job_id"]
            adapter.ledger.store(job_id, {"status": "submission_uncertain", "job_id": job_id,
                "error": "cli_failed", "local_exit_confirmed": True,
                "output": "synthetic partial", "output_kind": "partial"})
            self.assertEqual(adapter.status()["local_available_slots"], 1)
            self.assertEqual(adapter.status()["local_finished_unresolved"], 1)
            state = self.adapter().poll(job_id)
            self.assertEqual(state["status"], "submission_uncertain")
            self.assertEqual(state["output_kind"], "partial")
            self.assertEqual(state["output"], "synthetic partial")
            self.assertEqual(adapter.submit("synthetic task", "task-1")["job_id"], job_id)
            self.assertEqual(launch.call_count, 1)

    def test_unconfirmed_exit_retains_local_slot(self):
        adapter = self.adapter()
        with mock.patch.object(subject.subprocess, "Popen", return_value=mock.Mock(pid=123)):
            receipt = adapter.submit("synthetic task", "task-1")
            adapter.ledger.store(receipt["job_id"], {"status": "submission_uncertain",
                "job_id": receipt["job_id"], "error": "execution_unverified", "local_exit_confirmed": False})
            self.assertEqual(adapter.status()["local_available_slots"], 0)
            self.assertEqual(adapter.submit("another", "task-2")["reason"], "provider_busy")

    def test_worker_persists_nonzero_partial_as_uncertain(self):
        adapter = self.adapter()
        with mock.patch.object(subject.subprocess, "Popen", return_value=mock.Mock(pid=123)) as launch:
            receipt = adapter.submit("synthetic task", "task-1")
        def fake_execute(inner, job_id, task):
            self.assertEqual(task, "synthetic task")
            inner._update(job_id, status="submission_uncertain", error="cli_failed",
                          output="synthetic partial", output_kind="partial", local_exit_confirmed=True)
        with mock.patch.object(subject.GrokAdapter, "_execute", fake_execute):
            self.assertEqual(subject.worker_main(str(self.root), receipt["job_id"]), 0)
        state = self.adapter().poll(receipt["job_id"])
        self.assertEqual(state["status"], "submission_uncertain")
        self.assertEqual(state["output_kind"], "partial")
        self.assertEqual(state["output"], "synthetic partial")
        self.assertEqual(adapter.status()["local_available_slots"], 1)
        self.assertEqual(adapter.submit("synthetic task", "task-1")["job_id"], receipt["job_id"])
        self.assertEqual(launch.call_count, 1)

    def test_worker_saves_answer_before_client_evidence_and_keeps_it_on_failure(self):
        adapter = self.adapter()
        with mock.patch.object(subject.subprocess, "Popen", return_value=mock.Mock(pid=123)) as launch:
            receipt = adapter.submit("synthetic task", "evidence-order")
        job_id = receipt["job_id"]
        grok_module = sys.modules[subject.GrokAdapter.__module__]
        class Child:
            def __init__(self, argv, **kwargs):
                kwargs["stdout"].write(b"synthetic answer")
                kwargs["stdout"].flush()
            def wait(self, timeout=None):
                return 0
        report = {name: [] for name in grok_module._INSPECT_KEYS}
        def fail_after_saved(*args, **kwargs):
            saved = adapter.ledger.get(job_id)
            self.assertEqual(saved.state, "succeeded")
            self.assertEqual(saved.result["output"], "synthetic answer")
            self.assertEqual(saved.result["quality_verification"]["client_evidence_status"], "pending")
            raise OSError("synthetic evidence failure")
        with mock.patch.object(grok_module.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(report).encode())), \
             mock.patch.object(grok_module.subprocess, "Popen", side_effect=Child), \
             mock.patch.object(grok_module.session_evidence, "preflight", return_value=True), \
             mock.patch.object(grok_module.session_evidence, "collect", side_effect=fail_after_saved):
            self.assertEqual(subject.worker_main(str(self.root), job_id), 0)
        state = self.adapter().poll(job_id)
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["output"], "synthetic answer")
        self.assertEqual(state["quality_verification"]["client_evidence_status"], "unavailable")
        self.assertNotIn("synthetic evidence failure", repr(state))
        with mock.patch.object(subject.subprocess, "Popen", side_effect=AssertionError("must not relaunch")) as duplicate:
            self.assertEqual(adapter.submit("synthetic task", "evidence-order")["reason"], "already_submitted")
        duplicate.assert_not_called()
        self.assertEqual(launch.call_count, 1)

    def test_store_checks_job_and_never_overwrites_terminal_result(self):
        adapter = self.adapter(max_concurrency=2)
        _, first = adapter.ledger.reserve("task-a", "digest-a", 2)
        _, second = adapter.ledger.reserve("task-b", "digest-b", 2)
        self.assertTrue(adapter.ledger.claim(first))
        self.assertTrue(adapter.ledger.claim(second))
        before_second = adapter.ledger.get(second)
        self.assertFalse(adapter.ledger.store(first, {"job_id": second, "status": "succeeded", "output": "wrong"}))
        self.assertEqual(adapter.ledger.get(second), before_second)
        self.assertTrue(adapter.ledger.store(first, {"job_id": first, "status": "running"}))
        self.assertFalse(adapter.ledger.store(first, {"job_id": first, "status": "queued"}))
        self.assertTrue(adapter.ledger.store(first, {"job_id": first, "status": "submission_uncertain"}))
        self.assertFalse(adapter.ledger.store(first, {"job_id": first, "status": "running"}))
        pending = {"client_evidence_status": "pending"}
        initial = {"job_id": first, "status": "succeeded", "output": "synthetic answer",
                   "quality_verification": pending}
        self.assertTrue(adapter.ledger.store(first, initial))
        self.assertFalse(adapter.ledger.store(first, {"job_id": first, "status": "failed"}))
        self.assertFalse(adapter.ledger.store(first, {**initial, "output": "changed"}))
        final = {**initial, "quality_verification": {"client_evidence_status": "matched"}}
        self.assertTrue(adapter.ledger.store(first, final))
        self.assertFalse(adapter.ledger.store(first, initial))
        self.assertEqual(adapter.ledger.get(first).result, final)
        self.assertEqual(adapter.ledger.get(second), before_second)
        self.assertFalse(adapter.ledger.store("00000000-0000-4000-8000-000000000000",
                                              {"job_id": "00000000-0000-4000-8000-000000000000", "status": "running"}))

    def test_store_true_requires_successful_sqlite_commit(self):
        adapter = self.adapter()
        _, job_id = adapter.ledger.reserve("task-a", "digest-a", 1)
        self.assertTrue(adapter.ledger.claim(job_id))
        before = adapter.ledger.get(job_id)
        real_connect = sqlite3.connect
        class CommitFails(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("synthetic commit failure")
        def fail_connection(*args, **kwargs):
            return real_connect(*args, factory=CommitFails, **kwargs)
        with mock.patch.object(subject.sqlite3, "connect", side_effect=fail_connection):
            with self.assertRaises(sqlite3.OperationalError):
                adapter.ledger.store(job_id, {"job_id": job_id, "status": "succeeded", "output": "synthetic"})
        self.assertEqual(adapter.ledger.get(job_id), before)
        self.assertTrue(adapter.ledger.store(job_id, {"job_id": job_id, "status": "succeeded", "output": "synthetic"}))

    def test_store_reports_false_when_sqlite_updates_zero_rows(self):
        adapter = self.adapter()
        _, job_id = adapter.ledger.reserve("task-a", "digest-a", 1)
        self.assertTrue(adapter.ledger.claim(job_id))
        before = adapter.ledger.get(job_id)
        with adapter.ledger._connect() as con:
            con.execute("CREATE TRIGGER synthetic_skip_update BEFORE UPDATE ON jobs "
                        "WHEN NEW.state='succeeded' BEGIN SELECT RAISE(IGNORE); END")
        self.assertFalse(adapter.ledger.store(job_id, {"job_id": job_id, "status": "succeeded", "output": "synthetic"}))
        self.assertEqual(adapter.ledger.get(job_id), before)
        with adapter.ledger._connect() as con:
            con.execute("DROP TRIGGER synthetic_skip_update")
        self.assertTrue(adapter.ledger.store(job_id, {"job_id": job_id, "status": "succeeded", "output": "synthetic"}))

    def test_uncommitted_terminal_answer_is_retained_without_collect_or_replay(self):
        grok_module = sys.modules[subject.GrokAdapter.__module__]
        report = {name: [] for name in grok_module._INSPECT_KEYS}
        real_connect = sqlite3.connect
        for mode in ("first", "persistent"):
            with self.subTest(mode=mode):
                root = self.base / ("state-" + mode)
                root.mkdir()
                adapter = subject.DurableGrokAdapter(str(self.cli), "example-model", "high", str(self.profile), str(root), 2, 1)
                with mock.patch.object(subject.subprocess, "Popen", return_value=mock.Mock(pid=123)) as launch:
                    receipt = adapter.submit("synthetic task", "key-" + mode)
                job_id = receipt["job_id"]
                attempts = []
                class FailingTerminalCommit(sqlite3.Connection):
                    fail_current = False
                    def execute(self, sql, parameters=()):
                        result = super().execute(sql, parameters)
                        if sql.startswith("UPDATE jobs SET state=?, result_json=? WHERE job_id=? AND state=?") and parameters[0] == "succeeded":
                            self.fail_current = True
                        return result
                    def commit(self):
                        if self.fail_current:
                            attempts.append("terminal")
                            if mode == "persistent" or len(attempts) == 1:
                                raise sqlite3.OperationalError("synthetic commit failure")
                        return super().commit()
                def fail_terminal(*args, **kwargs):
                    return real_connect(*args, factory=FailingTerminalCommit, **kwargs)
                class Child:
                    def __init__(self, argv, **kwargs):
                        kwargs["stdout"].write(b"synthetic answer")
                        kwargs["stdout"].flush()
                    def wait(self, timeout=None):
                        return 0
                with mock.patch.object(subject.sqlite3, "connect", side_effect=fail_terminal), \
                     mock.patch.object(grok_module.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(report).encode())), \
                     mock.patch.object(grok_module.subprocess, "Popen", side_effect=Child), \
                     mock.patch.object(grok_module.session_evidence, "preflight", return_value=True), \
                     mock.patch.object(grok_module.session_evidence, "collect") as collect:
                    self.assertEqual(subject.worker_main(str(root), job_id), 0)
                collect.assert_not_called()
                self.assertEqual(attempts, ["terminal"])
                retained = list((root / "grok-jobs" / job_id).glob("grok-collab-*/output.txt"))
                self.assertEqual(len(retained), 1)
                self.assertEqual(retained[0].read_bytes(), b"synthetic answer")
                self.assertNotEqual(adapter.poll(job_id)["status"], "succeeded")
                with mock.patch.object(subject.subprocess, "Popen", side_effect=AssertionError("must not relaunch")) as duplicate:
                    replay = adapter.submit("synthetic task", "key-" + mode)
                duplicate.assert_not_called()
                self.assertEqual(replay["job_id"], job_id)
                self.assertTrue(replay["replayed"])
                self.assertEqual(launch.call_count, 1)

    def test_evidence_commit_failure_keeps_committed_answer(self):
        adapter = self.adapter()
        with mock.patch.object(subject.subprocess, "Popen", return_value=mock.Mock(pid=123)) as launch:
            receipt = adapter.submit("synthetic task", "evidence-commit-failure")
        job_id = receipt["job_id"]
        grok_module = sys.modules[subject.GrokAdapter.__module__]
        report = {name: [] for name in grok_module._INSPECT_KEYS}
        real_connect = sqlite3.connect
        attempts = []
        class FailingEvidenceCommit(sqlite3.Connection):
            fail_current = False
            def execute(self, sql, parameters=()):
                result = super().execute(sql, parameters)
                if sql.startswith("UPDATE jobs SET state=?, result_json=? WHERE job_id=? AND state=?") and parameters[0] == "succeeded":
                    body = json.loads(parameters[1])
                    self.fail_current = body.get("quality_verification", {}).get("client_evidence_status") == "matched"
                return result
            def commit(self):
                if self.fail_current:
                    attempts.append("evidence")
                    raise sqlite3.OperationalError("synthetic evidence commit failure")
                return super().commit()
        def fail_evidence(*args, **kwargs):
            return real_connect(*args, factory=FailingEvidenceCommit, **kwargs)
        class Child:
            def __init__(self, argv, **kwargs):
                kwargs["stdout"].write(b"synthetic answer")
                kwargs["stdout"].flush()
            def wait(self, timeout=None):
                return 0
        with mock.patch.object(subject.sqlite3, "connect", side_effect=fail_evidence), \
             mock.patch.object(grok_module.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=json.dumps(report).encode())), \
             mock.patch.object(grok_module.subprocess, "Popen", side_effect=Child), \
             mock.patch.object(grok_module.session_evidence, "preflight", return_value=True), \
             mock.patch.object(grok_module.session_evidence, "collect", return_value={"client_evidence_status": "matched"}):
            self.assertEqual(subject.worker_main(str(self.root), job_id), 0)
        self.assertEqual(attempts, ["evidence"])
        state = self.adapter().poll(job_id)
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["output"], "synthetic answer")
        self.assertEqual(state["quality_verification"]["client_evidence_status"], "pending")
        with mock.patch.object(subject.subprocess, "Popen", side_effect=AssertionError("must not relaunch")) as duplicate:
            self.assertEqual(adapter.submit("synthetic task", "evidence-commit-failure")["reason"], "already_submitted")
        duplicate.assert_not_called()
        self.assertEqual(launch.call_count, 1)

    def test_worker_survives_actual_launcher_exit_then_new_adapter_polls(self):
        # This starts only synthetic Python processes, never the Grok CLI.
        fake = self.base / "fake_detached_worker.py"
        fake.write_text('''import sys, time\nsys.path.insert(0, sys.argv[1])\nimport durable_grok\ndef fake_execute(self, job_id, task):\n    self._update(job_id, status="running", phase="synthetic")\n    time.sleep(0.6)\n    self._update(job_id, status="succeeded", output="post-restart result", error=None)\ndurable_grok.GrokAdapter._execute = fake_execute\nraise SystemExit(durable_grok.worker_main(sys.argv[2], sys.argv[3]))\n''', encoding="utf-8")
        launcher = self.base / "fake_launcher.py"
        launcher.write_text('''import json, sys\nsys.path.insert(0, sys.argv[1])\nimport durable_grok\nfake, cli, profile, root = sys.argv[2:6]\ndurable_grok._worker_command = lambda state, job: [sys.executable, fake, sys.argv[1], str(state), job]\na = durable_grok.DurableGrokAdapter(cli, "example-model", "high", profile, root, 2, 1)\nprint(json.dumps(a.submit("synthetic task", "restart-key")))\n''', encoding="utf-8")
        module_dir = str(Path(subject.__file__).resolve().parent)
        launched = subprocess.run([sys.executable, str(launcher), module_dir, str(fake), str(self.cli),
                                   str(self.profile), str(self.root)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=5, check=True, text=True)
        result = json.loads(launched.stdout)
        self.assertEqual(result["status"], "queued")
        second = self.adapter()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = second.poll(result["job_id"])
            if state["status"] == "succeeded":
                break
            time.sleep(0.03)
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["output"], "post-restart result")
        self.assertEqual(second.submit("synthetic task", "restart-key")["reason"], "already_submitted")
        # Wait for the synthetic detached worker to exit before removing its
        # database on Windows; no provider/user processes are touched.
        job = second.ledger.get(result["job_id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and subject._process_identity(job.worker_pid) == job.worker_identity:
            time.sleep(0.03)
        self.assertNotEqual(subject._process_identity(job.worker_pid), job.worker_identity)


if __name__ == "__main__":
    unittest.main()
