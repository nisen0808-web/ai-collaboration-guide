"""Offline durable-job checks; fake local Python child, no Claude or network."""
import json
import hashlib
from contextlib import closing
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
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
        self.args = (str(self.cli), str(self.profile), str(self.work), str(self.state), "model-example", "medium")

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
        fingerprint = hashlib.sha256(json.dumps(["synthetic task", "model-example", "medium"],
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

    def test_real_worker_writes_final_result_after_synthetic_slow_poll(self):
        a = self.adapter()
        self.insert("worker-job", "reserved")
        message = {"task": "synthetic prompt", "cli_path": str(self.cli),
                   "profile_dir": str(self.profile), "work_dir": str(self.work),
                   "model": "model-example", "effort": "medium", "timeout_seconds": 900}
        class FakeAdapter:
            def __init__(self, *_args, **_kwargs):
                self.events = iter([{"status": "running"},
                                    {"status": "submission_uncertain", "error": "provider_slow"},
                                    {"status": "succeeded", "output": "synthetic response"}])
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
                        "deadline=time.monotonic()+5\n"
                        "while not os.path.exists(os.path.join(state,'release')) and time.monotonic()<deadline: time.sleep(.01)\n"
                        "name=job+'.result.json'\n"
                        "with open(os.path.join(state,name),'w',encoding='utf-8') as f: json.dump({'output':'synthetic result'},f)\n"
                        "with sqlite3.connect(os.path.join(state,'jobs.sqlite3')) as c: c.execute(\"UPDATE jobs SET status='succeeded',output_file=? WHERE id=?\",(name,job))\n", encoding="utf-8")
        # Launch an intermediate parent, which starts the fake worker and exits.
        module_dir = str(Path(durable.__file__).resolve().parent)
        code = ("import sys,json;sys.path.insert(0,sys.argv[1]);import durable_claude as d;"
                "d._WORKER_SCRIPT=sys.argv[2];a=d.DurableClaudeAdapter(*json.loads(sys.argv[3]),enabled=True);"
                "print(json.dumps(a.submit('synthetic prompt','restart-job')))" )
        result = subprocess.run([sys.executable, "-c", code, module_dir, str(fake), json.dumps(self.args)],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "queued")
        a = self.adapter()
        self.assertNotEqual(a.poll("restart-job")["status"], "succeeded")
        replay = a.submit("synthetic prompt", "restart-job")
        self.assertTrue(replay["replayed"])
        (self.state / "release").write_text("go", encoding="ascii")
        for _ in range(150):
            output = a.poll("restart-job")
            if output["status"] == "succeeded":
                break
            time.sleep(.02)
        self.assertEqual(output["status"], "succeeded")
        self.assertEqual(output["output"], "synthetic result")
        self.assertNotIn(b"synthetic prompt", (self.state / "jobs.sqlite3").read_bytes())


if __name__ == "__main__":
    unittest.main()
