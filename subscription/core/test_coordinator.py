import sqlite3
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from coordinator import Coordinator, InputError, POLL_ERRORS, SUBMIT_REASONS


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.db"

    def test_unconfigured_provider_does_not_block_other(self):
        calls = []
        c = Coordinator(self.db, {"grok": lambda **kw: calls.append(kw) or {"status": "queued", "job_id": "job-1"}})
        self.assertEqual(c.submit("claude", "task", "key-c")["status"], "not_submitted")
        self.assertEqual(c.submit("grok", "task", "key-g")["status"], "queued")
        self.assertEqual(len(calls), 1)

    def test_replay_never_dispatches_again(self):
        calls = []
        c = Coordinator(self.db, {"grok": lambda **kw: calls.append(kw) or {"status": "queued", "job_id": "job-1"}})
        first = c.submit("grok", "task", "key")
        replay = c.submit("grok", "task", "key")
        self.assertEqual(first["task_id"], replay["task_id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(calls), 1)
        with self.assertRaises(InputError):
            c.submit("grok", "another task", "key")

    def test_concurrent_replay_never_calls_adapter_twice(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        def slow_adapter(**kw):
            calls.append(kw)
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test synchronization")
            return {"status": "queued", "job_id": "job-1"}
        c = Coordinator(self.db, {"grok": slow_adapter})
        first = []
        worker = threading.Thread(target=lambda: first.append(c.submit("grok", "task", "key")))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            replay = c.submit("grok", "task", "key")
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["status"], "submission_uncertain")
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertEqual(first[0]["status"], "queued")

    def test_other_provider_can_submit_while_first_adapter_is_waiting(self):
        entered = threading.Event()
        release = threading.Event()
        first = []
        def waiting_adapter(**kw):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test synchronization")
            return {"status": "queued", "job_id": "claude-job"}
        c = Coordinator(self.db, {"claude": waiting_adapter,
                                   "grok": lambda **kw: {"status": "queued", "job_id": "grok-job"}})
        worker = threading.Thread(target=lambda: first.append(c.submit("claude", "task", "key-claude")))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            second = c.submit("grok", "task", "key-grok")
            self.assertEqual(second["job_id"], "grok-job")
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(first[0]["status"], "queued")

    def test_exception_is_uncertain_and_secret_not_stored(self):
        def broken(**kw):
            raise RuntimeError("synthetic-sensitive-message")
        c = Coordinator(self.db, {"grok": broken})
        self.assertEqual(c.submit("grok", "private-task-contents", "key")["status"], "submission_uncertain")
        self.assertTrue(c.submit("grok", "private-task-contents", "key")["replayed"])
        self.assertNotIn(b"synthetic-sensitive-message", self.db.read_bytes())
        self.assertNotIn(b"private-task-contents", self.db.read_bytes())

    def test_crashed_dispatch_is_uncertain_without_resubmit(self):
        c = Coordinator(self.db)
        c.submit("grok", "task", "key")
        with closing(sqlite3.connect(self.db)) as con:
            with con:
                con.execute("UPDATE requests SET status='dispatching' WHERE request_key='key'")
        self.assertEqual(c.get("key")["status"], "submission_uncertain")
        calls = []
        c.adapters["grok"] = lambda **kw: calls.append(kw)
        self.assertEqual(c.submit("grok", "task", "key")["status"], "submission_uncertain")
        self.assertEqual(calls, [])

    def test_process_interruption_keeps_reservation_uncertain(self):
        script = '''
import sys, threading
from coordinator import Coordinator
def blocked(**kwargs):
    print("entered", flush=True)
    threading.Event().wait(30)
    return {"status": "queued", "job_id": "unreachable"}
Coordinator(sys.argv[1], {"grok": blocked}).submit("grok", "task", "key")
'''
        child = subprocess.Popen([sys.executable, "-c", script, str(self.db)], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        messages = queue.Queue()
        reader = threading.Thread(target=lambda: messages.put(child.stdout.readline()), daemon=True)
        reader.start()
        try:
            self.assertEqual(messages.get(timeout=5).strip(), "entered")
        finally:
            child.terminate()
            child.communicate(timeout=5)
            reader.join(1)
        c = Coordinator(self.db)
        self.assertEqual(c.get("key")["status"], "submission_uncertain")
        self.assertTrue(c.submit("grok", "task", "key")["replayed"])

    def test_bad_adapter_output_is_uncertain(self):
        c = Coordinator(self.db, {"claude": lambda **kw: {"status": "queued"}})
        self.assertEqual(c.submit("claude", "task", "key")["status"], "submission_uncertain")

    def test_status_never_claims_ready_from_adapter_presence(self):
        c = Coordinator(self.db, {"claude": lambda **kw: {}})
        self.assertFalse(c.status()["providers"]["claude"]["ready"])
        self.assertEqual(c.status()["providers"]["claude"]["verification"], "not_verified")

    def test_status_one_provider_hang_does_not_block_other(self):
        entered, release = threading.Event(), threading.Event()
        class Claude:
            def status(self):
                entered.set()
                release.wait(3)
                return {"ready": True, "loggedIn": True}
        class Grok:
            def status(self):
                return {"ready": True, "loggedIn": True, "authMethod": "cached_token", "secret": "dont-copy",
                        "local_active_jobs": 1, "local_max_concurrency": 2, "local_available_slots": 1,
                        "official_quota_verified": False}
        c = Coordinator(self.db, {"claude": Claude(), "grok": Grok()}, status_timeout=0.1)
        try:
            result = c.status()["providers"]
            self.assertTrue(entered.is_set())
            self.assertIsNone(result["claude"]["ready"])
            self.assertTrue(result["grok"]["loggedIn"])
            self.assertEqual(result["grok"]["authMethod"], "cached_token")
            self.assertTrue(result["grok"]["reported_by_adapter"])
            self.assertEqual(result["grok"]["local_available_slots"], 1)
            self.assertFalse(result["grok"]["official_quota_verified"])
            self.assertEqual(result["grok"]["verification"], "not_verified")
            self.assertNotIn("secret", str(result))
        finally:
            release.set()

    def test_existing_job_result_is_read_without_repeated_submission(self):
        class Adapter:
            def __init__(self): self.submits = 0
            def submit(self, *, task, task_id):
                self.submits += 1
                return {"status": "queued", "job_id": "worker-1"}
            def poll(self, job_id):
                self.assert_job = job_id
                return {"status": "succeeded", "output": "private response 🌐",
                        "quality_verification": {"verified": False}}
        adapter = Adapter()
        c = Coordinator(self.db, {"grok": adapter})
        c.submit("grok", "private prompt", "key")
        result = c.result("grok", "key")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["output"], "private response 🌐")
        self.assertFalse(result["quality_verification"]["verified"])
        self.assertEqual(c.result("grok", "key")["output"], "private response 🌐")
        self.assertEqual(adapter.submits, 1)
        self.assertEqual(adapter.assert_job, "worker-1")
        self.assertNotIn("private response".encode(), self.db.read_bytes())
        self.assertNotIn(b"private prompt", self.db.read_bytes())

    def test_poll_failure_and_missing_result_never_become_success(self):
        class Adapter:
            def submit(self, **kw): return {"status": "queued", "job_id": "worker-1"}
            def poll(self, job_id): raise RuntimeError("synthetic-sensitive-message")
        c = Coordinator(self.db, {"claude": Adapter()})
        c.submit("claude", "task", "key")
        result = c.result("claude", "key")
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertEqual(result["poll_error"], "adapter_error")
        self.assertNotIn("synthetic-sensitive-message", str(result))
        c.adapters["claude"].poll = lambda job_id: {"status": "succeeded"}
        self.assertEqual(c.result("claude", "key")["poll_error"], "incomplete_result")
        c.adapters["claude"].poll = lambda job_id: {"status": "not_submitted"}
        self.assertEqual(c.result("claude", "key")["status"], "submission_uncertain")
        with self.assertRaises(InputError): c.result("grok", "key")

    def test_uncertain_with_job_id_can_be_queried_without_resubmission(self):
        class Adapter:
            def __init__(self): self.submits = 0
            def submit(self, **kwargs):
                self.submits += 1
                return {"status": "submission_uncertain", "job_id": "job-unknown"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "recovered"}
        adapter = Adapter()
        c = Coordinator(self.db, {"grok": adapter})
        self.assertEqual(c.submit("grok", "task", "key")["job_id"], "job-unknown")
        self.assertEqual(c.result("grok", "key")["output"], "recovered")
        self.assertEqual(adapter.submits, 1)

    def test_existing_job_reported_as_not_submitted_is_reconciled_conservatively(self):
        class Adapter:
            def __init__(self): self.submits = 0
            def submit(self, **kwargs):
                self.submits += 1
                return {"status": "not_submitted", "reason": "already_submitted", "job_id": "existing-job"}
            def poll(self, job_id): return {"status": "succeeded", "output": "recovered"}
        adapter = Adapter()
        c = Coordinator(self.db, {"grok": adapter})
        response = c.submit("grok", "task", "key")
        self.assertEqual(response["status"], "submission_uncertain")
        self.assertEqual(response["reason"], "already_submitted")
        self.assertEqual(response["job_id"], "existing-job")
        self.assertEqual(c.result("grok", "key")["output"], "recovered")
        self.assertEqual(adapter.submits, 1)

    def test_known_submit_reasons_preserved_unknown_reason_redacted(self):
        class Adapter:
            def __init__(self): self.reason = None
            def submit(self, **kw): return {"status": "not_submitted", "reason": self.reason}
        adapter = Adapter()
        c = Coordinator(self.db, {"grok": adapter})
        for reason in SUBMIT_REASONS:
            with self.subTest(reason=reason):
                adapter.reason = reason
                response = c.submit("grok", "task", "key-" + reason)
                self.assertEqual(response["reason"], reason)
                if reason == "already_submitted":
                    self.assertEqual(response["status"], "submission_uncertain")
        adapter.reason = "secret-value: do not return"
        response = c.submit("grok", "task", "unsafe-reason")
        self.assertIsNone(response["reason"])
        self.assertNotIn(adapter.reason, str(response))

    def test_known_poll_errors_preserved_and_slow_job_reconciles(self):
        class Adapter:
            def __init__(self): self.error = "provider_slow"; self.submits = 0
            def submit(self, **kw):
                self.submits += 1
                return {"status": "queued", "job_id": "job-1"}
            def poll(self, job_id):
                if self.error is None: return {"status": "succeeded", "output": "complete"}
                return {"status": "submission_uncertain", "error": self.error}
        adapter = Adapter()
        c = Coordinator(self.db, {"claude": adapter})
        c.submit("claude", "task", "key")
        for error in POLL_ERRORS:
            with self.subTest(error=error):
                adapter.error = error
                response = c.result("claude", "key")
                self.assertEqual(response["status"], "submission_uncertain")
                self.assertEqual(response["job_id"], "job-1")
                self.assertEqual(response["error"], error)
        adapter.error = "secret-value: do not return"
        self.assertNotIn(adapter.error, str(c.result("claude", "key")))
        adapter.error = None
        self.assertEqual(c.result("claude", "key")["output"], "complete")
        self.assertEqual(adapter.submits, 1)

    def test_truncated_success_is_incomplete_with_original_execution_status(self):
        class Adapter:
            def __init__(self): self.submits = 0
            def submit(self, **kw):
                self.submits += 1
                return {"status": "queued", "job_id": "job-1"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "partial body", "truncated": True}
        adapter = Adapter()
        c = Coordinator(self.db, {"grok": adapter})
        c.submit("grok", "task", "key")
        response = c.result("grok", "key")
        self.assertEqual(response["status"], "incomplete")
        self.assertEqual(response["execution_status"], "succeeded")
        self.assertEqual(response["output_kind"], "partial")
        self.assertTrue(response["truncated"])
        self.assertEqual(response["output"], "partial body")
        self.assertTrue(c.get("key")["truncated"])
        restarted = Coordinator(self.db)
        missing = restarted.result("grok", "key")
        self.assertEqual(missing["status"], "result_unavailable")
        self.assertEqual(missing["execution_status"], "succeeded")
        self.assertTrue(missing["truncated"])
        self.assertNotIn("output", missing)
        self.assertEqual(adapter.submits, 1)

    def test_invalid_truncation_flag_does_not_claim_final_output(self):
        class Adapter:
            def submit(self, **kw): return {"status": "queued", "job_id": "job-1"}
            def poll(self, job_id): return {"status": "succeeded", "output": "body", "truncated": "false"}
        c = Coordinator(self.db, {"claude": Adapter()})
        c.submit("claude", "task", "key")
        response = c.result("claude", "key")
        self.assertEqual(response["status"], "submission_uncertain")
        self.assertNotIn("output", response)

    def test_late_poll_cannot_overwrite_terminal_success(self):
        entered, release = threading.Event(), threading.Event()
        class Adapter:
            def __init__(self): self.calls = 0
            def submit(self, **kw): return {"status": "queued", "job_id": "worker-1"}
            def poll(self, job_id):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    if not release.wait(3): raise TimeoutError()
                    return {"status": "running"}
                return {"status": "succeeded", "output": "complete"}
        c = Coordinator(self.db, {"claude": Adapter()})
        c.submit("claude", "task", "key")
        first = []
        worker = threading.Thread(target=lambda: first.append(c.result("claude", "key")))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(c.result("claude", "key")["status"], "succeeded")
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(first[0]["status"], "result_unavailable")
        self.assertEqual(first[0]["execution_status"], "succeeded")
        self.assertEqual(first[0]["poll_error"], "provider_state_inconsistent")
        self.assertEqual(c.get("key")["status"], "succeeded")

    def test_success_then_lost_adapter_result_is_explicitly_unavailable(self):
        class Adapter:
            def __init__(self): self.submits = 0
            def submit(self, **kw):
                self.submits += 1
                return {"status": "queued", "job_id": "job-1"}
            def poll(self, job_id): return {"status": "succeeded", "output": "complete"}
        adapter = Adapter()
        c = Coordinator(self.db, {"grok": adapter})
        c.submit("grok", "task", "key")
        self.assertEqual(c.result("grok", "key")["output"], "complete")
        adapter.poll = lambda job_id: {"status": "submission_uncertain"}
        response = c.result("grok", "key")
        self.assertEqual(response["status"], "result_unavailable")
        self.assertEqual(response["execution_status"], "succeeded")
        self.assertNotIn("output", response)
        restarted = Coordinator(self.db)
        response = restarted.result("grok", "key")
        self.assertEqual(response["status"], "result_unavailable")
        self.assertEqual(response["execution_status"], "succeeded")
        self.assertEqual(restarted.get("key")["status"], "succeeded")
        self.assertEqual(adapter.submits, 1)


if __name__ == "__main__":
    unittest.main()
