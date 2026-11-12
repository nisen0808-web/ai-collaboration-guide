"""Candidate adapter for the user's own installed Claude Code CLI.

No API keys are requested or read. Users sign in with the official client and
are responsible for its account and billing settings. Narrow real-client
samples and their limitations are documented separately in EVIDENCE.md.
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
import uuid

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_EVIDENCE_MODEL = re.compile(r"claude-[A-Za-z0-9][A-Za-z0-9._-]{0,89}\Z")
_MODEL_SOURCE = "cli_result_model_usage"
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
_NETWORK_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                      "http_proxy", "https_proxy", "all_proxy", "no_proxy",
                      "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR")
_INHERIT = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "NO_COLOR", *_NETWORK_ENV_NAMES})
_CLAUDE_ACCOUNT = frozenset({"claude.ai", "claude_ai", "claudeai", "claude.ai oauth", "claude.ai/oauth"})
_PLANS = frozenset({"free", "pro", "max", "ultra", "team", "enterprise", "claude_pro", "claude_max",
                    "default_claude_pro", "default_claude_max_5x", "default_claude_max_20x", "max5x", "max20x"})


def valid_session_evidence_model(model: object) -> bool:
    """Require a syntactically explicit Claude ID, not a bare alias."""
    return isinstance(model, str) and bool(_EVIDENCE_MODEL.fullmatch(model))


def valid_model(model: object) -> bool:
    """The same syntax rule the adapter constructor enforces."""
    return isinstance(model, str) and bool(_MODEL.fullmatch(model))


def valid_effort(effort: object) -> bool:
    """The same effort set the adapter constructor enforces."""
    return isinstance(effort, str) and effort in _EFFORTS


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
    # Keep the profile's own customizations out of a collaboration task:
    # documented safe mode skips CLAUDE.md, skills, plugins, hooks, MCP
    # servers and auto memory while login keeps working. The two narrower
    # switches cover memory files on clients that predate safe mode. These
    # are requests to the official client, not proof of what it loaded, and
    # managed policy can still apply.
    env.update({"CLAUDE_CODE_SAFE_MODE": "1", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
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


def _model_quality(payload: object, requested_model: str) -> dict:
    """Project exact single-model CLI evidence; do not infer effort or aliases.

    Only call on this invocation's successful, confirmed-exit JSON result.
    The selected CLI is trusted; this does not authenticate the executable.
    """
    quality = {"verified": False, "model_usage_matches_requested": None,
               "effort_verified": False, "evidence_source": "not_verified"}
    if not isinstance(requested_model, str) or not _EVIDENCE_MODEL.fullmatch(requested_model):
        return quality
    if not isinstance(payload, dict) or payload.get("type") != "result" or payload.get("subtype") != "success":
        return quality
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict) or len(usage) != 1 or requested_model not in usage:
        return quality
    model_usage = usage[requested_model]
    if not isinstance(model_usage, dict):
        return quality
    input_tokens, output_tokens = model_usage.get("inputTokens"), model_usage.get("outputTokens")
    if (type(input_tokens) is not int or type(output_tokens) is not int
            or input_tokens < 0 or output_tokens < 0 or input_tokens + output_tokens <= 0):
        return quality
    canonical = model_usage.get("canonicalModel", requested_model)
    if canonical != requested_model:
        return quality
    quality.update(model_usage_matches_requested=True, recorded_model=requested_model, evidence_source=_MODEL_SOURCE)
    return quality


def _unambiguous_json(text: str) -> object:
    """Reject duplicate object members in official CLI JSON responses."""
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("ambiguous JSON")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique_pairs)


def _stored_model_quality(value: object, requested_model: str | None = None) -> dict:
    """Validate the private durable file's narrow evidence projection."""
    unknown = {"verified": False, "model_usage_matches_requested": None,
               "effort_verified": False, "evidence_source": "not_verified"}
    if not isinstance(value, dict) or value.get("model_usage_matches_requested") is not True:
        return unknown
    model = value.get("recorded_model")
    if (not isinstance(model, str) or not _EVIDENCE_MODEL.fullmatch(model)
            or value.get("evidence_source") != _MODEL_SOURCE
            or (requested_model is not None and model != requested_model)):
        return unknown
    return {"verified": False, "model_usage_matches_requested": True, "effort_verified": False,
            "recorded_model": model, "evidence_source": _MODEL_SOURCE}


