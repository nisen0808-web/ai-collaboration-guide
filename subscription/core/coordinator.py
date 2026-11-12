"""Portable, credential-agnostic task ledger for a future provider adapter.

This module deliberately contains no provider integration or network access.
Adapters supplied by the caller must prove their own routing and authorization.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class InputError(ValueError):
    pass


# Shared limit for newly submitted Claude and Grok tasks. Existing requests
# created under the former character-based limit remain replayable.
MAX_TASK_UTF8_BYTES = 100_000


# Adapter diagnostics are selected from fixed public machine-readable codes.
# Never forward arbitrary CLI stderr or exception strings, which may contain
# user data, filesystem paths, credentials, or authentication information.
SUBMIT_REASONS = frozenset({
    "adapter_unavailable", "adapter_error", "login_required", "billing_unverified",
    "provider_busy", "invalid_task", "invalid_task_id", "cli_or_profile_unavailable",
    "task_id_conflict", "worker_unavailable", "already_submitted",
    "state_unavailable", "state_write_failed", "dispatch_interrupted", "session_evidence_unavailable",
    "session_evidence_model_invalid", "worker_start_unconfirmed",
})
POLL_ERRORS = frozenset({
    "login_required", "provider_busy", "provider_error", "adapter_error", "timed_out",
    "cancelled", "process_exit_unconfirmed", "invalid_model", "result_unavailable",
    "subscription_unavailable", "unknown_job", "subscription_login_unavailable",
    "preflight_error", "provider_process_failed", "invalid_provider_response",
    "provider_reported_error", "provider_slow", "provider_result_unavailable",
    "inspection_unavailable", "unexpected_or_unknown_extensions", "process_still_running",
    "cli_failed", "empty_result", "execution_unverified", "job_state_unavailable",
    "worker_error", "worker_unavailable", "worker_setup_failed", "input_too_large",
    "session_evidence_unavailable",
    "worker_identity_unconfirmed", "worker_start_unconfirmed",
    "worker_request_invalid", "dispatch_interrupted", "state_write_failed",
    "operator_released",
})
# Startup diagnostics supplied by the trusted entry point, never by a provider.
SETUP_ISSUES = frozenset({
    "cli_not_found", "invalid_configuration", "local_state_unavailable",
    "session_evidence_model_invalid",
})
INSPECTION_REASONS = frozenset({
    "inspection_format_unknown", "extensions_present_or_unknown",
    "skills_present_or_unknown", "agents_present_or_unknown",
    "subagents_disable_unverified", "agent_state_conflict_or_unknown",
    "metadata_format_unknown", "unknown_nonempty_container",
})


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _proved_grok_prelaunch_abort(adapter: object, task_id: str, job_id: str) -> bool:
    """Trust only the built-in adapter's bound, versioned local ledger evidence."""
    try:
        from grok.durable_grok import DurableGrokAdapter
        if type(adapter) is not DurableGrokAdapter:
            return False
        # Do not trust a generic adapter flag, duck-typed method, subclass, or
        # instance-level method override as proof about a provider request.
        return DurableGrokAdapter.proves_prelaunch_aborted(adapter, task_id, job_id) is True
    except (ImportError, OSError, sqlite3.Error, ValueError, TypeError):
        return False


def _result_unavailable(current: dict, reason: str) -> dict:
    """Distinguish a lost successful result from unknown execution."""
    if current.get("status") == "succeeded" or current.get("execution_status") == "succeeded":
        return {**current, "status": "result_unavailable", "execution_status": "succeeded", "poll_error": reason}
    return {**current, "status": "submission_uncertain", "poll_error": reason}


