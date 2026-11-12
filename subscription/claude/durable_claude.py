"""Unpublished candidate: detached Claude jobs in a user-selected directory.

Never logs or persists prompts; one private result file is retained until the
user removes it. The caller must provide a private directory with appropriate
OS permissions; this module does not claim to verify Windows ACLs. Jobs with
uncertain submission are never automatically re-launched.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

if __package__:
    from .claude_adapter import ClaudeAdapter, child_environment, _absolute, _stored_model_quality, _stored_session_quality
else:
    # Detached execution uses this file as a script; Python then adds its
    # containing directory rather than the package parent to sys.path.
    from claude_adapter import ClaudeAdapter, child_environment, _absolute, _stored_model_quality, _stored_session_quality

_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_WORKER_SCRIPT = str(Path(__file__).resolve())
_MAX_INPUT_BYTES = 900_000
_ACTIVE = ("reserved", "queued", "running", "submission_uncertain")


@contextmanager
def _connect(db: Path):
    connection = sqlite3.connect(str(db), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _atomic_json(path: Path, value: dict) -> None:
    data = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    # Exclusive creation and replacement avoid reading or following a prior
    # result path. The state directory must itself be private and trusted.
    temp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


class DurableClaudeAdapter:
    """Locally bounded, detached worker candidate; never retry uncertain jobs."""
    def __init__(self, cli_path, profile_dir, work_dir, state_dir, model, effort,
                 *, enabled=False, timeout_seconds=900, max_concurrency=2, session_evidence=False):
        self.cli_path = _absolute(cli_path)
        self.profile_dir = _absolute(profile_dir)
        self.work_dir = _absolute(work_dir)
        self.state_dir = Path(_absolute(state_dir))
        if self.state_dir.is_symlink() or not self.state_dir.is_dir():
            raise ValueError("state directory must be an existing private directory")
        attrs = self.state_dir.lstat()
        if getattr(attrs, "st_file_attributes", 0) & 0x400:
            raise ValueError("state directory cannot be a reparse point")
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 8:
            raise ValueError("invalid max_concurrency")
        # Delegate model, effort, timeout, and enabled validation to the same
        # code that eventually starts the CLI, but do not call it here.
        self._adapter = ClaudeAdapter(self.cli_path, self.profile_dir, self.work_dir,
                                      model, effort, enabled=enabled,
                                      timeout_seconds=timeout_seconds, session_evidence=session_evidence)
        self.model, self.effort, self.enabled = model, effort, enabled
        self.session_evidence = session_evidence
        self.timeout, self.max_concurrency = timeout_seconds, max_concurrency
        self.db = self.state_dir / "jobs.sqlite3"
        with _connect(self.db) as con:
            con.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                        "status TEXT NOT NULL, error TEXT, heartbeat REAL, output_file TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS session_evidence_intents (job_id TEXT PRIMARY KEY, "
                        "session_id TEXT NOT NULL, quality_json TEXT)")

    def login(self) -> dict:
        return self._adapter.login()

    def status(self) -> dict:
        with _connect(self.db) as con:
            rows = con.execute("SELECT status,heartbeat FROM jobs WHERE status IN (?,?,?,?)", _ACTIVE).fetchall()
            finished_unresolved = con.execute("SELECT COUNT(*) FROM jobs WHERE status='finished_uncertain'").fetchone()[0]
        # A recent heartbeat is a local observation, not proof that a worker
        # or its provider child is alive. Uncertain rows continue to own slots.
        now = time.time()
        observed = sum(row[0] in ("queued", "running") and row[1] is not None
                       and 0 <= now - row[1] <= 10 for row in rows)
        n = len(rows)
        return {"provider": "claude", "configured": self.enabled, "ready": None,
                "verification": "not_verified", "local_active": n,
                "local_observed_inflight": observed, "local_uncertain": n - observed,
                "local_finished_unresolved": finished_unresolved,
                "local_limit": self.max_concurrency}

    def find_job(self, task_id: str) -> str | None:
        """Return an existing job handle without launching or retrying work."""
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            return None
        try:
            with _connect(self.db) as con:
                row = con.execute("SELECT id FROM jobs WHERE id=?", (task_id,)).fetchone()
        except (OSError, sqlite3.Error):
            return None
        return row[0] if row else None

    def submit(self, task: str, task_id: str) -> dict:
        if not self.enabled:
            return {"status": "not_submitted", "reason": "adapter_unavailable"}
        if not isinstance(task, str) or not task.strip() or len(task) > 100_000:
            return {"status": "not_submitted", "reason": "invalid_task"}
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            return {"status": "not_submitted", "reason": "invalid_task_id"}
        original_input = [task, self.model, self.effort]
        legacy_fingerprint = hashlib.sha256(json.dumps(original_input, ensure_ascii=True).encode("ascii")).hexdigest()
        evidence_fingerprint = hashlib.sha256(json.dumps(original_input + ["client_session_evidence_v1"], ensure_ascii=True).encode("ascii")).hexdigest()
        fingerprint = evidence_fingerprint if self.session_evidence else legacy_fingerprint
        session_id = None
        with _connect(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT fingerprint,status,error,heartbeat FROM jobs WHERE id=?", (task_id,)).fetchone()
            if old:
                if old["fingerprint"] not in (legacy_fingerprint, evidence_fingerprint):
                    return {"status": "not_submitted", "reason": "task_id_conflict"}
                prior = old["status"]
                if prior in ("queued", "running"):
                    heartbeat = old["heartbeat"]
                    if heartbeat is None or not 0 <= time.time() - heartbeat <= 10:
                        prior = "submission_uncertain"
                    return {"status": prior, "job_id": task_id, "replayed": True}
                if prior in ("reserved", "submission_uncertain", "finished_uncertain"):
                    return {"status": "submission_uncertain", "job_id": task_id, "replayed": True}
                # This invocation did not start new work. Return the handle
                # of the original terminal record so callers can poll it; do
                # not claim that a finished or failed job is queued.
                outcome = ("not_submitted" if prior == "failed" and old["error"] in
                           ("worker_setup_failed", "input_too_large") else prior)
                return {"status": "not_submitted", "reason": "already_submitted",
                        "job_id": task_id, "replayed": True, "existing_status": outcome}
            active = con.execute("SELECT COUNT(*) FROM jobs WHERE status IN (?,?,?,?)", _ACTIVE).fetchone()[0]
            if active >= self.max_concurrency:
                return {"status": "not_submitted", "reason": "provider_busy"}
            if self.session_evidence:
                try:
                    session_id = str(uuid.uuid4())
                except Exception:
                    # The transaction has not reserved or launched anything.
                    return {"status": "not_submitted", "reason": "session_evidence_unavailable"}
            con.execute("INSERT INTO jobs VALUES (?,?,'reserved',NULL,NULL,NULL)", (task_id, fingerprint))
            if session_id:
                con.execute("INSERT INTO session_evidence_intents VALUES (?,?,NULL)", (task_id, session_id))

        payload = json.dumps({"task": task, "cli_path": self.cli_path, "profile_dir": self.profile_dir,
                              "work_dir": self.work_dir, "model": self.model, "effort": self.effort,
                              "timeout_seconds": self.timeout, "session_evidence": self.session_evidence,
                              "session_id": session_id}, ensure_ascii=True).encode("ascii")
        if len(payload) > _MAX_INPUT_BYTES:
            # No worker or provider has been started yet.
            self._set(task_id, "failed", "input_too_large")
            return {"status": "not_submitted", "reason": "invalid_task"}
        try:
            kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                      "cwd": self.work_dir, "shell": False, "close_fds": True,
                      "env": child_environment(os.environ, self.profile_dir, self.work_dir)}
            if os.name == "nt":
                kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                                           getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            else:
                kwargs["start_new_session"] = True
        except Exception:
            # No process launch was attempted, so this reservation can safely
            # stop occupying a slot. Keep the task record to prevent replays.
            self._set_if_reserved(task_id, "failed", "worker_setup_failed")
            return {"status": "not_submitted", "reason": "worker_unavailable"}
        try:
            process = subprocess.Popen([sys.executable, _WORKER_SCRIPT, "--worker", str(self.state_dir), task_id], **kwargs)
        except Exception:
            # A spawn error alone is not a durable proof that no worker was
            # created. Preserve the ID and slot until an operator reconciles.
            try:
                self._set_if_reserved(task_id, "submission_uncertain", "worker_unavailable")
            except Exception:
                pass
            return {"status": "submission_uncertain", "job_id": task_id,
                    "reason": "worker_unavailable"}
        # Once a worker exists, failure to write or observe a receipt does not
        # prove that it failed to submit. Preserve the ID and never relaunch.
        try:
            threading.Thread(target=process.wait, daemon=True).start()
        except RuntimeError:
            # A reaper failure must not be reported as not_submitted: the
            # independently started worker already exists and may run.
            pass
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except (OSError, ValueError, BrokenPipeError):
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            self._set_if_reserved(task_id, "submission_uncertain", "dispatch_interrupted")
            return {"status": "submission_uncertain", "job_id": task_id, "reason": "dispatch_interrupted"}
        self._set_if_reserved(task_id, "queued", None)
        return {"status": "queued", "job_id": task_id}

    def _set(self, job_id, status, error=None, output_file=None):
        with _connect(self.db) as con:
            con.execute("UPDATE jobs SET status=?,error=?,heartbeat=?,output_file=? WHERE id=?",
                        (status, error, time.time(), output_file, job_id))

    def _set_if_reserved(self, job_id, status, error):
        with _connect(self.db) as con:
            con.execute("UPDATE jobs SET status=?,error=?,heartbeat=? WHERE id=? AND status='reserved'",
                        (status, error, time.time(), job_id))

    def poll(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not _TASK_ID.fullmatch(job_id):
            return {"status": "submission_uncertain", "error": "unknown_job"}
        with _connect(self.db) as con:
            row = con.execute("SELECT status,error,heartbeat,output_file FROM jobs WHERE id=?", (job_id,)).fetchone()
            evidence_row = con.execute("SELECT quality_json FROM session_evidence_intents WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return {"status": "submission_uncertain", "error": "unknown_job"}
        session_quality = None
        if evidence_row is not None:
            try:
                session_quality = _stored_session_quality(json.loads(evidence_row[0])) if evidence_row[0] else _stored_session_quality(None)
            except (ValueError, TypeError):
                session_quality = _stored_session_quality(None)
        status, error, heartbeat, output_file = row
        if status == "finished_uncertain":
            # Local CLI exit is confirmed, but the remote outcome is not.
            # It does not occupy a live local slot and cannot be re-sent.
            result = {"status": "submission_uncertain", "error": error,
                      "local_exit_confirmed": True}
            if session_quality is not None:
                result["quality_verification"] = {**_stored_model_quality(None), **session_quality}
            return result
        if status == "succeeded":
            # Only the expected fixed filename inside the private state root is
            # accepted. A missing/corrupt output does not authorize a retry.
            expected = job_id + ".result.json"
            if output_file != expected:
                result = {"status": "submission_uncertain", "error": "result_unavailable"}
                if session_quality is not None:
                    result["quality_verification"] = {**_stored_model_quality(None), **session_quality}
                return result
            path = self.state_dir / expected
            try:
                if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                    raise ValueError("unsafe result path")
                result = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(result, dict) or not isinstance(result.get("output"), str):
                    raise ValueError("invalid result")
                quality = _stored_model_quality(result.get("quality_verification"))
                if session_quality is not None:
                    quality.update(session_quality)
                return {"status": "succeeded", "output": result["output"],
                        "model_verification": "unverified",
                        "effort_verification": "unverified", "quality_verification": quality}
            except (OSError, ValueError):
                result = {"status": "submission_uncertain", "error": "result_unavailable"}
                if session_quality is not None:
                    result["quality_verification"] = {**_stored_model_quality(None), **session_quality}
                return result
        if status == "reserved" or (status in ("queued", "running") and (not heartbeat or time.time() - heartbeat > 10)):
            result = {"status": "submission_uncertain", "error": "job_state_unavailable"}
            if session_quality is not None:
                result["quality_verification"] = {**_stored_model_quality(None), **session_quality}
            return result
        result = {"status": status}
        if error:
            result["error"] = error
        if session_quality is not None:
            result["quality_verification"] = {**_stored_model_quality(None), **session_quality}
        return result


def _run_worker(state_dir: str, job_id: str) -> None:
    if not _TASK_ID.fullmatch(job_id):
        return
    state = Path(_absolute(state_dir))
    db = state / "jobs.sqlite3"
    def update(status, error=None, output_file=None):
        with _connect(db) as con:
            changed = con.execute("UPDATE jobs SET status=?,error=?,heartbeat=?,output_file=? WHERE id=? "
                                  "AND status IN ('reserved','queued','running','submission_uncertain')",
                                  (status, error, time.time(), output_file, job_id)).rowcount
        return changed == 1

    def commit_result(output_file, evidence_pending):
        with _connect(db) as con:
            con.execute("BEGIN IMMEDIATE")
            changed = con.execute("UPDATE jobs SET status='succeeded',error=NULL,heartbeat=?,output_file=? "
                                  "WHERE id=? AND status IN ('reserved','queued','running','submission_uncertain')",
                                  (time.time(), output_file, job_id)).rowcount
            if changed != 1:
                return False
            if evidence_pending:
                pending = {**_stored_session_quality(None), "client_evidence_status": "pending",
                           "session_cleanup": "unconfirmed"}
                changed = con.execute("UPDATE session_evidence_intents SET quality_json=? "
                                      "WHERE job_id=? AND session_id=?",
                                      (json.dumps(pending, ensure_ascii=True), job_id, message["session_id"])).rowcount
                if changed != 1:
                    raise ValueError("session intent unavailable")
        return True
    try:
        raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            raise ValueError("input too large")
        message = json.loads(raw.decode("utf-8"))
        task = message["task"]
        session_evidence = message.get("session_evidence", False)
        if type(session_evidence) is not bool:
            raise ValueError("invalid session mode")
        if session_evidence:
            with _connect(db) as con:
                intent = con.execute("SELECT session_id FROM session_evidence_intents WHERE job_id=?", (job_id,)).fetchone()
            if intent is None or intent[0] != message.get("session_id"):
                raise ValueError("missing session intent")
        adapter = ClaudeAdapter(message["cli_path"], message["profile_dir"], message["work_dir"],
                                message["model"], message["effort"], enabled=True,
                                timeout_seconds=message["timeout_seconds"], max_concurrency=1,
                                session_evidence=session_evidence)
        receipt = (adapter.submit(task, job_id, _session_id=message["session_id"]) if session_evidence
                   else adapter.submit(task, job_id))
        if receipt.get("status") != "queued":
            # An uncertain receipt may represent a real submission: preserve
            # its slot and ID rather than falsely marking it as failed.
            if receipt.get("status") == "not_submitted":
                update("failed", "worker_unavailable")
            else:
                update("submission_uncertain", "worker_unavailable")
            return
        while True:
            result = adapter.poll(job_id)
            status = result.get("status")
            if session_evidence and status not in ("queued", "running", "succeeded"):
                safe_session = _stored_session_quality(None)
                with _connect(db) as con:
                    con.execute("UPDATE session_evidence_intents SET quality_json=? WHERE job_id=?",
                                (json.dumps(safe_session, ensure_ascii=True), job_id))
            if status == "succeeded":
                filename = job_id + ".result.json"
                if session_evidence and (result.get("local_exit_confirmed") is not True or result.get("session_bound") is not True):
                    update("submission_uncertain", "invalid_provider_response")
                    return
                try:
                    _atomic_json(state / filename, {"output": result["output"],
                                 "quality_verification": _stored_model_quality(result.get("quality_verification"), message["model"])})
                except Exception:
                    # A completed provider result could not be saved. Never
                    # expose it as a usable result or launch the job again.
                    # Free only the local slot when this precise response
                    # carries explicit evidence of the CLI process exit.
                    durable_status = ("finished_uncertain" if result.get("local_exit_confirmed") is True
                                      else "submission_uncertain")
                    update(durable_status, "result_unavailable")
                    return
                if not commit_result(filename, session_evidence):
                    return
                # The answer and its ledger status must already be durable
                # before any operation that may delete the client session.
                # A crash or error in collection does not invalidate the
                # committed result and never authorizes another submission.
                if session_evidence and result.get("local_exit_confirmed") is True and result.get("session_bound") is True:
                    # Pending/unconfirmed and succeeded were committed in
                    # the same transaction before any session access.
                    try:
                        if __package__:
                            from .session_evidence import collect_session_evidence
                        else:
                            from session_evidence import collect_session_evidence
                        safe_session = _stored_session_quality(collect_session_evidence(
                            message["profile_dir"], message["work_dir"], message["session_id"],
                            message["model"], message["effort"], process_exit_confirmed=True, enabled=True,
                            durable_result_saved=True))
                    except Exception:
                        return
                    try:
                        with _connect(db) as con:
                            con.execute("UPDATE session_evidence_intents SET quality_json=? WHERE job_id=?",
                                        (json.dumps(safe_session, ensure_ascii=True), job_id))
                    except (OSError, sqlite3.Error):
                        pass
                return
            if status == "failed":
                update("failed", result.get("error"))
                return
            if status == "submission_uncertain" and result.get("error") != "provider_slow":
                # Only an explicit, verified local exit can free this local
                # slot. The remote result remains unknown and is never retried.
                durable_status = ("finished_uncertain" if result.get("local_exit_confirmed") is True
                                  else "submission_uncertain")
                update(durable_status, result.get("error"))
                return
            update("submission_uncertain" if status == "submission_uncertain" else "running", result.get("error"))
            time.sleep(1)
    except Exception:
        # If anything fails after a worker has started, the provider may have
        # received a request. Keep the job and its slot in the uncertain state.
        try:
            update("submission_uncertain", "worker_error")
        except Exception:
            pass


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--worker":
    _run_worker(sys.argv[2], sys.argv[3])
