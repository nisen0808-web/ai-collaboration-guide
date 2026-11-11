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
        fake.write_text('''import sys, time\nsys.path.insert(0, sys.argv[1])\nimport durable_grok\ndef fake_execute(self, job_id, task):\n    self._update(job_id, status="running", phase="synthetic")\n    self._update(job_id, status="submission_uncertain", error="process_still_running")\n    time.sleep(0.6)\n    self._update(job_id, status="succeeded", output="synthetic result", error=None)\ndurable_grok.GrokAdapter._execute = fake_execute\nraise SystemExit(durable_grok.worker_main(sys.argv[2], sys.argv[3]))\n''', encoding="utf-8")
        module_dir = str(Path(subject.__file__).resolve().parent)
        def fake_command(root, job_id):
            return [sys.executable, str(fake), module_dir, str(root), job_id]
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
                if state.get("error") == "process_still_running":
                    saw_worker_uncertain = True
                    self.assertEqual(second.status()["local_available_slots"], 0)
                    replay = second.submit("synthetic private task", "task-1")
                    self.assertEqual(replay["job_id"], job_id)
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