def _grok_client_quality_projection(quality: dict, *, complete_success: bool) -> dict:
    """Expose only consistent client-record evidence, never infer server attestation.

    This is an adapter-reported projection, not independent verification by
    the coordinator. A missing, contradictory or unsafe observation must not
    promote a task's quality or leak arbitrary metadata.
    """
    safe = {"verified": False, "server_effort_verified": False, "client_record_verified": False,
            "client_evidence_status": "unavailable", "evidence_source": "not_verified",
            "client_model_matches_requested": None, "client_effort_matches_requested": None}
    if not complete_success or quality.get("verified") is not False or quality.get("server_effort_verified") is not False:
        return safe
    if quality.get("evidence_source") != "official_grok_client_session":
        return safe
    status = quality.get("client_evidence_status")
    if status == "pending":
        pending_fields = ("client_model_matches_requested", "client_effort_matches_requested",
                          "observed_model", "observed_effort")
        if all(key in quality and quality[key] is None for key in pending_fields):
            safe.update(client_evidence_status="pending", evidence_source="official_grok_client_session",
                        observed_model=None, observed_effort=None)
        return safe
    model_match = quality.get("client_model_matches_requested")
    effort_match = quality.get("client_effort_matches_requested")
    model = quality.get("observed_model")
    effort = quality.get("observed_effort")
    safe_model = (isinstance(model, str) and
                  re.fullmatch(r"grok-[A-Za-z0-9][A-Za-z0-9._-]{0,89}", model) is not None)
    # A mismatched model's name may deliberately be withheld to prevent
    # leaking arbitrary untrusted metadata. A claimed match still requires a
    # safe observed name, including when only effort mismatched.
    withheld_mismatch = status == "mismatch" and model_match is False and model is None
    if (status not in ("matched", "mismatch") or type(model_match) is not bool or
            type(effort_match) is not bool or not (safe_model or withheld_mismatch) or
            effort not in ("low", "medium", "high", "xhigh")):
        return safe
    # A valid-looking model string still must not contain a recognized token
    # format. Raw session data, paths and diagnostics are never projected.
    if model is not None and re.search(r"(?:sk-(?:ant|proj)-[A-Za-z0-9_-]{10,}|xai-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{15,})", model, re.I):
        return safe
    if ((status == "matched" and not (model_match and effort_match)) or
            (status == "mismatch" and model_match and effort_match)):
        return safe
    safe.update(client_evidence_status=status, client_record_verified=status == "matched",
                evidence_source="official_grok_client_session",
                client_model_matches_requested=model_match, client_effort_matches_requested=effort_match,
                observed_model=model, observed_effort=effort)
    return safe


