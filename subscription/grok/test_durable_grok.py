"""Synthetic jobs only; one test starts a local fake worker, never Grok."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
