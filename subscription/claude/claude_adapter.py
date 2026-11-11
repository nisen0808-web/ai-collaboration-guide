"""Candidate adapter for the user's own installed Claude Code CLI.

No API keys are requested or read. Users sign in with the official client and
are responsible for its account and billing settings. This candidate has not
been approved for public distribution or tested against a real provider.
"""
from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping
import hashlib
import json
import os
import re
import subprocess
import threading

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_EFFORTS = frozenset({"low", "medium", "high", "max"})
_NETWORK_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                      "http_proxy", "https_proxy", "all_proxy", "no_proxy",
                      "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR")
_INHERIT = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "NO_COLOR", *_NETWORK_ENV_NAMES})
_CLAUDE_ACCOUNT = frozenset({"claude.ai", "claude_ai", "claudeai", "claude.ai oauth", "claude.ai/oauth"})
_PLANS = frozenset({"free", "pro", "max", "ultra", "team", "enterprise", "claude_pro", "claude_max",
                    "default_claude_pro", "default_claude_max_5x", "default_claude_max_20x", "max5x", "max20x"})


def _absolute(path) -> str:
    if not isinstance(path, (str, os.PathLike)):
        raise ValueError("invalid path")
    value = os.fspath(path)
    if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
        raise ValueError("path must be absolute")
    return value


def child_environment(source: Mapping, profile_dir: str, work_dir: str) -> dict[str, str]:
    """Allowlist launch variables; never inspect API key variables.

    CLI configuration, managed policy and account settings can still affect
    billing. This function does not claim to verify a request's billing route.
    Proxy settings may themselves contain credentials and must not be logged.
    """
    if not isinstance(source, Mapping):
        raise TypeError("source must be a mapping")
    profile = _absolute(profile_dir)
    work = _absolute(work_dir)
    env = {}
    for name in _INHERIT:
        value = source.get(name)
        if isinstance(value, str) and "\0" not in value:
            env[name] = value
    env.update({"CLAUDE_CONFIG_DIR": profile, "HOME": work, "USERPROFILE": work,
                "TEMP": work, "TMP": work, "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_MAX_RETRIES": "0", "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0"})
    return env


def _subscription_login(payload: object) -> bool:
    if not isinstance(payload, dict) or payload.get("loggedIn") is not True:
        return False
    method = payload.get("authMethod")
    provider = payload.get("apiProvider") or ""
    plan = payload.get("subscriptionType")
    if not isinstance(method, str) or not isinstance(provider, str) or not isinstance(plan, str):
        return False
    method = method.strip().lower()
    provider = provider.strip().lower().replace("_", "")
    plan = plan.strip().lower().replace("-", "_")
    return (method in _CLAUDE_ACCOUNT or (method == "oauth" and provider == "firstparty")) and provider in {"", "firstparty"} and plan in _PLANS