class Coordinator:
    """At-most-once dispatch, with conservative recovery from unknown outcomes.

    A reservation is committed before calling the adapter. A crash or exception
    after that commit can leave an unknown outcome; replay never calls submit.
    The adapter must not signal not_submitted if it might have sent a request.
    """

    def __init__(self, db_path: str | Path, adapters: dict[str, Any] | None = None, *, status_timeout: float = 1.5,
                 unconfigured_providers: tuple[str, ...] = (),
                 setup_issues: dict[str, str] | None = None):
        self.path = str(db_path)
        self.adapters = dict(adapters or {})
        self.status_timeout = max(0.0, min(float(status_timeout), 5.0))
        # Trusted startup configuration, not a live provider observation.
        # In particular, a read-only historical ledger must remain visibly
        # disabled even if its status probe blocks on an existing DB lock.
        self._unconfigured_providers = frozenset(unconfigured_providers) & {"claude", "grok"}
        # Why a provider did not start: one fixed code each, set at startup.
        self._setup_issues = {name: code for name, code in dict(setup_issues or {}).items()
                              if name in ("claude", "grok") and isinstance(code, str) and code in SETUP_ISSUES}
        self._status_slots = {name: threading.BoundedSemaphore(1) for name in ("claude", "grok")}
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialise(self) -> None:
        with self._db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS requests (
                request_key TEXT PRIMARY KEY,
                digest TEXT NOT NULL,
                task_id TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                job_id TEXT,
                reason TEXT
            )""")

    def status(self) -> dict:
        # Status is separately bounded per provider; a hung provider cannot
        # prevent a different provider or the coordinator from proceeding.
        values = {name: {"configured": name in self.adapters and name not in self._unconfigured_providers,
                         "ready": None if name in self.adapters and name not in self._unconfigured_providers else False,
                         "verification": "not_verified"}
                  for name in ("claude", "grok")}
        for name, code in self._setup_issues.items():
            values[name]["setup_issue"] = code
        finished = {}
        observations = {}
        for name in ("claude", "grok"):
            probe = getattr(self.adapters.get(name), "status", None)
            slot = self._status_slots[name]
            if not callable(probe) or not slot.acquire(blocking=False):
                continue
            event = finished[name] = threading.Event()
            def run(current=name, fn=probe, sem=slot, done=event):
                try:
                    value = fn()
                    if isinstance(value, dict):
                        # Configuration is an adapter observation, not proof
                        # of authentication, availability, or model execution.
                        safe = {key: value[key] for key in ("configured", "ready", "loggedIn") if type(value.get(key)) is bool}
                        method = value.get("authMethod")
                        if method in ("oauth", "claude.ai", "subscription", "cached_token", "api_key", "unknown"):
                            safe["authMethod"] = method
                        for key in ("local_active", "local_limit", "local_active_jobs", "local_max_concurrency", "local_available_slots", "local_observed_inflight", "local_uncertain", "local_finished_unresolved"):
                            count = value.get(key)
                            if type(count) is int and 0 <= count <= 1000:
                                safe[key] = count
                        if value.get("official_quota_verified") is False:
                            safe["official_quota_verified"] = False
                        safe["reported_by_adapter"] = True
                        observations[current] = safe
                except Exception:
                    pass
                finally:
                    sem.release()
                    done.set()
            threading.Thread(target=run, daemon=True).start()
        deadline = time.monotonic() + self.status_timeout
        for event in finished.values():
            event.wait(max(0.0, deadline - time.monotonic()))
        for name, event in finished.items():
            if event.is_set() and name in observations:
                values[name].update(observations[name])
            if name in self._unconfigured_providers:
                values[name]["configured"] = False
                values[name]["ready"] = False
                values[name].pop("loggedIn", None)
        return {"providers": values}

    def get(self, request_key: str) -> dict:
        if not isinstance(request_key, str) or not request_key or len(request_key) > 200:
            raise InputError("invalid request_key")
        with self._db() as conn:
            row = conn.execute("SELECT task_id, provider, status, job_id, reason FROM requests WHERE request_key = ?", (request_key,)).fetchone()
        if row is None:
            return {"status": "not_found"}
        result = dict(row)
        if result.get("status") == "incomplete" and result.get("reason") == "truncated_output":
            result.update(execution_status="succeeded", truncated=True)
        # 'dispatching' could be a live dispatch or a dead process. Either way,
        # it must not be retried automatically or represented as not_submitted.
        if result["status"] == "dispatching":
            result["status"] = "submission_uncertain"
        return result

    def submit(self, provider: str, task: str, request_key: str) -> dict:
        if provider not in ("claude", "grok"):
            raise InputError("unsupported provider")
        if not isinstance(task, str) or not task.strip() or len(task) > 100_000:
            raise InputError("invalid task")
        if not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 200:
            raise InputError("invalid request_key")
        try:
            task_bytes = task.encode("utf-8", errors="strict")
            digest = hashlib.sha256(_canonical({"provider": provider, "task": task}).encode("utf-8")).hexdigest()
        except UnicodeError:
            raise InputError("invalid task") from None
        task_id = uuid.uuid4().hex
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT * FROM requests WHERE request_key = ?", (request_key,)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise InputError("request_key already used for different input")
                replay = dict(old)
                replay.pop("digest", None)
                replay["replayed"] = True
                if replay["status"] == "dispatching":
                    replay["status"] = "submission_uncertain"
                return replay
            # The shared byte limit is checked only after matching an existing
            # request key, so previously accepted long UTF-8 tasks can still
            # replay their original receipt without starting new work.
            if len(task_bytes) > MAX_TASK_UTF8_BYTES:
                raise InputError("task exceeds UTF-8 byte limit")
            conn.execute("INSERT INTO requests VALUES (?, ?, ?, ?, 'dispatching', NULL, NULL)",
                         (request_key, digest, task_id, provider))
        adapter = self.adapters.get(provider)
        if adapter is None:
            result = {"status": "not_submitted", "reason": "adapter_unavailable"}
        else:
            try:
                # The adapter is called only after the reservation commits.
                submit = getattr(adapter, "submit", adapter)
                result = submit(task=task, task_id=task_id)
                if not isinstance(result, dict) or result.get("status") not in ("queued", "running", "not_submitted", "submission_uncertain"):
                    raise ValueError("unrecognized adapter response")
                if result["status"] in ("queued", "running") and (not isinstance(result.get("job_id"), str) or not result["job_id"]):
                    raise ValueError("missing adapter job id")
                if result.get("job_id") is not None and (not isinstance(result["job_id"], str) or not result["job_id"] or len(result["job_id"]) > 256):
                    raise ValueError("invalid adapter job id")
            except Exception:
                # Do not persist exception text: it could contain credentials.
                result = {"status": "submission_uncertain", "reason": "adapter_error"}
        status = result["status"]
        if status == "not_submitted" and (result.get("job_id") or result.get("reason") == "already_submitted"):
            # Some adapters report that this local attempt did not start
            # because an existing job already owns its ID. That does not prove
            # the earlier request was never submitted. Keep the handle and
            # ask only about the original job; do not automatically re-run it.
            status = "submission_uncertain"
        # Even an uncertain submission may carry a legitimate job handle;
        # keep it for read-only reconciliation, never for automatic resubmission.
        job_id = result.get("job_id") if status in ("queued", "running", "submission_uncertain") else None
        # Limit stored adapter messages to fixed machine-readable categories.
        reason = result.get("reason")
        if not isinstance(reason, str) or reason not in SUBMIT_REASONS:
            reason = None
        with self._db() as conn:
            conn.execute("UPDATE requests SET status = ?, job_id = ?, reason = ? WHERE request_key = ? AND status = 'dispatching'",
                         (status, job_id, reason, request_key))
        return {"task_id": task_id, "provider": provider, "status": status, "job_id": job_id, "reason": reason, "replayed": False}

    def result(self, provider: str, request_key: str) -> dict:
        """Query the original job without resubmitting and return available text.

        Provider adapters must retain output and implement repeatable, read-only
        poll(job_id). The ledger stores status and job id, never output text.
        """
        if provider not in ("claude", "grok"):
            raise InputError("unsupported provider")
        current = self.get(request_key)
        if current.get("status") == "not_found":
            return current
        if current.get("provider") != provider:
            raise InputError("provider does not match request")
        if current.get("status") == "not_submitted":
            return current
        adapter = self.adapters.get(provider)
        if not current.get("job_id") and current.get("status") == "submission_uncertain":
            # An adapter can have durably recorded an original job after the
            # coordinator reserved task_id but before it received job_id.
            # Resolve only through the adapter's read-only lookup; never call
            # submit or infer that a missing record means no dispatch occurred.
            find_job = getattr(adapter, "find_job", None) if adapter is not None else None
            if callable(find_job):
                try:
                    recovered = find_job(current["task_id"])
                except Exception:
                    recovered = None
                if isinstance(recovered, str) and recovered and len(recovered) <= 256:
                    with self._db() as conn:
                        conn.execute("UPDATE requests SET job_id = ?, status = CASE WHEN status = 'dispatching' "
                                     "THEN 'submission_uncertain' ELSE status END WHERE request_key = ? "
                                     "AND provider = ? AND task_id = ? AND job_id IS NULL "
                                     "AND status IN ('dispatching', 'submission_uncertain')",
                                     (recovered, request_key, provider, current["task_id"]))
                    # A concurrent submit/poll may have updated the record;
                    # use the ledger's actual value, never overwrite it here.
                    current = self.get(request_key)
        if not current.get("job_id"):
            return current
        poll = getattr(adapter, "poll", None) if adapter is not None else None
        if not callable(poll):
            return _result_unavailable(current, "adapter_unavailable")
        try:
            response = poll(current["job_id"])
            valid = {"queued", "running", "succeeded", "failed", "cancelled", "timed_out", "blocked", "submission_uncertain", "not_submitted", "incomplete", "refused"}
            if not isinstance(response, dict) or response.get("status") not in valid:
                raise ValueError("invalid response")
            observed = response["status"]
            # A generic later not-submitted response cannot prove that the
            # original receipt did not represent a real provider request.
            if observed == "not_submitted":
                if provider == "grok" and _proved_grok_prelaunch_abort(adapter, current["task_id"], current["job_id"]):
                    observed = "not_submitted"
                else:
                    observed = "submission_uncertain"
            if observed == "succeeded" and not isinstance(response.get("output"), str):
                return _result_unavailable(current, "incomplete_result")
            if "output" in response and not isinstance(response["output"], str):
                raise ValueError("invalid output")
            if "truncated" in response and type(response["truncated"]) is not bool:
                raise ValueError("invalid truncation flag")
            truncated_success = observed == "succeeded" and response.get("truncated") is True
            if truncated_success:
                # The process may have completed, but its body was truncated;
                # record it as incomplete and never present it as final.
                observed = "incomplete"
        except Exception:
            # Never expose exception text (which might contain credentials).
            return _result_unavailable(current, "adapter_error")
        terminals = {"succeeded", "failed", "cancelled", "timed_out", "blocked", "incomplete", "refused", "not_submitted"}
        with self._db() as conn:
            # A late stale poll must not overwrite any terminal result.
            conn.execute("UPDATE requests SET status = ?, reason = CASE WHEN ? THEN 'truncated_output' "
                         "WHEN ? THEN 'state_write_failed' ELSE reason END "
                         "WHERE request_key = ? AND provider = ? AND task_id = ? AND job_id = ? "
                         "AND status IN ('queued', 'running', 'submission_uncertain')",
                         (observed, truncated_success, observed == "not_submitted", request_key,
                          provider, current["task_id"], current["job_id"]))
            persisted = conn.execute("SELECT status, reason FROM requests WHERE request_key = ?", (request_key,)).fetchone()
            actual = persisted["status"]
            was_truncated = actual == "incomplete" and persisted["reason"] == "truncated_output"
        if actual != observed and actual in terminals:
            if actual == "succeeded":
                return _result_unavailable({**current, "status": actual}, "provider_state_inconsistent")
            if was_truncated:
                return _result_unavailable({**current, "status": actual, "execution_status": "succeeded", "truncated": True}, "provider_state_inconsistent")
            return {**current, "status": actual, "poll_error": "provider_state_inconsistent"}
        result = {**current, "status": actual}
        if was_truncated:
            result.update(reason="truncated_output", execution_status="succeeded", truncated=True)
        elif response.get("truncated") is True:
            result["truncated"] = True
        if "output" in response:
            result["output"] = response["output"]
            result["output_kind"] = "final" if actual == "succeeded" else "partial"
        if type(response.get("local_exit_confirmed")) is bool:
            # Local exit is not proof of the remote result or non-submission.
            result["local_exit_confirmed"] = (response["local_exit_confirmed"] and
                response.get("error") not in {"operator_released", "process_exit_unconfirmed",
                                              "process_still_running", "worker_identity_unconfirmed"} and
                response.get("release_basis") != "operator_confirmed")
        if response.get("error") == "operator_released" and response.get("release_basis") == "operator_confirmed":
            # A user assertion must not be represented as observed process exit.
            result["release_basis"] = "operator_confirmed"
        if type(response.get("inspection_metadata_unverified")) is bool:
            result["inspection_metadata_unverified"] = response["inspection_metadata_unverified"]
        error = response.get("error")
        # Only accept known fixed codes, never arbitrary provider diagnostics.
        if isinstance(error, str) and error in POLL_ERRORS:
            result["error"] = error
        inspection_reason = response.get("inspection_reason")
        if (provider == "grok" and actual == "failed" and
                error == "unexpected_or_unknown_extensions" and
                isinstance(inspection_reason, str) and inspection_reason in INSPECTION_REASONS):
            result["inspection_reason"] = inspection_reason
        # Raw provider metadata can contain paths or credentials. Return a
        # deliberately small set of primitive verification fields only.
        quality = response.get("quality_verification")
        if isinstance(quality, dict):
            flags = {"verified"}
            strings = {"actual_model", "actual_effort", "evidence_source"}
            safe = {key: quality[key] for key in flags if type(quality.get(key)) is bool}
            safe.update({key: quality[key] for key in strings if isinstance(quality.get(key), str)
                         and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", quality[key])})
            if provider == "grok":
                safe = _grok_client_quality_projection(
                    quality, complete_success=actual == "succeeded" and response.get("truncated") is False)
            if provider == "claude":
                # The current CLI JSON does not attest the effort used for
                # the response; adapter claims cannot promote it to verified.
                safe["verified"] = False
                safe["effort_verified"] = False
                safe.pop("actual_effort", None)
                safe.pop("actual_model", None)
                safe["model_usage_matches_requested"] = None
                # Never pass through an untrusted or contradictory source.
                safe["evidence_source"] = "not_verified"
                client_status = quality.get("client_evidence_status")
                cleanup = quality.get("session_cleanup")
                if client_status in ("matched", "unavailable", "disabled", "pending") and cleanup in ("deletion_requested", "retained", "not_attempted", "unconfirmed"):
                    if client_status == "matched" and (quality.get("client_model_matches_requested") is not True
                                                       or quality.get("client_effort_matches_requested") is not True
                                                       or cleanup != "deletion_requested"):
                        client_status = "unavailable"
                    safe["client_evidence_status"] = client_status
                    safe["session_cleanup"] = cleanup
                    for key in ("client_model_matches_requested", "client_effort_matches_requested"):
                        safe[key] = True if client_status == "matched" and quality.get(key) is True else None
                    safe["server_effort_verified"] = False
            if provider == "claude" and "model_usage_matches_requested" in quality:
                model = quality.get("recorded_model")
                valid_model_evidence = (quality["model_usage_matches_requested"] is True
                        and quality.get("evidence_source") == "cli_result_model_usage"
                        and isinstance(model, str) and bool(re.fullmatch(r"claude-[A-Za-z0-9][A-Za-z0-9._-]{0,89}", model)))
                safe["model_usage_matches_requested"] = True if valid_model_evidence else None
                if valid_model_evidence:
                    safe["recorded_model"] = model
                    safe["evidence_source"] = "cli_result_model_usage"
            safe["reported_by_adapter"] = True
            safe["independently_verified_by_coordinator"] = False
            result["quality_verification"] = safe
        elif actual == "succeeded":
            result["quality_verification"] = {"verified": False, "evidence_source": "not_verified"}
            if provider == "claude":
                result["quality_verification"].update(model_usage_matches_requested=None,
                                                       effort_verified=False)
        return result
