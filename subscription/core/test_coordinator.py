import hashlib
import json
import sqlite3
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_new_tasks_use_same_utf8_byte_limit_for_both_providers(self):
        calls = []
        def adapter(**kwargs):
            calls.append(kwargs)
            return {"status": "queued", "job_id": "synthetic-job"}
        coordinator = Coordinator(self.db, {"claude": adapter, "grok": adapter})
        valid = ("a" * 100_000, "中" * 33_333 + "a", "😀" * 25_000)
        too_large = ("中" * 33_334, "😀" * 25_000 + "a")
        for provider in ("claude", "grok"):
            for index, task in enumerate(valid):
                with self.subTest(provider=provider, accepted=index):
                    self.assertEqual(len(task.encode("utf-8")), 100_000)
                    result = coordinator.submit(provider, task, f"{provider}-valid-{index}")
                    self.assertEqual(result["status"], "queued")
            for index, task in enumerate(too_large):
                key = f"{provider}-large-{index}"
                with self.subTest(provider=provider, rejected=index):
                    count = len(calls)
                    with self.assertRaises(InputError):
                        coordinator.submit(provider, task, key)
                    self.assertEqual(len(calls), count)
                    self.assertEqual(coordinator.get(key)["status"], "not_found")

    def test_invalid_unicode_is_rejected_without_reserving_or_calling_provider(self):
        adapter = Mock()
        coordinator = Coordinator(self.db, {"claude": adapter, "grok": adapter})
        for provider in ("claude", "grok"):
            with self.subTest(provider=provider), self.assertRaises(InputError):
                coordinator.submit(provider, "invalid\ud800", f"bad-{provider}")
            self.assertEqual(coordinator.get(f"bad-{provider}")["status"], "not_found")
        adapter.assert_not_called()

    def test_historical_over_byte_limit_replays_original_receipt_and_can_be_polled(self):
        task = "旧" * 40_000  # Old versions accepted this 120,000-byte Claude input.
        self.assertGreater(len(task.encode("utf-8")), 100_000)
        adapter = Mock()
        adapter.poll.return_value = {"status": "running"}
        coordinator = Coordinator(self.db, {"claude": adapter})
        canonical = json.dumps({"provider": "claude", "task": task}, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with coordinator._db() as con:
            con.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?)",
                        ("old-key", digest, "old-task", "claude", "running", "old-job", None))
        replay = coordinator.submit("claude", task, "old-key")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["job_id"], "old-job")
        self.assertEqual(coordinator.get("old-key")["status"], "running")
        self.assertEqual(coordinator.result("claude", "old-key")["status"], "running")
        adapter.submit.assert_not_called()
        adapter.poll.assert_called_once_with("old-job")
        with self.assertRaises(InputError):
            coordinator.submit("claude", task + "a", "old-key")
        adapter.submit.assert_not_called()

    def test_known_unstarted_write_failure_stays_not_submitted_without_retry(self):
        calls = []
        def unstarted(**kwargs):
            calls.append(kwargs)
            return {"status": "not_submitted", "reason": "state_write_failed"}
        coordinator = Coordinator(self.db, {"grok": unstarted})
        result = coordinator.submit("grok", "synthetic task", "request-key")
        self.assertEqual(result["status"], "not_submitted")
        self.assertEqual(result["reason"], "state_write_failed")
        self.assertIsNone(result["job_id"])
        replay = coordinator.submit("grok", "synthetic task", "request-key")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "not_submitted")
        self.assertEqual(len(calls), 1)

    def test_actual_grok_write_failure_is_reported_not_submitted(self):
        # Exercise the real local adapter and coordinator with a synthetic CLI
        # and a failing state write; no provider process or network is used.
        root = Path(self.tmp.name)
        package_root = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, package_root)
        try:
            from grok import durable_grok
            state = root / "grok-state"
            profile = root / "grok-profile"
            cli = root / "synthetic-grok"
            state.mkdir()
            profile.mkdir()
            cli.touch()
            adapter = durable_grok.DurableGrokAdapter(str(cli), "synthetic-model", "medium",
                                                       str(profile), str(state))
            coordinator = Coordinator(self.db, {"grok": adapter})
            with patch.object(adapter.ledger, "write_request", side_effect=OSError("synthetic")), \
                 patch.object(durable_grok.subprocess, "Popen", side_effect=AssertionError("must not start")) as launch:
                result = coordinator.submit("grok", "synthetic task", "request-key")
                replay = coordinator.submit("grok", "synthetic task", "request-key")
                launch.assert_not_called()
            self.assertEqual(result["status"], "not_submitted")
            self.assertEqual(result["reason"], "state_write_failed")
            self.assertIsNone(result["job_id"])
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["status"], "not_submitted")
        finally:
            sys.path.remove(package_root)

    def test_grok_double_write_failure_keeps_original_handle_without_retry(self):
        root = Path(self.tmp.name)
        package_root = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, package_root)
        try:
            from grok import durable_grok
            state, profile, cli = root / "grok-state", root / "grok-profile", root / "synthetic-grok"
            state.mkdir()
            profile.mkdir()
            cli.touch()
            adapter = durable_grok.DurableGrokAdapter(str(cli), "synthetic-model", "medium",
                                                       str(profile), str(state), max_concurrency=1)
            coordinator = Coordinator(self.db, {"grok": adapter})
            with patch.object(adapter.ledger, "write_request", side_effect=OSError("request")), \
                 patch.object(adapter.ledger, "finish_known_unstarted", side_effect=OSError("ledger")), \
                 patch.object(durable_grok.subprocess, "Popen", side_effect=AssertionError("must not start")) as launch:
                result = coordinator.submit("grok", "synthetic task", "request-key")
                self.assertEqual(result["status"], "submission_uncertain")
                self.assertTrue(result.get("job_id"))
                self.assertEqual(coordinator.submit("grok", "synthetic task", "request-key")["job_id"], result["job_id"])
                self.assertEqual(adapter.status()["local_available_slots"], 0)
                launch.assert_not_called()
            # Only the built-in adapter's versioned, task-bound evidence can
            # reconcile the original receipt after local storage recovers.
            self.assertEqual(adapter.status()["local_available_slots"], 1)
            self.assertEqual(coordinator.result("grok", "request-key")["status"], "not_submitted")
            self.assertEqual(coordinator.get("request-key")["reason"], "state_write_failed")
            self.assertEqual(coordinator.get("request-key")["job_id"], result["job_id"])
        finally:
            sys.path.remove(package_root)

    def test_generic_adapter_cannot_self_attest_prelaunch(self):
        class FakeAdapter:
            def submit(self, **kwargs):
                return {"status": "submission_uncertain", "job_id": "job-fake"}
            def poll(self, job_id):
                return {"status": "not_submitted", "reason": "state_write_failed",
                        "prelaunch_aborted_v1": True}
            def proves_prelaunch_aborted(self, task_id, job_id):
                return True
        coordinator = Coordinator(self.db, {"grok": FakeAdapter()})
        coordinator.submit("grok", "synthetic task", "request-key")
        self.assertEqual(coordinator.result("grok", "request-key")["status"], "submission_uncertain")

    def test_old_and_mismatched_local_evidence_is_never_promoted(self):
        root = Path(self.tmp.name)
        package_root = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, package_root)
        try:
            from grok import durable_grok
            state, profile, cli = root / "grok-state", root / "grok-profile", root / "synthetic-grok"
            state.mkdir()
            profile.mkdir()
            cli.touch()
            adapter = durable_grok.DurableGrokAdapter(str(cli), "synthetic-model", "medium", str(profile), str(state))
            coordinator = Coordinator(self.db, {"grok": adapter})
            with patch.object(adapter.ledger, "write_request", side_effect=OSError("request")), \
                 patch.object(adapter.ledger, "finish_known_unstarted", side_effect=OSError("ledger")):
                result = coordinator.submit("grok", "synthetic task", "request-key")
            job_id, task_id = result["job_id"], result["task_id"]
            # Legacy generic terminal state is not the new, narrowly defined proof.
            old = {"status": "not_submitted", "job_id": job_id, "reason": "state_write_failed"}
            with adapter.ledger._connect() as con:
                con.execute("UPDATE jobs SET state='not_submitted',result_json=? WHERE job_id=?",
                            (__import__("json").dumps(old), job_id))
            self.assertFalse(adapter.proves_prelaunch_aborted(task_id, job_id))
            with patch.object(adapter, "proves_prelaunch_aborted", return_value=True):
                self.assertEqual(coordinator.result("grok", "request-key")["status"], "submission_uncertain")
            # A versioned state still cannot prove a different original task.
            with adapter.ledger._connect() as con:
                con.execute("UPDATE jobs SET state=? WHERE job_id=?", (durable_grok._PRELAUNCH_ABORTED, job_id))
            self.assertFalse(adapter.proves_prelaunch_aborted("wrong-task", job_id))
            self.assertFalse(adapter.proves_prelaunch_aborted(task_id, "00000000-0000-0000-0000-000000000000"))
            # Claimed identity invalidates even the versioned state.
            with adapter.ledger._connect() as con:
                con.execute("UPDATE jobs SET worker_pid=123 WHERE job_id=?", (job_id,))
            self.assertEqual(coordinator.result("grok", "request-key")["status"], "submission_uncertain")
        finally:
            sys.path.remove(package_root)

    def test_proof_cannot_overwrite_a_concurrent_terminal_result(self):
        class Adapter:
            def submit(self, **kwargs):
                return {"status": "submission_uncertain", "job_id": "job-original"}
            def poll(self, job_id):
                return {"status": "not_submitted", "reason": "state_write_failed"}
        coordinator = Coordinator(self.db, {"grok": Adapter()})
        coordinator.submit("grok", "synthetic task", "request-key")
        def concurrent_success(*args):
            with coordinator._db() as conn:
                conn.execute("UPDATE requests SET status='succeeded' WHERE request_key='request-key'")
            return True
        with patch("coordinator._proved_grok_prelaunch_abort", side_effect=concurrent_success):
            coordinator.result("grok", "request-key")
        self.assertEqual(coordinator.get("request-key")["status"], "succeeded")

    def test_corrupt_grok_states_do_not_become_coordinator_success_or_release(self):
        # The adapter and coordinator are real, but the only spawned process
        # is mocked. Each case has a separate synthetic SQLite ledger.
        root = Path(self.tmp.name)
        package_root = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, package_root)
        try:
            from grok import durable_grok
            cases = (("not_submitted", "succeeded"),
                     ("not_submitted", "failed"),
                     (durable_grok._PRELAUNCH_ABORTED, "succeeded"),
                     (durable_grok._PRELAUNCH_ABORTED, "failed"),
                     ("synthetic_unknown_state", "succeeded"),
                     ("finished_uncertain", "succeeded"),
                     ("succeeded", "failed"),
                     ("running", "succeeded"))
            for index, (durable_state, payload_status) in enumerate(cases):
                with self.subTest(state=durable_state, payload=payload_status):
                    case = root / str(index)
                    state, profile, cli = case / "state", case / "profile", case / "synthetic-cli"
                    state.mkdir(parents=True)
                    profile.mkdir()
                    cli.touch()
                    adapter = durable_grok.DurableGrokAdapter(str(cli), "synthetic-model", "medium",
                                                               str(profile), str(state), max_concurrency=1)
                    coordinator = Coordinator(case / "coord.db", {"grok": adapter})
                    with patch.object(durable_grok.subprocess, "Popen", return_value=Mock(pid=123)) as launch:
                        receipt = coordinator.submit("grok", "synthetic task", "original-key")
                        job_id = receipt["job_id"]
                        payload = {"status": payload_status, "job_id": job_id, "output": "synthetic result"}
                        with adapter.ledger._connect() as con:
                            con.execute("UPDATE jobs SET state=?,result_json=? WHERE job_id=?",
                                        (durable_state, json.dumps(payload), job_id))
                            if durable_state == "running":
                                con.execute("UPDATE jobs SET worker_pid=?,worker_identity=? WHERE job_id=?",
                                            (123, "synthetic-identity", job_id))
                        with patch.object(durable_grok, "_process_identity", return_value="synthetic-identity"):
                            result = coordinator.result("grok", "original-key")
                        self.assertEqual(result["status"], "submission_uncertain")
                        self.assertEqual(result["job_id"], job_id)
                        self.assertNotIn("output", result)
                        self.assertEqual(coordinator.get("original-key")["status"], "submission_uncertain")
                        replay = coordinator.submit("grok", "synthetic task", "original-key")
                        self.assertTrue(replay["replayed"])
                        self.assertEqual(replay["job_id"], job_id)
                        self.assertEqual(adapter.status()["local_available_slots"], 0)
                        self.assertEqual(adapter.submit("other task", "other-id")["reason"], "provider_busy")
                        self.assertEqual(launch.call_count, 1)
        finally:
            sys.path.remove(package_root)

    def test_coordinator_preserves_legitimate_grok_success(self):
        root = Path(self.tmp.name)
        package_root = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, package_root)
        try:
            from grok import durable_grok
            state, profile, cli = root / "state", root / "profile", root / "synthetic-cli"
            state.mkdir()
            profile.mkdir()
            cli.touch()
            adapter = durable_grok.DurableGrokAdapter(str(cli), "synthetic-model", "medium",
                                                       str(profile), str(state), max_concurrency=1)
            coordinator = Coordinator(self.db, {"grok": adapter})
            with patch.object(durable_grok.subprocess, "Popen", return_value=Mock(pid=123)) as launch:
                receipt = coordinator.submit("grok", "synthetic task", "original-key")
                job_id = receipt["job_id"]
                self.assertTrue(adapter.ledger.claim(job_id))
                adapter.ledger.store(job_id, {"status": "succeeded", "job_id": job_id, "output": "42"})
                result = coordinator.result("grok", "original-key")
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["output"], "42")
                self.assertEqual(adapter.status()["local_available_slots"], 1)
                self.assertEqual(launch.call_count, 1)
        finally:
            sys.path.remove(package_root)

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

    def test_status_preserves_unconfigured_adapter_without_claiming_login(self):
        class Disabled:
            def status(self):
                return {"configured": False, "ready": False}
        status = Coordinator(self.db, {"claude": Disabled()}).status()["providers"]["claude"]
        self.assertIs(status["configured"], False)
        self.assertIs(status["ready"], False)
        self.assertNotIn("loggedIn", status)
        self.assertEqual(status["verification"], "not_verified")

    def test_trusted_disabled_status_survives_a_blocked_probe(self):
        entered, release = threading.Event(), threading.Event()
        class Disabled:
            def status(self):
                entered.set()
                release.wait(3)
                return {"configured": True, "ready": True, "loggedIn": True}
        class Grok:
            def status(self):
                return {"ready": True}
        c = Coordinator(self.db, {"claude": Disabled(), "grok": Grok()},
                        status_timeout=0.1, unconfigured_providers=("claude",))
        try:
            for _ in range(2):
                providers = c.status()["providers"]
                self.assertTrue(entered.is_set())
                self.assertIs(providers["claude"]["configured"], False)
                self.assertIs(providers["claude"]["ready"], False)
                self.assertNotIn("loggedIn", providers["claude"])
                self.assertEqual(providers["claude"]["verification"], "not_verified")
                self.assertIs(providers["grok"]["ready"], True)
            release.set()
            # An inconsistent late adapter observation cannot override the
            # startup fact or claim authentication on the disabled entry.
            for _ in range(20):
                providers = c.status()["providers"]
                if providers["claude"].get("reported_by_adapter"):
                    break
                time.sleep(0.01)
            self.assertIs(providers["claude"]["configured"], False)
            self.assertIs(providers["claude"]["ready"], False)
            self.assertNotIn("loggedIn", providers["claude"])
        finally:
            release.set()

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
                        "local_observed_inflight": 1, "local_uncertain": 2,
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
            self.assertEqual(result["grok"]["local_observed_inflight"], 1)
            self.assertEqual(result["grok"]["local_uncertain"], 2)
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

    @staticmethod
    def grok_session_quality(**overrides):
        value = {"verified": False, "server_effort_verified": False,
                 "client_evidence_status": "matched", "evidence_source": "official_grok_client_session",
                 "client_model_matches_requested": True, "client_effort_matches_requested": True,
                 "observed_model": "grok-synthetic-1", "observed_effort": "xhigh"}
        value.update(overrides)
        return value

    def grok_result_with_quality(self, quality, key, *, status="succeeded", truncated=False):
        class Adapter:
            def submit(self, **kwargs):
                return {"status": "queued", "job_id": "job"}

            def poll(self, job_id):
                return {"status": status, "output": "synthetic", "truncated": truncated,
                        "quality_verification": quality}
        coordinator = Coordinator(self.db, {"grok": Adapter()})
        coordinator.submit("grok", "synthetic task", key)
        return coordinator.result("grok", key)

    def test_grok_success_exposes_only_client_record_evidence(self):
        evidence = self.grok_session_quality(private="synthetic-secret", actual_model="synthetic-private",
                                             arbitrary={"path": "synthetic-private"})
        result = self.grok_result_with_quality(evidence, "grok-session-match")
        quality = result["quality_verification"]
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(quality["client_evidence_status"], "matched")
        self.assertTrue(quality["client_record_verified"])
        self.assertTrue(quality["client_model_matches_requested"])
        self.assertTrue(quality["client_effort_matches_requested"])
        self.assertEqual(quality["observed_model"], "grok-synthetic-1")
        self.assertEqual(quality["observed_effort"], "xhigh")
        self.assertEqual(quality["evidence_source"], "official_grok_client_session")
        self.assertFalse(quality["verified"])
        self.assertFalse(quality["server_effort_verified"])
        self.assertTrue(quality["reported_by_adapter"])
        self.assertFalse(quality["independently_verified_by_coordinator"])
        self.assertNotIn("synthetic-secret", repr(quality))
        self.assertNotIn("synthetic-private", repr(quality))

    def test_grok_consistent_client_record_mismatch_is_not_a_match(self):
        evidence = self.grok_session_quality(client_evidence_status="mismatch",
                                             client_model_matches_requested=False,
                                             observed_model="grok-other-1")
        quality = self.grok_result_with_quality(evidence, "grok-session-mismatch")["quality_verification"]
        self.assertEqual(quality["client_evidence_status"], "mismatch")
        self.assertFalse(quality["client_record_verified"])
        self.assertIs(quality["client_model_matches_requested"], False)
        self.assertIs(quality["client_effort_matches_requested"], True)
        self.assertEqual(quality["observed_model"], "grok-other-1")
        self.assertFalse(quality["verified"])
        self.assertFalse(quality["server_effort_verified"])

    def test_grok_model_mismatch_can_withhold_untrusted_model_name(self):
        evidence = self.grok_session_quality(client_evidence_status="mismatch",
                                             client_model_matches_requested=False,
                                             observed_model=None)
        quality = self.grok_result_with_quality(evidence, "grok-withheld-model")["quality_verification"]
        self.assertEqual(quality["client_evidence_status"], "mismatch")
        self.assertIs(quality["client_model_matches_requested"], False)
        self.assertIs(quality["client_effort_matches_requested"], True)
        self.assertIsNone(quality["observed_model"])
        self.assertEqual(quality["observed_effort"], "xhigh")
        self.assertFalse(quality["client_record_verified"])
        self.assertFalse(quality["verified"])
        self.assertFalse(quality["server_effort_verified"])

    def test_grok_pending_evidence_stays_unverified_and_answer_is_available(self):
        evidence = self.grok_session_quality(client_evidence_status="pending",
                                             client_model_matches_requested=None,
                                             client_effort_matches_requested=None,
                                             observed_model=None, observed_effort=None,
                                             client_record_verified=True)
        result = self.grok_result_with_quality(evidence, "grok-pending-quality")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["output"], "synthetic")
        quality = result["quality_verification"]
        self.assertEqual(quality["client_evidence_status"], "pending")
        self.assertEqual(quality["evidence_source"], "official_grok_client_session")
        self.assertIsNone(quality["client_model_matches_requested"])
        self.assertIsNone(quality["client_effort_matches_requested"])
        self.assertIsNone(quality["observed_model"])
        self.assertIsNone(quality["observed_effort"])
        self.assertFalse(quality["client_record_verified"])
        self.assertFalse(quality["verified"])
        self.assertFalse(quality["server_effort_verified"])

    def test_grok_rejects_malformed_or_incomplete_pending_evidence(self):
        pending = self.grok_session_quality(client_evidence_status="pending",
                                            client_model_matches_requested=None,
                                            client_effort_matches_requested=None,
                                            observed_model=None, observed_effort=None)
        cases = [dict(pending, client_model_matches_requested=True),
                 dict(pending, client_effort_matches_requested=False),
                 dict(pending, observed_model="grok-synthetic-1"),
                 dict(pending, observed_effort="xhigh"),
                 dict(pending, evidence_source="synthetic-secret")]
        missing = dict(pending)
        del missing["observed_model"]
        cases.append(missing)
        for index, value in enumerate(cases):
            with self.subTest(case=index):
                quality = self.grok_result_with_quality(value, f"grok-bad-pending-{index}")["quality_verification"]
                self.assertEqual(quality["client_evidence_status"], "unavailable")
                self.assertFalse(quality["client_record_verified"])
                self.assertFalse(quality["verified"])
                self.assertFalse(quality["server_effort_verified"])
                self.assertNotIn("synthetic-secret", repr(quality))
        truncated = self.grok_result_with_quality(pending, "grok-truncated-pending", truncated=True)
        self.assertEqual(truncated["quality_verification"]["client_evidence_status"], "unavailable")
        self.assertFalse(truncated["quality_verification"]["client_record_verified"])

    def test_grok_rejects_forged_missing_or_contradictory_client_evidence(self):
        synthetic_key = "x" + "ai-" + "0123456789abcdef"
        variants = (
            {"verified": True}, {"server_effort_verified": True},
            {"evidence_source": "synthetic-secret"},
            {"client_evidence_status": "unavailable"},
            {"client_evidence_status": "matched", "client_model_matches_requested": False},
            {"client_evidence_status": "mismatch"},
            {"client_evidence_status": "mismatch", "client_effort_matches_requested": False,
             "observed_model": None},
            {"client_model_matches_requested": 1}, {"client_effort_matches_requested": "true"},
            {"observed_model": "synthetic-secret"}, {"observed_model": "grok-" + synthetic_key},
            {"observed_effort": "xhigh-synthetic-secret"},
            {"observed_model": None}, {"observed_effort": None},
        )
        for index, changes in enumerate(variants):
            with self.subTest(changes=changes):
                quality = self.grok_result_with_quality(self.grok_session_quality(**changes),
                                                        f"grok-invalid-{index}")["quality_verification"]
                self.assertFalse(quality["verified"])
                self.assertFalse(quality["server_effort_verified"])
                self.assertFalse(quality["client_record_verified"])
                self.assertEqual(quality["client_evidence_status"], "unavailable")
                self.assertEqual(quality["evidence_source"], "not_verified")
                self.assertIsNone(quality["client_model_matches_requested"])
                self.assertIsNone(quality["client_effort_matches_requested"])
                self.assertNotIn("observed_model", quality)
                self.assertNotIn("observed_effort", quality)
                self.assertNotIn("synthetic-secret", repr(quality))
                self.assertNotIn(synthetic_key, repr(quality))

    def test_grok_never_claims_client_match_for_failed_or_truncated_result(self):
        for index, (status, truncated) in enumerate((("failed", False), ("running", False),
                                                     ("succeeded", True), ("succeeded", None))):
            with self.subTest(status=status, truncated=truncated):
                # An explicitly invalid truncation type causes a conservative
                # poll failure, while a truncated success is incomplete.
                result = self.grok_result_with_quality(self.grok_session_quality(client_record_verified=True),
                                                       f"grok-not-complete-{index}",
                                                       status=status, truncated=truncated)
                if truncated is None:
                    self.assertEqual(result["status"], "submission_uncertain")
                    self.assertNotIn("quality_verification", result)
                    continue
                quality = result["quality_verification"]
                self.assertEqual(quality["client_evidence_status"], "unavailable")
                self.assertFalse(quality["client_record_verified"])
                self.assertFalse(quality["verified"])
                self.assertNotIn("observed_model", quality)
                if truncated:
                    self.assertEqual(result["status"], "incomplete")

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

    def test_partial_cli_model_evidence_is_exposed_without_private_metadata_or_effort_claim(self):
        class Adapter:
            def submit(self, **kwargs): return {"status": "queued", "job_id": "job"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "ok", "quality_verification": {
                    "verified": False, "model_usage_matches_requested": True,
                    "effort_verified": False, "recorded_model": "claude-example-1",
                    "evidence_source": "cli_result_model_usage", "private": "synthetic-private"}}
        c = Coordinator(self.db, {"claude": Adapter()})
        c.submit("claude", "task", "quality-key")
        quality = c.result("claude", "quality-key")["quality_verification"]
        self.assertTrue(quality["model_usage_matches_requested"])
        self.assertEqual(quality["recorded_model"], "claude-example-1")
        self.assertFalse(quality["verified"])
        self.assertFalse(quality["effort_verified"])
        self.assertFalse(quality["independently_verified_by_coordinator"])
        self.assertNotIn("synthetic-private", repr(quality))
        self.assertNotIn("actual_effort", quality)

    def test_invalid_model_evidence_does_not_claim_a_match(self):
        class Adapter:
            def submit(self, **kwargs): return {"status": "queued", "job_id": "job"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "ok", "quality_verification": {
                    "verified": False, "model_usage_matches_requested": True,
                    "recorded_model": "synthetic-private", "evidence_source": "cli_result_model_usage"}}
        c = Coordinator(self.db, {"claude": Adapter()})
        c.submit("claude", "task", "invalid-quality-key")
        quality = c.result("claude", "invalid-quality-key")["quality_verification"]
        self.assertIsNone(quality["model_usage_matches_requested"])
        self.assertNotIn("recorded_model", quality)
        self.assertEqual(quality["evidence_source"], "not_verified")
        self.assertNotIn("synthetic-private", repr(quality))

    def test_claude_durable_fixed_poll_errors_survive_coordinator_without_arbitrary_text(self):
        package_root = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, package_root)
        try:
            from claude import durable_claude
            root = Path(self.tmp.name)
            state, profile, work, cli = (root / name for name in ("claude-state", "profile", "work", "cli"))
            for directory in (state, profile, work):
                directory.mkdir()
            adapter = durable_claude.DurableClaudeAdapter(str(cli), str(profile), str(work), str(state),
                                                          "model-example", "medium", enabled=True)
            c = Coordinator(self.db, {"claude": adapter})
            for index, error in enumerate(("worker_unavailable", "worker_setup_failed", "input_too_large",
                                           "synthetic-private")):
                with self.subTest(error=error):
                    job_id, key = "job-" + str(index), "key-" + str(index)
                    with durable_claude._connect(adapter.db) as con:
                        con.execute("INSERT INTO jobs VALUES (?,?,?, ?,NULL,NULL)",
                                    (job_id, "digest", "failed", error))
                    with patch.object(adapter, "submit", return_value={"status": "queued", "job_id": job_id}):
                        c.submit("claude", "task", key)
                    result = c.result("claude", key)
                    self.assertEqual(result["status"], "failed")
                    if error == "synthetic-private":
                        self.assertNotIn("error", result)
                        self.assertNotIn(error, repr(result))
                    else:
                        self.assertEqual(result["error"], error)
        finally:
            sys.path.remove(package_root)

    def test_claude_durable_uuid_failure_is_known_unsubmitted_at_coordinator(self):
        package_root = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, package_root)
        try:
            from claude import durable_claude
            root = Path(self.tmp.name)
            state, profile, work, cli = (root / name for name in ("uuid-state", "uuid-profile", "uuid-work", "uuid-cli"))
            for directory in (state, profile, work):
                directory.mkdir()
            adapter = durable_claude.DurableClaudeAdapter(str(cli), str(profile), str(work), str(state),
                "claude-synthetic", "medium", enabled=True, session_evidence=True)
            coordinator = Coordinator(self.db, {"claude": adapter})
            with patch.object(durable_claude, "uuid") as fake_uuid, \
                 patch.object(durable_claude.subprocess, "Popen", side_effect=AssertionError("must not launch")) as popen:
                fake_uuid.uuid4.side_effect = OSError("synthetic-private")
                receipt = coordinator.submit("claude", "task", "uuid-key")
            self.assertEqual(receipt["status"], "not_submitted")
            self.assertEqual(receipt["reason"], "session_evidence_unavailable")
            self.assertEqual(coordinator.get("uuid-key")["status"], "not_submitted")
            self.assertIsNone(receipt["job_id"])
            self.assertNotIn("synthetic-private", repr(receipt))
            with durable_claude._connect(adapter.db) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM session_evidence_intents").fetchone()[0], 0)
            popen.assert_not_called()
        finally:
            sys.path.remove(package_root)

    def test_claude_adapter_cannot_claim_effort_or_overall_verification(self):
        class Adapter:
            def submit(self, **kwargs): return {"status": "queued", "job_id": "job"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "ok", "quality_verification": {
                    "verified": True, "effort_verified": True, "actual_effort": "medium",
                    "actual_model": "claude-conflicting-model", "model_usage_matches_requested": True,
                    "recorded_model": "claude-example-1", "evidence_source": "cli_result_model_usage"}}
        c = Coordinator(self.db, {"claude": Adapter()})
        c.submit("claude", "task", "effort-key")
        quality = c.result("claude", "effort-key")["quality_verification"]
        self.assertFalse(quality["verified"])
        self.assertFalse(quality["effort_verified"])
        self.assertNotIn("actual_effort", quality)
        self.assertNotIn("actual_model", quality)
        self.assertEqual(quality["recorded_model"], "claude-example-1")

    def test_unproven_claude_actual_model_is_not_forwarded(self):
        class Adapter:
            def submit(self, **kwargs): return {"status": "queued", "job_id": "job"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "ok", "quality_verification": {
                    "actual_model": "claude-unproven-model", "verified": True,
                    "evidence_source": "synthetic-private"}}
        c = Coordinator(self.db, {"claude": Adapter()})
        c.submit("claude", "task", "unproven-model-key")
        quality = c.result("claude", "unproven-model-key")["quality_verification"]
        self.assertNotIn("actual_model", quality)
        self.assertNotIn("recorded_model", quality)
        self.assertFalse(quality["verified"])
        self.assertIsNone(quality["model_usage_matches_requested"])
        self.assertEqual(quality["evidence_source"], "not_verified")
        self.assertNotIn("synthetic-private", repr(quality))

    def test_session_quality_is_narrow_and_never_server_verification(self):
        class Adapter:
            def submit(self, **kwargs): return {"status": "queued", "job_id": "job"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "ok", "quality_verification": {
                    "verified": True, "effort_verified": True, "server_effort_verified": True,
                    "client_evidence_status": "matched", "session_cleanup": "deletion_requested",
                    "client_model_matches_requested": True, "client_effort_matches_requested": True,
                    "path": "synthetic-private", "record": "synthetic-private"}}
        c = Coordinator(self.db, {"claude": Adapter()})
        c.submit("claude", "task", "session-quality")
        quality = c.result("claude", "session-quality")["quality_verification"]
        self.assertTrue(quality["client_effort_matches_requested"])
        self.assertFalse(quality["verified"])
        self.assertFalse(quality["effort_verified"])
        self.assertFalse(quality["server_effort_verified"])
        self.assertNotIn("synthetic-private", repr(quality))

    def test_model_usage_and_client_evidence_are_both_preserved(self):
        class Adapter:
            def submit(self, **kwargs): return {"status": "queued", "job_id": "job"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "ok", "quality_verification": {
                    "verified": False, "model_usage_matches_requested": True,
                    "recorded_model": "claude-synthetic-1", "evidence_source": "cli_result_model_usage",
                    "client_evidence_status": "matched", "session_cleanup": "deletion_requested",
                    "client_model_matches_requested": True, "client_effort_matches_requested": True}}
        c = Coordinator(self.db, {"claude": Adapter()})
        c.submit("claude", "task", "both-quality-fields")
        quality = c.result("claude", "both-quality-fields")["quality_verification"]
        self.assertIs(quality["model_usage_matches_requested"], True)
        self.assertEqual(quality["recorded_model"], "claude-synthetic-1")
        self.assertEqual(quality["evidence_source"], "cli_result_model_usage")
        self.assertEqual(quality["client_evidence_status"], "matched")
        self.assertIs(quality["client_model_matches_requested"], True)
        self.assertIs(quality["client_effort_matches_requested"], True)
        self.assertIs(quality["verified"], False)
        self.assertIs(quality["server_effort_verified"], False)

    def test_inconsistent_session_match_is_not_forwarded_as_verified(self):
        class Adapter:
            def submit(self, **kwargs): return {"status": "queued", "job_id": "job"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "ok", "quality_verification": {
                    "client_evidence_status": "matched", "session_cleanup": "deletion_requested",
                    "client_model_matches_requested": True, "client_effort_matches_requested": None}}
        c = Coordinator(self.db, {"claude": Adapter()})
        c.submit("claude", "task", "invalid-session-quality")
        quality = c.result("claude", "invalid-session-quality")["quality_verification"]
        self.assertEqual(quality["client_evidence_status"], "unavailable")
        self.assertIsNone(quality["client_model_matches_requested"])
        self.assertIsNone(quality["client_effort_matches_requested"])
        self.assertEqual(quality["session_cleanup"], "deletion_requested")

    def test_pending_session_evidence_does_not_block_answer_or_resubmit(self):
        class Adapter:
            def __init__(self): self.calls = 0
            def submit(self, **kwargs):
                self.calls += 1
                return {"status": "queued", "job_id": "job"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "ready", "quality_verification": {
                    "client_evidence_status": "pending", "session_cleanup": "unconfirmed",
                    "client_model_matches_requested": None, "client_effort_matches_requested": None}}
        adapter = Adapter()
        c = Coordinator(self.db, {"claude": adapter})
        c.submit("claude", "task", "pending-key")
        result = c.result("claude", "pending-key")
        self.assertEqual(result["output"], "ready")
        self.assertEqual(result["quality_verification"]["client_evidence_status"], "pending")
        self.assertEqual(c.result("claude", "pending-key")["quality_verification"]["session_cleanup"], "unconfirmed")
        self.assertTrue(c.submit("claude", "task", "pending-key")["replayed"])
        self.assertEqual(adapter.calls, 1)

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

    def test_lost_receipt_is_recovered_by_original_task_id_after_restart(self):
        class Adapter:
            def __init__(self): self.submits = 0; self.recorded = {}
            def submit(self, *, task, task_id):
                self.submits += 1
                self.recorded[task_id] = "original-worker"
                raise RuntimeError("after worker launch")
            def find_job(self, task_id): return self.recorded.get(task_id)
            def poll(self, job_id):
                self.polled = job_id
                return {"status": "succeeded", "output": "original result"}
        adapter = Adapter()
        receipt = Coordinator(self.db, {"claude": adapter}).submit("claude", "task", "key")
        self.assertEqual(receipt["status"], "submission_uncertain")
        self.assertIsNone(receipt["job_id"])
        restarted = Coordinator(self.db, {"claude": adapter})
        recovered = restarted.result("claude", "key")
        self.assertEqual(recovered["job_id"], "original-worker")
        self.assertEqual(recovered["output"], "original result")
        self.assertEqual(adapter.polled, "original-worker")
        self.assertEqual(adapter.submits, 1)
        self.assertEqual(restarted.get("key")["job_id"], "original-worker")

    def test_missing_invalid_or_failed_lookup_remains_uncertain_and_never_retries(self):
        class Adapter:
            def __init__(self): self.submits = 0; self.value = None
            def submit(self, **kwargs):
                self.submits += 1
                raise RuntimeError("unknown outcome")
            def find_job(self, task_id):
                if self.value == "raise": raise RuntimeError("private detail")
                return self.value
            def poll(self, job_id): raise AssertionError("must not poll invalid handle")
        adapter = Adapter()
        c = Coordinator(self.db, {"grok": adapter})
        c.submit("grok", "task", "key")
        for value in (None, "", "x" * 257, 42, "raise"):
            with self.subTest(value=value):
                adapter.value = value
                response = c.result("grok", "key")
                self.assertEqual(response["status"], "submission_uncertain")
                self.assertIsNone(response["job_id"])
                self.assertNotIn("private detail", str(response))
        self.assertEqual(adapter.submits, 1)

    def test_recovery_during_active_dispatch_keeps_original_job_and_terminal_result(self):
        entered, release = threading.Event(), threading.Event()
        class Adapter:
            def __init__(self): self.submits = 0; self.task_id = None
            def submit(self, *, task, task_id):
                self.submits += 1
                self.task_id = task_id
                entered.set()
                if not release.wait(3): raise TimeoutError()
                return {"status": "queued", "job_id": "worker-1"}
            def find_job(self, task_id):
                return "worker-1" if task_id == self.task_id else None
            def poll(self, job_id): return {"status": "succeeded", "output": "done"}
        adapter = Adapter()
        c = Coordinator(self.db, {"grok": adapter})
        thread = threading.Thread(target=lambda: c.submit("grok", "task", "key"))
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(c.result("grok", "key")["output"], "done")
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(c.get("key")["status"], "succeeded")
        self.assertEqual(c.get("key")["job_id"], "worker-1")
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

    def test_contradictory_exit_report_is_not_treated_as_confirmed(self):
        class Adapter:
            def submit(self, **kw): return {"status": "queued", "job_id": "job-1"}
            def poll(self, job_id):
                return {"status": "submission_uncertain", "error": "worker_identity_unconfirmed",
                        "local_exit_confirmed": True, "inspection_metadata_unverified": True}
        coordinator = Coordinator(self.db, {"claude": Adapter()})
        coordinator.submit("claude", "synthetic", "key")
        result = coordinator.result("claude", "key")
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertEqual(result["error"], "worker_identity_unconfirmed")
        self.assertIs(result["local_exit_confirmed"], False)
        self.assertIs(result["inspection_metadata_unverified"], True)

    def test_successful_grok_exit_is_visible_and_not_restarted(self):
        class Adapter:
            def __init__(self): self.calls = 0
            def submit(self, **kw):
                self.calls += 1
                return {"status": "queued", "job_id": "job-1"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "synthetic", "truncated": False,
                        "local_exit_confirmed": True}
        adapter = Adapter()
        coordinator = Coordinator(self.db, {"grok": adapter})
        coordinator.submit("grok", "synthetic", "key")
        self.assertIs(coordinator.result("grok", "key")["local_exit_confirmed"], True)
        self.assertIs(coordinator.result("grok", "key")["local_exit_confirmed"], True)
        self.assertEqual(adapter.calls, 1)

    def test_operator_release_does_not_claim_observed_exit(self):
        class Adapter:
            def submit(self, **kw): return {"status": "queued", "job_id": "job-1"}
            def poll(self, job_id):
                return {"status": "submission_uncertain", "error": "operator_released",
                        "local_exit_confirmed": False, "release_basis": "operator_confirmed"}
        coordinator = Coordinator(self.db, {"grok": Adapter()})
        coordinator.submit("grok", "synthetic", "key")
        result = coordinator.result("grok", "key")
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertEqual(result["error"], "operator_released")
        self.assertEqual(result["release_basis"], "operator_confirmed")
        self.assertIs(result["local_exit_confirmed"], False)

    def test_operator_release_overrides_inconsistent_positive_exit_claim(self):
        class Adapter:
            def submit(self, **kw): return {"status": "queued", "job_id": "job-1"}
            def poll(self, job_id):
                return {"status": "submission_uncertain", "error": "operator_released",
                        "local_exit_confirmed": True, "release_basis": "operator_confirmed"}
        coordinator = Coordinator(self.db, {"grok": Adapter()})
        coordinator.submit("grok", "synthetic", "key")
        result = coordinator.result("grok", "key")
        self.assertEqual(result["release_basis"], "operator_confirmed")
        self.assertIs(result["local_exit_confirmed"], False)

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

    def test_actual_grok_preflight_category_round_trips_without_report_content(self):
        package_root = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, package_root)
        try:
            from grok import grok_adapter
            root = Path(self.tmp.name)
            cli = root / "synthetic-client"
            profile = root / "synthetic-profile"
            cli.touch()
            profile.mkdir()
            adapter = grok_adapter.GrokAdapter(str(cli), "synthetic-model", "medium", str(profile))
            report = {key: [] for key in grok_adapter._INSPECT_KEYS}
            report["skills"] = [{"name": "private-skill-name", "source": {"path": "C:/private/profile"}}]
            coordinator = Coordinator(self.db, {"grok": adapter})
            with patch.object(grok_adapter.subprocess, "run", return_value=Mock(returncode=0, stdout=json.dumps(report).encode())), \
                 patch.object(grok_adapter.subprocess, "Popen", side_effect=AssertionError("must not start")) as launch:
                coordinator.submit("grok", "synthetic task", "request-key")
                response = {}
                for _ in range(300):
                    response = coordinator.result("grok", "request-key")
                    if response.get("status") == "failed":
                        break
                    threading.Event().wait(0.01)
                self.assertEqual(response.get("status"), "failed")
                self.assertEqual(response.get("inspection_reason"), "skills_present_or_unknown")
                self.assertNotIn("private", repr(response))
                launch.assert_not_called()
        finally:
            sys.path.remove(package_root)

    def test_durable_grok_preflight_diagnostic_survives_worker_and_coordinator_restart(self):
        # Spawn only a synthetic Python worker. Its fake inspect contains
        # private-looking placeholders, and its provider-launch stub fails if
        # invoked. No official client, login, or network is used.
        root = Path(self.tmp.name)
        package_root = str(Path(__file__).resolve().parents[1])
        sys.path.insert(0, package_root)
        try:
            from grok import durable_grok
            state, profile, cli = root / "grok-state", root / "grok-profile", root / "synthetic-grok"
            state.mkdir()
            profile.mkdir()
            cli.touch()
            marker = root / "provider-launch-attempted"
            fake_worker = root / "fake_preflight_worker.py"
            fake_worker.write_text('''import json, sys\nfrom pathlib import Path\nfrom types import SimpleNamespace\nsys.path.insert(0, sys.argv[1])\nfrom grok import durable_grok, grok_adapter\nreport = {key: [] for key in grok_adapter._INSPECT_KEYS}\nreport["skills"] = [{"name":"private-skill-name", "source":{"path":"C:/private/profile"}}]\ndef fake_inspect(*args, **kwargs):\n    return SimpleNamespace(returncode=0, stdout=json.dumps(report).encode())\ndef forbidden_launch(*args, **kwargs):\n    Path(sys.argv[4]).touch()\n    raise AssertionError("provider launch is forbidden")\ngrok_adapter.subprocess.run = fake_inspect\ngrok_adapter.subprocess.Popen = forbidden_launch\nraise SystemExit(durable_grok.worker_main(sys.argv[2], sys.argv[3]))\n''', encoding="utf-8")

            def fake_command(work_root, job_id):
                return [sys.executable, str(fake_worker), package_root, str(work_root), job_id, str(marker)]

            adapter = durable_grok.DurableGrokAdapter(str(cli), "synthetic-model", "medium",
                                                       str(profile), str(state), max_concurrency=1)
            coordinator = Coordinator(self.db, {"grok": adapter})
            spawned = []
            real_popen = subprocess.Popen
            def start_worker(*args, **kwargs):
                child = real_popen(*args, **kwargs)
                spawned.append(child)
                return child

            with patch.object(durable_grok, "_worker_command", side_effect=fake_command), \
                 patch.object(durable_grok.subprocess, "Popen", side_effect=start_worker) as launch:
                submitted = coordinator.submit("grok", "synthetic task", "grok-key")
                self.assertEqual(submitted["status"], "queued")
                self.assertEqual(len(spawned), 1)
                self.assertEqual(spawned[0].wait(timeout=10), 0)
                self.assertEqual(launch.call_count, 1)
            self.assertFalse(marker.exists())

            class Other:
                def submit(self, **kwargs):
                    return {"status": "queued", "job_id": "synthetic-other-job"}
                def poll(self, job_id):
                    return {"status": "succeeded", "output": "independent answer"}

            restored_adapter = durable_grok.DurableGrokAdapter(str(cli), "synthetic-model", "medium",
                                                                str(profile), str(state), max_concurrency=1)
            restored = Coordinator(self.db, {"grok": restored_adapter, "claude": Other()})
            with patch.object(durable_grok.subprocess, "Popen", side_effect=AssertionError("must not resend")) as launch:
                result = restored.result("grok", "grok-key")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"], "unexpected_or_unknown_extensions")
                self.assertEqual(result["inspection_reason"], "skills_present_or_unknown")
                self.assertNotIn("private", repr(result))
                replay = restored.submit("grok", "synthetic task", "grok-key")
                self.assertTrue(replay["replayed"])
                self.assertEqual(replay["job_id"], submitted["job_id"])
                self.assertEqual(restored_adapter.status()["local_available_slots"], 1)
                restored.submit("claude", "independent task", "claude-key")
                self.assertEqual(restored.result("claude", "claude-key")["output"], "independent answer")
                launch.assert_not_called()
            self.assertFalse(marker.exists())
        finally:
            sys.path.remove(package_root)

    def test_grok_preflight_category_is_fixed_and_other_provider_continues(self):
        class Adapter:
            def __init__(self, reason, error="unexpected_or_unknown_extensions"):
                self.reason = reason
                self.error = error
            def submit(self, **kw):
                return {"status": "queued", "job_id": "job-1"}
            def poll(self, job_id):
                return {"status": "failed", "error": self.error, "inspection_reason": self.reason}

        class Other:
            def submit(self, **kw):
                return {"status": "queued", "job_id": "job-2"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "independent answer"}

        adapter = Adapter("skills_present_or_unknown")
        c = Coordinator(self.db, {"grok": adapter, "claude": Other()})
        c.submit("grok", "task", "grok-key")
        self.assertEqual(c.result("grok", "grok-key")["inspection_reason"], "skills_present_or_unknown")
        c.submit("claude", "independent task", "claude-key")
        self.assertEqual(c.result("claude", "claude-key")["output"], "independent answer")
        adapter.reason = "C:/private-profile/secret"
        leaked = c.result("grok", "grok-key")
        self.assertNotIn("inspection_reason", leaked)
        self.assertNotIn("private-profile", repr(leaked))
        adapter.reason = "skills_present_or_unknown"
        adapter.error = "other-error"
        self.assertNotIn("inspection_reason", c.result("grok", "grok-key"))

    def test_truncated_success_is_incomplete_with_original_execution_status(self):
        class Adapter:
            def __init__(self): self.submits = 0
            def submit(self, **kw):
                self.submits += 1
                return {"status": "queued", "job_id": "job-1"}
            def poll(self, job_id):
                return {"status": "succeeded", "output": "partial body", "truncated": True,
                        "local_exit_confirmed": True}
        adapter = Adapter()
        c = Coordinator(self.db, {"grok": adapter})
        c.submit("grok", "task", "key")
        response = c.result("grok", "key")
        self.assertEqual(response["status"], "incomplete")
        self.assertEqual(response["execution_status"], "succeeded")
        self.assertEqual(response["output_kind"], "partial")
        self.assertIs(response["local_exit_confirmed"], True)
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