class ClaudeAdapter:
    """In-process asynchronous text worker; no durable replay after restart.

    The coordinator must persist task IDs and never retry a request whose
    submission is unknown. The user must opt in to enable this candidate.
    Existing CLI profile contents are neither copied nor read by this code;
    the official CLI may access its own login in the selected profile.
    """
    def __init__(self, cli_path, profile_dir, work_dir, model, effort, *, enabled=False,
                 timeout_seconds=900, max_concurrency=2):
        self.cli_path = _absolute(cli_path)
        self.profile_dir = _absolute(profile_dir)
        self.work_dir = _absolute(work_dir)
        if not isinstance(model, str) or not _MODEL.fullmatch(model):
            raise ValueError("invalid model")
        if not isinstance(effort, str) or effort not in _EFFORTS:
            raise ValueError("invalid effort")
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 3600:
            raise ValueError("invalid timeout")
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 8:
            raise ValueError("invalid max_concurrency")
        self.model, self.effort = model, effort
        self.enabled, self.timeout = enabled, timeout_seconds
        # This is a local safety limit, not a provider quota or plan limit.
        self.max_concurrency = max_concurrency
        self._active = 0
        self._jobs = {}
        self._lock = threading.Lock()

    def submit(self, task: str, task_id: str) -> dict:
        if not self.enabled:
            return {"status": "not_submitted", "reason": "adapter_unavailable"}
        if not isinstance(task, str) or not task.strip() or len(task) > 100000:
            return {"status": "not_submitted", "reason": "invalid_task"}
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            return {"status": "not_submitted", "reason": "invalid_task_id"}
        digest = hashlib.sha256(task.encode("utf-8")).digest()
        with self._lock:
            if task_id in self._jobs:
                if self._jobs[task_id]["digest"] != digest:
                    return {"status": "not_submitted", "reason": "task_id_conflict"}
                return {"status": "queued", "job_id": task_id, "replayed": True}
            if self._active >= self.max_concurrency:
                return {"status": "not_submitted", "reason": "provider_busy"}
            self._active += 1
            self._jobs[task_id] = {"status": "queued", "digest": digest}
            worker = threading.Thread(target=self._worker, args=(task_id, task), daemon=True)
            try:
                worker.start()
            except Exception:
                self._jobs.pop(task_id, None)
                self._active -= 1
                return {"status": "not_submitted", "reason": "worker_unavailable"}
        return {"status": "queued", "job_id": task_id}

    def login(self) -> dict:
        """Let the user interact directly with official Claude Code login.

        This invokes no model request, captures no login output, asks for no
        key, and writes no authentication data itself. The CLI can save its own
        session under the user-chosen CLAUDE_CONFIG_DIR. The user must run this
        in an interactive terminal and complete any official browser flow.
        """
        try:
            result = subprocess.run([self.cli_path, "auth", "login"],
                                    cwd=self.work_dir,
                                    env=child_environment(os.environ, self.profile_dir, self.work_dir),
                                    shell=False)
            return {"status": "finished" if result.returncode == 0 else "failed"}
        except OSError:
            return {"status": "failed"}

    def poll(self, job_id: str) -> dict:
        with self._lock:
            entry = self._jobs.get(job_id)
            if entry is None:
                return {"status": "submission_uncertain", "error": "unknown_job"}
            return {key: value for key, value in entry.items() if key != "digest"}

    def status(self) -> dict:
        """Configuration only; this is not evidence of current login or quota."""
        with self._lock:
            return {"provider": "claude", "configured": self.enabled,
                    "ready": None, "verification": "not_verified",
                    "local_active": self._active, "local_limit": self.max_concurrency}

    def _update(self, job_id: str, **fields) -> None:
        with self._lock:
            if fields.get("status") == "succeeded" and "error" not in fields:
                self._jobs[job_id].pop("error", None)
            self._jobs[job_id].update(fields)

    def _worker(self, job_id: str, task: str) -> None:
        try:
            self._run_worker(job_id, task)
        except BaseException:
            # A worker failure can occur after submission; preserve uncertainty.
            self._update(job_id, status="submission_uncertain", error="worker_error")
        finally:
            with self._lock:
                self._active -= 1

    def _run_worker(self, job_id: str, task: str) -> None:
        try:
            self._update(job_id, status="running")
            env = child_environment(os.environ, self.profile_dir, self.work_dir)
            auth = subprocess.run([self.cli_path, "auth", "status"], stdin=subprocess.DEVNULL,
                                  capture_output=True, cwd=self.work_dir, env=env, timeout=20,
                                  shell=False, text=True, encoding="utf-8", errors="replace",
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if auth.returncode != 0 or not _subscription_login(json.loads(auth.stdout)):
                self._update(job_id, status="failed", error="subscription_login_unavailable")
                return
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self._update(job_id, status="failed", error="subscription_login_unavailable")
            return
        except Exception:
            self._update(job_id, status="failed", error="preflight_error")
            return

        # This is the first potential model submission. Never retry after here.
        command = [self.cli_path, "-p", "--model", self.model, "--effort", self.effort,
                   "--output-format", "json", "--no-session-persistence", "--tools", "", "--permission-prompts", "none",
                   "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        process_exit_confirmed = False
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, cwd=self.work_dir, env=env,
                                       shell=False, text=True, encoding="utf-8", errors="replace",
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                stdout, _stderr = process.communicate(input=task, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                # Timeout is only a progress warning. Keep waiting on this very
                # same process, without terminating it or re-submitting input.
                self._update(job_id, status="submission_uncertain", error="provider_slow")
                stdout, _stderr = process.communicate()
            # communicate() returning with a known return code establishes
            # that this local CLI process has exited, but not whether the
            # service received or completed the request. Keep those facts
            # separate so another task need not wait on a dead local process.
            process_exit_confirmed = type(process.returncode) is int
            # A structured CLI error is a known failure even if the process
            # exits nonzero. Never forward its body, which may contain secrets.
            # Other nonzero, malformed, or incomplete results stay uncertain.
            try:
                payload = json.loads(stdout)
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict) and payload.get("is_error") is True:
                self._update(job_id, status="failed" if process_exit_confirmed else "submission_uncertain",
                             error="provider_reported_error",
                             local_exit_confirmed=process_exit_confirmed)
                return
            if process.returncode != 0:
                self._update(job_id, status="submission_uncertain", error="provider_process_failed",
                             local_exit_confirmed=process_exit_confirmed)
                return
            if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
                self._update(job_id, status="submission_uncertain", error="invalid_provider_response",
                             local_exit_confirmed=process_exit_confirmed)
                return
            if payload.get("is_error") is not False:
                self._update(job_id, status="submission_uncertain", error="invalid_provider_response",
                             local_exit_confirmed=process_exit_confirmed)
                return
            self._update(job_id, status="succeeded", output=payload["result"],
                         model_verification="unverified", effort_verification="unverified",
                         local_exit_confirmed=process_exit_confirmed)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self._update(job_id, status="submission_uncertain", error="provider_result_unavailable",
                         local_exit_confirmed=process_exit_confirmed)
        except Exception:
            self._update(job_id, status="submission_uncertain", error="provider_result_unavailable",
                         local_exit_confirmed=process_exit_confirmed)