def _stored_session_quality(value: object) -> dict:
    """Allow only non-identifying, fixed client-record observations."""
    unknown = {"client_model_matches_requested": None, "client_effort_matches_requested": None,
               "client_evidence_status": "unavailable", "session_cleanup": "not_attempted"}
    if not isinstance(value, dict):
        return unknown
    status, cleanup = value.get("client_evidence_status"), value.get("session_cleanup")
    if status not in ("matched", "unavailable", "disabled", "pending") or cleanup not in ("deletion_requested", "retained", "not_attempted", "unconfirmed"):
        return unknown
    if status == "matched" and (value.get("client_model_matches_requested") is not True
                                or value.get("client_effort_matches_requested") is not True
                                or cleanup != "deletion_requested"):
        return {**unknown, "session_cleanup": cleanup}
    return {"client_model_matches_requested": True if value.get("client_model_matches_requested") is True and status == "matched" else None,
            "client_effort_matches_requested": True if value.get("client_effort_matches_requested") is True and status == "matched" else None,
            "client_evidence_status": status, "session_cleanup": cleanup}


class ClaudeAdapter:
    """In-process asynchronous text worker; no durable replay after restart.

    The coordinator must persist task IDs and never retry a request whose
    submission is unknown. The user must opt in to enable this candidate.
    Existing CLI profile contents are neither copied nor read by this code;
    the official CLI may access its own login in the selected profile.
    """
    def __init__(self, cli_path, profile_dir, work_dir, model, effort, *, enabled=False,
                 timeout_seconds=900, max_concurrency=2, session_evidence=False):
        self.cli_path = _absolute(cli_path)
        self.profile_dir = _absolute(profile_dir)
        self.work_dir = _absolute(work_dir)
        if not valid_model(model):
            raise ValueError("invalid model")
        if not valid_effort(effort):
            raise ValueError("invalid effort")
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 3600:
            raise ValueError("invalid timeout")
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 8:
            raise ValueError("invalid max_concurrency")
        if type(session_evidence) is not bool:
            raise ValueError("session_evidence must be boolean")
        if session_evidence and not valid_session_evidence_model(model):
            raise ValueError("session evidence requires an explicit claude-* model ID")
        self.model, self.effort = model, effort
        self.enabled, self.timeout = enabled, timeout_seconds
        self.session_evidence = session_evidence
        # This is a local safety limit, not a provider quota or plan limit.
        self.max_concurrency = max_concurrency
        self._active = 0
        self._jobs = {}
        self._lock = threading.Lock()

    def submit(self, task: str, task_id: str, *, _session_id: str | None = None) -> dict:
        if not self.enabled:
            return {"status": "not_submitted", "reason": "adapter_unavailable"}
        if not isinstance(task, str) or not task.strip() or len(task) > 100000:
            return {"status": "not_submitted", "reason": "invalid_task"}
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            return {"status": "not_submitted", "reason": "invalid_task_id"}
        if _session_id is not None:
            try:
                if not self.session_evidence or not isinstance(_session_id, str) or str(uuid.UUID(_session_id)) != _session_id:
                    raise ValueError("invalid session")
            except ValueError:
                return {"status": "not_submitted", "reason": "session_evidence_unavailable"}
        digest = hashlib.sha256(task.encode("utf-8")).digest()
        with self._lock:
            if task_id in self._jobs:
                if self._jobs[task_id]["digest"] != digest:
                    return {"status": "not_submitted", "reason": "task_id_conflict"}
                prior = self._jobs[task_id].get("status")
                if prior in ("queued", "running", "submission_uncertain"):
                    return {"status": prior, "job_id": task_id, "replayed": True}
                if prior in ("succeeded", "failed"):
                    return {"status": "not_submitted", "reason": "already_submitted",
                            "job_id": task_id, "replayed": True, "existing_status": prior}
                return {"status": "submission_uncertain", "job_id": task_id, "replayed": True}
            if self._active >= self.max_concurrency:
                return {"status": "not_submitted", "reason": "provider_busy"}
            session_id = None
            if self.session_evidence:
                try:
                    session_id = _session_id or str(uuid.uuid4())
                except Exception:
                    # Nothing has been reserved or launched yet.
                    return {"status": "not_submitted", "reason": "session_evidence_unavailable"}
            self._active += 1
            self._jobs[task_id] = {"status": "queued", "digest": digest}
            if self.session_evidence:
                self._jobs[task_id]["_session_id"] = session_id
                self._jobs[task_id]["session_quality"] = _stored_session_quality(None)
                self._jobs[task_id]["quality_verification"] = {**_stored_model_quality(None),
                                                               **_stored_session_quality(None)}
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
            return {key: value for key, value in entry.items() if key not in ("digest", "_session_id")}

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
                job = self._jobs[job_id]
                known_prelaunch_failure = (job.get("status") == "failed" and job.get("error") in
                                           {"subscription_login_unavailable", "preflight_error", "session_evidence_unavailable"})
                # Never free a slot merely because the local worker stopped:
                # a child may still exist after a spawn or communicate error.
                if known_prelaunch_failure or job.get("local_exit_confirmed") is True:
                    self._active -= 1

    def _run_worker(self, job_id: str, task: str) -> None:
        try:
            self._update(job_id, status="running")
            env = child_environment(os.environ, self.profile_dir, self.work_dir)
            auth = subprocess.run([self.cli_path, "auth", "status"], stdin=subprocess.DEVNULL,
                                  capture_output=True, cwd=self.work_dir, env=env, timeout=20,
                                  shell=False, text=True, encoding="utf-8", errors="replace",
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if auth.returncode != 0 or not _subscription_login(_unambiguous_json(auth.stdout)):
                self._update(job_id, status="failed", error="subscription_login_unavailable")
                return
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self._update(job_id, status="failed", error="subscription_login_unavailable")
            return
        except Exception:
            self._update(job_id, status="failed", error="preflight_error")
            return

        session = None
        if self.session_evidence:
            try:
                if __package__:
                    from .session_evidence import prepare_session_evidence
                else:
                    from session_evidence import prepare_session_evidence
                with self._lock:
                    session = self._jobs[job_id].get("_session_id")
                if prepare_session_evidence(self.profile_dir, self.work_dir, session, enabled=True) is not True:
                    raise ValueError("session evidence unavailable")
            except Exception:
                self._update(job_id, status="failed", error="session_evidence_unavailable")
                return

        # This is the first potential model submission. Never retry after here.
        command = [self.cli_path, "-p", "--model", self.model, "--effort", self.effort,
                   "--output-format", "json", "--tools", "", "--permission-prompts", "none",
                   "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        command.extend(["--session-id", session] if session else ["--no-session-persistence"])
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
                payload = _unambiguous_json(stdout)
            except (TypeError, ValueError):
                payload = None
            session_quality = None
            if session:
                session_quality = _stored_session_quality(None)
                # This in-memory adapter cannot durably save the answer. It
                # never reads or deletes the session. A durable wrapper may
                # collect evidence only after it has committed the result.
                self._update(job_id, session_quality=session_quality)
                self._update(job_id, quality_verification={**_stored_model_quality(None), **session_quality})
            if isinstance(payload, dict) and payload.get("is_error") is True:
                error_subtypes = {"error_max_turns", "error_during_execution", "error_max_budget_usd",
                                  "error_max_structured_output_retries"}
                subtype = payload.get("subtype")
                structured_error = (payload.get("type") in (None, "result")
                                    and (subtype is None or
                                         (payload.get("type") == "result" and isinstance(subtype, str)
                                          and subtype in error_subtypes)))
                self._update(job_id, status="failed" if process_exit_confirmed and structured_error else "submission_uncertain",
                             error="provider_reported_error" if structured_error else "invalid_provider_response",
                             local_exit_confirmed=process_exit_confirmed)
                return
            if not process_exit_confirmed or process.returncode != 0:
                self._update(job_id, status="submission_uncertain", error="provider_process_failed",
                             local_exit_confirmed=process_exit_confirmed)
                return
            if (not isinstance(payload, dict) or payload.get("type") != "result"
                    or payload.get("subtype") != "success" or payload.get("is_error") is not False
                    or not isinstance(payload.get("result"), str)):
                self._update(job_id, status="submission_uncertain", error="invalid_provider_response",
                             local_exit_confirmed=process_exit_confirmed)
                return
            if session and payload.get("session_id") != session:
                self._update(job_id, status="submission_uncertain", error="invalid_provider_response",
                             local_exit_confirmed=process_exit_confirmed)
                return
            if session:
                self._update(job_id, session_bound=True)
            quality = _model_quality(payload, self.model)
            if session_quality is not None:
                quality.update(session_quality)
            self._update(job_id, status="succeeded", output=payload["result"],
                         model_verification="unverified",
                         effort_verification="unverified", quality_verification=quality,
                         local_exit_confirmed=process_exit_confirmed)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self._update(job_id, status="submission_uncertain", error="provider_result_unavailable",
                         local_exit_confirmed=process_exit_confirmed)
        except Exception:
            self._update(job_id, status="submission_uncertain", error="provider_result_unavailable",
                         local_exit_confirmed=process_exit_confirmed)
