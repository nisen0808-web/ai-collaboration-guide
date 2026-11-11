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

if __package__:
    from .claude_adapter import ClaudeAdapter, child_environment, _absolute
else:
    # Detached execution uses this file as a script; Python then adds its
    # containing directory rather than the package parent to sys.path.
    from claude_adapter import ClaudeAdapter, child_environment, _absolute

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
                 *, enabled=False, timeout_seconds=900, max_concurrency=2):
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
                                      timeout_seconds=timeout_seconds)
        self.model, self.effort, self.enabled = model, effort, enabled
        self.timeout, self.max_concurrency = timeout_seconds, max_concurrency
        self.db = self.state_dir / "jobs.sqlite3"
        with _connect(self.db) as con:
            con.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                        "status TEXT NOT NULL, error TEXT, heartbeat REAL, output_file TEXT)")

    def login(self) -> dict:
        return self._adapter.login()

    def status(self) -> dict:
        with _connect(self.db) as con:
            n = con.execute("SELECT COUNT(*) FROM jobs WHERE status IN (?,?,?,?)", _ACTIVE).fetchone()[0]
        return {"provider": "claude", "configured": self.enabled, "ready": None,
                "verification": "not_verified", "local_active": n,
                "local_limit": self.max_concurrency}

    def submit(self, task: str, task_id: str) -> dict:
        if not self.enabled:
            return {"status": "not_submitted", "reason": "adapter_unavailable"}
        if not isinstance(task, str) or not task.strip() or len(task) > 100_000:
            return {"status": "not_submitted", "reason": "invalid_task"}
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            return {"status": "not_submitted", "reason": "invalid_task_id"}
        fingerprint = hashlib.sha256(json.dumps([task, self.model, self.effort], ensure_ascii=True).encode("ascii")).hexdigest()
        with _connect(self.db) as con:
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT fingerprint FROM jobs WHERE id=?", (task_id,)).fetchone()
            if old:
                if old[0] != fingerprint:
                    return {"status": "not_submitted", "reason": "task_id_conflict"}
                return {"status": "queued", "job_id": task_id, "replayed": True}
            active = con.execute("SELECT COUNT(*) FROM jobs WHERE status IN (?,?,?,?)", _ACTIVE).fetchone()[0]
            if active >= self.max_concurrency:
                return {"status": "not_submitted", "reason": "provider_busy"}
            con.execute("INSERT INTO jobs VALUES (?,?,'reserved',NULL,NULL,NULL)", (task_id, fingerprint))

        payload = json.dumps({"task": task, "cli_path": self.cli_path, "profile_dir": self.profile_dir,
                              "work_dir": self.work_dir, "model": self.model, "effort": self.effort,
                              "timeout_seconds": self.timeout}, ensure_ascii=True).encode("ascii")
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
            process = subprocess.Popen([sys.executable, _WORKER_SCRIPT, "--worker", str(self.state_dir), task_id], **kwargs)
        except Exception:
            # A spawn error alone is not a durable proof that no worker was
            # created. Preserve the ID and slot until an operator reconciles.
            try:
                self._set(task_id, "submission_uncertain", "worker_unavailable")
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
        if row is None:
            return {"status": "submission_uncertain", "error": "unknown_job"}
        status, error, heartbeat, output_file = row
        if status == "succeeded":
            # Only the expected fixed filename inside the private state root is
            # accepted. A missing/corrupt output does not authorize a retry.
            expected = job_id + ".result.json"
            if output_file != expected:
                return {"status": "submission_uncertain", "error": "result_unavailable"}
            path = self.state_dir / expected
            try:
                if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                    raise ValueError("unsafe result path")
                result = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(result, dict) or not isinstance(result.get("output"), str):
                    raise ValueError("invalid result")
                return {"status": "succeeded", "output": result["output"],
                        "model_verification": "unverified", "effort_verification": "unverified"}
            except (OSError, ValueError):
                return {"status": "submission_uncertain", "error": "result_unavailable"}
        if status == "reserved" or (status in ("queued", "running") and (not heartbeat or time.time() - heartbeat > 10)):
            return {"status": "submission_uncertain", "error": "job_state_unavailable"}
        result = {"status": status}
        if error:
            result["error"] = error
        return result


def _run_worker(state_dir: str, job_id: str) -> None:
    if not _TASK_ID.fullmatch(job_id):
        return
    state = Path(_absolute(state_dir))
    db = state / "jobs.sqlite3"
    def update(status, error=None, output_file=None):
        with _connect(db) as con:
            con.execute("UPDATE jobs SET status=?,error=?,heartbeat=?,output_file=? WHERE id=?",
                        (status, error, time.time(), output_file, job_id))
    try:
        raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            raise ValueError("input too large")
        message = json.loads(raw.decode("utf-8"))
        task = message["task"]
        adapter = ClaudeAdapter(message["cli_path"], message["profile_dir"], message["work_dir"],
                                message["model"], message["effort"], enabled=True,
                                timeout_seconds=message["timeout_seconds"], max_concurrency=1)
        receipt = adapter.submit(task, job_id)
        if receipt.get("status") != "queued":
            update("failed", "worker_unavailable")
            return
        while True:
            result = adapter.poll(job_id)
            status = result.get("status")
            if status == "succeeded":
                filename = job_id + ".result.json"
                _atomic_json(state / filename, {"output": result["output"]})
                update("succeeded", output_file=filename)
                return
            if status == "failed":
                update("failed", result.get("error"))
                return
            if status == "submission_uncertain" and result.get("error") != "provider_slow":
                update("submission_uncertain", result.get("error"))
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
