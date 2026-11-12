"""Synthetic subprocess tests only; no Claude client, network, or secrets."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from claude_adapter import ClaudeAdapter, child_environment, _NETWORK_ENV_NAMES, _model_quality


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.cli = str(base / "claude-fake")
        self.profile = str(base / "profile")
        self.work = str(base / "work")
        self.options = (self.cli, self.profile, self.work, "model-example", "medium")
        self.auth = SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": True,
            "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}))
        self.ok = SimpleNamespace(returncode=0, stdout=json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "synthetic response"}))

    def wait_job(self, adapter, job_id):
        for _ in range(100):
            result = adapter.poll(job_id)
            if result["status"] not in ("queued", "running"):
                return result
            time.sleep(0.005)
        self.fail("synthetic job did not finish")

    def test_no_api_environment_is_inherited(self):
        source = {"PATH": "synthetic-path", "ANTHROPIC_API_KEY": "synthetic-secret",
                  "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-secret", "AWS_SECRET_ACCESS_KEY": "synthetic-secret",
                  "CLAUDE_CODE_USE_BEDROCK": "1", "XAI_API_KEY": "synthetic-secret"}
        result = child_environment(source, self.profile, self.work)
        self.assertEqual(result["PATH"], "synthetic-path")
        self.assertEqual(result["CLAUDE_CONFIG_DIR"], self.profile)
        for name in set(source) - {"PATH"}:
            self.assertNotIn(name, result)
        self.assertNotIn("synthetic-secret", repr(result))

    def test_proxy_and_ca_configuration_is_passed_without_api_credentials(self):
        # All values in this synthetic mapping are test fixtures, not host data.
        source = {key: "synthetic-value" for key in _NETWORK_ENV_NAMES}
        source.update({"HTTPS_PROXY": "http://synthetic-user:synthetic-password@127.0.0.1:8080",
                       "ANTHROPIC_API_KEY": "synthetic-api-key", "NODE_OPTIONS": "synthetic-option"})
        result = child_environment(source, self.profile, self.work)
        for key in _NETWORK_ENV_NAMES:
            self.assertEqual(result[key], source[key])
        self.assertNotIn("ANTHROPIC_API_KEY", result)
        self.assertNotIn("NODE_OPTIONS", result)

    def test_invalid_proxy_environment_value_is_not_inherited(self):
        self.assertNotIn("HTTPS_PROXY", child_environment({"HTTPS_PROXY": "invalid\0value"}, self.profile, self.work))

    def test_disabled_does_not_start_a_process(self):
        adapter = ClaudeAdapter(*self.options)
        with patch("claude_adapter.subprocess.run", side_effect=AssertionError("launched")), \
             patch("claude_adapter.subprocess.Popen", side_effect=AssertionError("launched")):
            self.assertEqual(adapter.submit("task", "id1")["status"], "not_submitted")

    def test_status_reports_unknown_readiness_without_invoking_cli(self):
        for enabled in (False, True):
            adapter = ClaudeAdapter(*self.options, enabled=enabled)
            with patch("claude_adapter.subprocess.run", side_effect=AssertionError("launched")), \
                 patch("claude_adapter.subprocess.Popen", side_effect=AssertionError("launched")):
                status = adapter.status()
            self.assertIsNone(status["ready"])
            self.assertEqual(status["configured"], enabled)
            self.assertEqual(status["verification"], "not_verified")

    def test_user_login_invokes_only_official_interactive_cli(self):
        adapter = ClaudeAdapter(*self.options)
        with patch("claude_adapter.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(adapter.login(), {"status": "finished"})
        args, kwargs = run.call_args
        self.assertEqual(args[0], [self.cli, "auth", "login"])
        self.assertNotIn("capture_output", kwargs)
        self.assertEqual(kwargs["env"]["CLAUDE_CONFIG_DIR"], self.profile)

    def test_subscription_login_then_one_text_request(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        process = Mock(returncode=0)
        process.communicate.return_value = (self.ok.stdout, "")
        with patch("claude_adapter.subprocess.run", return_value=self.auth) as run, \
             patch("claude_adapter.subprocess.Popen", return_value=process) as popen:
            self.assertEqual(adapter.submit("synthetic task", "id2")["status"], "queued")
            result = self.wait_job(adapter, "id2")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["output"], "synthetic response")
        self.assertEqual(result["model_verification"], "unverified")
        self.assertEqual(result["effort_verification"], "unverified")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(popen.call_count, 1)
        auth, generate = run.call_args, popen.call_args
        self.assertEqual(auth.args[0], [self.cli, "auth", "status"])
        self.assertNotIn("synthetic task", generate.args[0])
        process.communicate.assert_called_once_with(input="synthetic task", timeout=900)
        self.assertFalse(generate.kwargs["shell"])
        self.assertEqual(generate.args[0][generate.args[0].index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", generate.args[0])
        self.assertNotIn("--bare", generate.args[0])

    def test_nonzero_structured_error_is_failed_without_returning_body(self):
        adapter = ClaudeAdapter(*self.options, enabled=True, max_concurrency=1)
        process = Mock(returncode=1)
        process.communicate.return_value = (json.dumps({"is_error": True, "result": "synthetic-sensitive"}), "")
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", return_value=process) as started:
            adapter.submit("task", "error1")
            result = self.wait_job(adapter, "error1")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"], "provider_reported_error")
            self.assertTrue(result["local_exit_confirmed"])
            self.assertNotIn("synthetic-sensitive", repr(result))
            replay = adapter.submit("task", "error1")
            self.assertEqual(replay, {"status": "not_submitted", "reason": "already_submitted",
                                      "existing_status": "failed", "job_id": "error1", "replayed": True})
            for _ in range(100):
                if adapter.status()["local_active"] == 0:
                    break
                time.sleep(0.005)
            self.assertEqual(adapter.status()["local_active"], 0)
            self.assertEqual(started.call_count, 1)

    def test_same_invocation_model_usage_is_narrowly_projected_without_effort_claim(self):
        options = (self.cli, self.profile, self.work, "claude-example-1", "medium")
        adapter = ClaudeAdapter(*options, enabled=True)
        payload = {"type": "result", "subtype": "success", "is_error": False, "result": "synthetic response",
                   "modelUsage": {"claude-example-1": {"inputTokens": 3, "outputTokens": 2,
                                                         "private": "synthetic-private"}},
                   "effort": "medium", "other": "synthetic-private"}
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps(payload), "")
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", return_value=process) as launched:
            adapter.submit("synthetic task", "quality-job")
            result = self.wait_job(adapter, "quality-job")
        self.assertEqual(result["quality_verification"], {
            "verified": False, "model_usage_matches_requested": True, "effort_verified": False,
            "recorded_model": "claude-example-1", "evidence_source": "cli_result_model_usage"})
        self.assertEqual(result["model_verification"], "unverified")
        self.assertEqual(result["effort_verification"], "unverified")
        self.assertNotIn("synthetic-private", repr(result))
        self.assertIn("--no-session-persistence", launched.call_args.args[0])

    def test_ambiguous_or_missing_usage_never_becomes_model_evidence(self):
        valid = {"inputTokens": 1, "outputTokens": 2}
        cases = [None, {}, [], {"claude-example-1": None}, {"claude-example-1": {}},
                 {"claude-example-1": {"inputTokens": 0, "outputTokens": 0}},
                 {"claude-example-1": {"inputTokens": True, "outputTokens": 1}},
                 {"claude-example-1": {"inputTokens": -1, "outputTokens": 1}},
                 {"claude-example-2": valid},
                 {"claude-example-1": valid, "claude-example-2": valid},
                 {"claude-example-1": {**valid, "canonicalModel": "claude-example-2"}}]
        for usage in cases:
            with self.subTest(usage=usage):
                quality = _model_quality({"type": "result", "subtype": "success", "modelUsage": usage}, "claude-example-1")
                self.assertFalse(quality["model_usage_matches_requested"])
                self.assertFalse(quality["effort_verified"])
                self.assertNotIn("recorded_model", quality)
        self.assertFalse(_model_quality({"type": "result", "subtype": "success", "modelUsage": {"alias": valid}}, "alias")["model_usage_matches_requested"])
        for field in ("type", "subtype"):
            payload = {"type": "result", "subtype": "success", "modelUsage": {"claude-example-1": valid}}
            del payload[field]
            self.assertFalse(_model_quality(payload, "claude-example-1")["model_usage_matches_requested"])

    def test_duplicate_json_members_cannot_become_usable_response(self):
        options = (self.cli, self.profile, self.work, "claude-example-1", "medium")
        adapter = ClaudeAdapter(*options, enabled=True)
        process = Mock(returncode=0)
        process.communicate.return_value = ('{"type":"result","subtype":"success","is_error":false,"result":"ok",'
            '"modelUsage":{"claude-example-2":{"inputTokens":1,"outputTokens":1}},'
            '"modelUsage":{"claude-example-1":{"inputTokens":1,"outputTokens":1}}}', "")
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", return_value=process):
            adapter.submit("task", "ambiguous")
            result = self.wait_job(adapter, "ambiguous")
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertEqual(result["error"], "invalid_provider_response")
        self.assertNotIn("output", result)
        self.assertNotIn("quality_verification", result)

    def test_duplicate_success_and_auth_fields_are_rejected(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        duplicate_auth = SimpleNamespace(returncode=0, stdout='{"loggedIn":false,"loggedIn":true, '
            '"authMethod":"claude.ai","apiProvider":"firstParty","subscriptionType":"max"}')
        with patch("claude_adapter.subprocess.run", return_value=duplicate_auth), \
             patch("claude_adapter.subprocess.Popen", side_effect=AssertionError("must not launch")) as popen:
            adapter.submit("task", "duplicate-auth")
            self.assertEqual(self.wait_job(adapter, "duplicate-auth")["error"], "subscription_login_unavailable")
            popen.assert_not_called()
        for name, stdout in (("error", '{"is_error":true,"is_error":false,"result":"private"}'),
                             ("result", '{"is_error":false,"result":"first","result":"private"}')):
            with self.subTest(name=name):
                process = Mock(returncode=0)
                process.communicate.return_value = (stdout, "")
                with patch("claude_adapter.subprocess.run", return_value=self.auth), \
                     patch("claude_adapter.subprocess.Popen", return_value=process):
                    adapter.submit("task", "duplicate-" + name)
                    result = self.wait_job(adapter, "duplicate-" + name)
                self.assertEqual(result["status"], "submission_uncertain")
                self.assertEqual(result["error"], "invalid_provider_response")
                self.assertNotIn("output", result)
                self.assertNotIn("private", repr(result))

    def test_unknown_exit_cannot_become_success_or_model_evidence(self):
        options = (self.cli, self.profile, self.work, "claude-example-1", "medium")
        adapter = ClaudeAdapter(*options, enabled=True)
        process = Mock(returncode=False)  # False == 0, but is not an exit code.
        process.communicate.return_value = (json.dumps({"is_error": False, "result": "ok",
                                                       "modelUsage": {"claude-example-1": {}}}), "")
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", return_value=process):
            adapter.submit("task", "exit-unknown")
            result = self.wait_job(adapter, "exit-unknown")
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertFalse(result["local_exit_confirmed"])
        self.assertNotIn("quality_verification", result)

    def test_missing_error_flag_is_uncertain_not_reported_failure(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps({"result": "synthetic response"}), "")
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", return_value=process):
            adapter.submit("task", "missing-flag")
            result = self.wait_job(adapter, "missing-flag")
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertEqual(result["error"], "invalid_provider_response")
        self.assertTrue(result["local_exit_confirmed"])

    def test_result_requires_noncontradictory_official_success_shape(self):
        valid = {"type": "result", "subtype": "success", "is_error": False, "result": "private synthetic"}
        cases = ({**valid, "type": "assistant"}, {**valid, "subtype": "error_max_turns"},
                 {**valid, "subtype": "future_unknown"}, {**valid, "is_error": True},
                 {**valid, "result": None},
                 {key: value for key, value in valid.items() if key != "type"},
                 {key: value for key, value in valid.items() if key != "subtype"})
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                adapter = ClaudeAdapter(*self.options, enabled=True)
                process = Mock(returncode=0)
                process.communicate.return_value = (json.dumps(payload), "")
                with patch("claude_adapter.subprocess.run", return_value=self.auth), \
                     patch("claude_adapter.subprocess.Popen", return_value=process) as popen:
                    adapter.submit("task", "invalid-success")
                    result = self.wait_job(adapter, "invalid-success")
                self.assertEqual(result["status"], "submission_uncertain")
                self.assertEqual(result["error"], "invalid_provider_response")
                self.assertTrue(result["local_exit_confirmed"])
                self.assertNotIn("output", result)
                self.assertNotIn("private synthetic", repr(result))
                self.assertEqual(popen.call_count, 1)

    def test_known_error_shape_stays_failed_without_exposing_body(self):
        for subtype in ("error_max_turns", "error_during_execution", "error_max_budget_usd",
                        "error_max_structured_output_retries"):
            with self.subTest(subtype=subtype):
                adapter = ClaudeAdapter(*self.options, enabled=True)
                process = Mock(returncode=1)
                process.communicate.return_value = (json.dumps({"type": "result", "subtype": subtype,
                    "is_error": True, "errors": ["private synthetic"]}), "")
                with patch("claude_adapter.subprocess.run", return_value=self.auth), \
                     patch("claude_adapter.subprocess.Popen", return_value=process):
                    adapter.submit("task", "error-shape")
                    result = self.wait_job(adapter, "error-shape")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"], "provider_reported_error")
                self.assertNotIn("private synthetic", repr(result))

    def test_nonzero_unstructured_error_remains_uncertain(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        process = Mock(returncode=1)
        process.communicate.return_value = ("synthetic-sensitive", "")
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", return_value=process):
            adapter.submit("task", "error2")
            result = self.wait_job(adapter, "error2")
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertTrue(result["local_exit_confirmed"])
        self.assertNotIn("synthetic-sensitive", repr(result))

    def test_communication_failure_keeps_slot_when_child_exit_is_unknown(self):
        adapter = ClaudeAdapter(*self.options, enabled=True, max_concurrency=1)
        process = Mock(returncode=None)
        process.communicate.side_effect = OSError("synthetic-private")
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", return_value=process) as popen:
            adapter.submit("task", "unknown-child")
            result = self.wait_job(adapter, "unknown-child")
            self.assertEqual(result["status"], "submission_uncertain")
            self.assertFalse(result["local_exit_confirmed"])
            self.assertEqual(adapter.submit("task", "unknown-child")["status"], "submission_uncertain")
            self.assertEqual(adapter.submit("other task", "other")["reason"], "provider_busy")
            self.assertEqual(adapter.status()["local_active"], 1)
            self.assertEqual(popen.call_count, 1)
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        self.assertNotIn("synthetic-private", repr(result))

    def test_spawn_failure_keeps_slot_when_child_creation_is_unknown(self):
        adapter = ClaudeAdapter(*self.options, enabled=True, max_concurrency=1)
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", side_effect=OSError("synthetic-private")) as popen:
            adapter.submit("task", "unknown-spawn")
            result = self.wait_job(adapter, "unknown-spawn")
            self.assertEqual(result["status"], "submission_uncertain")
            self.assertFalse(result["local_exit_confirmed"])
            self.assertEqual(adapter.submit("other", "other")["reason"], "provider_busy")
            self.assertEqual(adapter.status()["local_active"], 1)
            self.assertEqual(popen.call_count, 1)

    def test_confirmed_exit_releases_slot_even_if_remote_result_uncertain(self):
        adapter = ClaudeAdapter(*self.options, enabled=True, max_concurrency=1)
        process = Mock(returncode=1)
        process.communicate.return_value = ("not json", "")
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", return_value=process) as popen:
            adapter.submit("task", "finished-uncertain")
            result = self.wait_job(adapter, "finished-uncertain")
            self.assertEqual(result["status"], "submission_uncertain")
            self.assertTrue(result["local_exit_confirmed"])
            for _ in range(100):
                if adapter.status()["local_active"] == 0:
                    break
                time.sleep(.005)
            self.assertEqual(adapter.status()["local_active"], 0)
            self.assertEqual(popen.call_count, 1)

    def test_structured_error_without_confirmed_exit_stays_uncertain(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        process = Mock(returncode=None)
        process.communicate.return_value = (json.dumps({"is_error": True}), "")
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", return_value=process):
            adapter.submit("task", "error3")
            result = self.wait_job(adapter, "error3")
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertFalse(result["local_exit_confirmed"])

    def test_login_failure_never_sends_model_request(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        fail = SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": True, "authMethod": "api_key"}))
        with patch("claude_adapter.subprocess.run", return_value=fail) as run, \
             patch("claude_adapter.subprocess.Popen", side_effect=AssertionError("launched")):
            adapter.submit("task", "id3")
            result = self.wait_job(adapter, "id3")
        self.assertEqual(result, {"status": "failed", "error": "subscription_login_unavailable"})
        self.assertEqual(run.call_count, 1)
        for _ in range(100):
            if adapter.status()["local_active"] == 0:
                break
            time.sleep(.005)
        self.assertEqual(adapter.status()["local_active"], 0)

    def test_slow_process_completes_without_kill_or_resubmission(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        release = threading.Event()
        process = Mock(returncode=0)
        def communicate(*args, **kwargs):
            if kwargs:
                raise subprocess.TimeoutExpired("synthetic", 30)
            if not release.wait(2):
                raise AssertionError("test release timed out")
            return self.ok.stdout, ""
        process.communicate.side_effect = communicate
        with patch("claude_adapter.subprocess.run", return_value=self.auth) as run, \
             patch("claude_adapter.subprocess.Popen", return_value=process) as popen:
            try:
                adapter.submit("task", "id4")
                result = self.wait_job(adapter, "id4")
                self.assertEqual(result["status"], "submission_uncertain")
                self.assertEqual(result["error"], "provider_slow")
                replay = adapter.submit("task", "id4")
                self.assertEqual(replay["status"], "submission_uncertain")
                release.set()
                for _ in range(100):
                    result = adapter.poll("id4")
                    if result["status"] == "succeeded":
                        break
                    time.sleep(0.005)
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["output"], "synthetic response")
                self.assertNotIn("error", result)
            finally:
                release.set()
        self.assertTrue(replay["replayed"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(process.communicate.call_args_list, [call(input="task", timeout=900), call()])
        process.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_slow_real_local_child_remains_alive_and_result_is_collected(self):
        # This is a synthetic Python child, not Claude or a network request.
        Path(self.work).mkdir()
        Path(self.profile).mkdir()
        adapter = ClaudeAdapter(*self.options, enabled=True)
        adapter.timeout = 0.05  # Synthetic test only; constructor normally requires >= 30s.
        original_popen = subprocess.Popen
        spawned = []
        def fake_cli(_command, **kwargs):
            code = ("import sys,time,json; data=sys.stdin.read(); time.sleep(.4); "
                    "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': data}))")
            process = original_popen([sys.executable, "-c", code], **kwargs)
            spawned.append(process)
            return process
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", side_effect=fake_cli):
            adapter.submit("synthetic local input", "id-real-child")
            warning = self.wait_job(adapter, "id-real-child")
            self.assertEqual(warning["status"], "submission_uncertain")
            self.assertIsNone(spawned[0].poll())
            for _ in range(150):
                result = adapter.poll("id-real-child")
                if result["status"] == "succeeded":
                    break
                time.sleep(0.01)
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["output"], "synthetic local input")
            self.assertEqual(len(spawned), 1)

    def test_errors_never_include_provider_exception_or_credentials(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        with patch("claude_adapter.subprocess.run", side_effect=OSError("synthetic-secret")):
            adapter.submit("task", "id5")
            result = self.wait_job(adapter, "id5")
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("synthetic-secret", repr(result))

    def test_unknown_job_after_restart_stays_uncertain(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        self.assertEqual(adapter.poll("prior-job"), {"status": "submission_uncertain", "error": "unknown_job"})

    def test_same_id_different_task_rejected_without_new_worker(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        with patch.object(threading.Thread, "start") as start:
            self.assertEqual(adapter.submit("task one", "id6")["status"], "queued")
            self.assertEqual(adapter.submit("task two", "id6")["reason"], "task_id_conflict")
        self.assertEqual(start.call_count, 1)

    def test_replay_queued_preserves_original_job_without_starting_again(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        with patch.object(threading.Thread, "start") as start:
            self.assertEqual(adapter.submit("synthetic task", "queued-job")["status"], "queued")
            replay = adapter.submit("synthetic task", "queued-job")
        self.assertEqual(replay, {"status": "queued", "job_id": "queued-job", "replayed": True})
        self.assertEqual(start.call_count, 1)
        self.assertEqual(adapter.status()["local_active"], 1)

    def test_replay_unknown_state_is_uncertain_and_keeps_capacity(self):
        adapter = ClaudeAdapter(*self.options, enabled=True, max_concurrency=1)
        with patch.object(threading.Thread, "start") as start:
            adapter.submit("synthetic task", "unknown-state")
            adapter._update("unknown-state", status="future-state")
            replay = adapter.submit("synthetic task", "unknown-state")
        self.assertEqual(replay, {"status": "submission_uncertain", "job_id": "unknown-state", "replayed": True})
        self.assertEqual(start.call_count, 1)
        self.assertEqual(adapter.status()["local_active"], 1)

    def test_replay_succeeded_keeps_original_job_without_resubmission(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        process = Mock(returncode=0)
        process.communicate.return_value = (self.ok.stdout, "")
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", return_value=process) as popen:
            adapter.submit("synthetic task", "finished-job")
            self.assertEqual(self.wait_job(adapter, "finished-job")["status"], "succeeded")
            replay = adapter.submit("synthetic task", "finished-job")
            conflict = adapter.submit("different task", "finished-job")
        self.assertEqual(replay, {"status": "not_submitted", "reason": "already_submitted",
                                  "existing_status": "succeeded", "job_id": "finished-job", "replayed": True})
        self.assertEqual(conflict["reason"], "task_id_conflict")
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(adapter.poll("finished-job")["output"], "synthetic response")

    def test_two_jobs_run_concurrently_busy_is_not_submitted_and_slots_release(self):
        adapter = ClaudeAdapter(*self.options, enabled=True)
        release = threading.Event()
        started_lock = threading.Lock()
        started = []
        def process_factory(*_args, **_kwargs):
            process = Mock(returncode=0)
            def communicate(*_args, **_kwargs):
                with started_lock:
                    started.append(1)
                if not release.wait(2):
                    raise AssertionError("test release timed out")
                return self.ok.stdout, ""
            process.communicate.side_effect = communicate
            return process
        with patch("claude_adapter.subprocess.run", return_value=self.auth), \
             patch("claude_adapter.subprocess.Popen", side_effect=process_factory) as popen:
            try:
                self.assertEqual(adapter.submit("task one", "concurrent-1")["status"], "queued")
                self.assertEqual(adapter.submit("task two", "concurrent-2")["status"], "queued")
                for _ in range(100):
                    with started_lock:
                        if len(started) == 2:
                            break
                    time.sleep(0.005)
                self.assertEqual(len(started), 2)
                self.assertEqual(adapter.status()["local_active"], 2)
                self.assertEqual(adapter.status()["local_limit"], 2)
                self.assertEqual(adapter.submit("task three", "concurrent-3"),
                                 {"status": "not_submitted", "reason": "provider_busy"})
                replay = adapter.submit("task one", "concurrent-1")
                self.assertTrue(replay["replayed"])
                self.assertEqual(replay["status"], "running")
                self.assertEqual(replay["job_id"], "concurrent-1")
                self.assertEqual(popen.call_count, 2)
                release.set()
                self.assertEqual(self.wait_job(adapter, "concurrent-1")["status"], "succeeded")
                self.assertEqual(self.wait_job(adapter, "concurrent-2")["status"], "succeeded")
                for _ in range(100):
                    if adapter.status()["local_active"] == 0:
                        break
                    time.sleep(0.005)
                self.assertEqual(adapter.status()["local_active"], 0)
                self.assertEqual(adapter.submit("task three", "concurrent-3")["status"], "queued")
                self.assertEqual(self.wait_job(adapter, "concurrent-3")["status"], "succeeded")
            finally:
                release.set()

    def test_worker_start_failure_releases_capacity(self):
        adapter = ClaudeAdapter(*self.options, enabled=True, max_concurrency=1)
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("synthetic")):
            self.assertEqual(adapter.submit("task", "job")["reason"], "worker_unavailable")
        self.assertEqual(adapter.status()["local_active"], 0)

    def test_invalid_local_capacity_rejected(self):
        for value in (0, 9, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ClaudeAdapter(*self.options, max_concurrency=value)


if __name__ == "__main__":
    unittest.main()
