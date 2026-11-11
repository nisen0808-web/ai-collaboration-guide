"""Detached, local job candidate for the Grok CLI adapter.

The caller explicitly selects a tool-owned state directory. Job input, output
and metadata are kept there, under operating-system permissions supplied by the
user. This is not an ACL verifier or a guarantee against same-account access.
"""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import uuid

if __package__:
    from .grok_adapter import GrokAdapter, _MAX_TASK_BYTES
else:
    # The detached worker executes this file directly.
    from grok_adapter import GrokAdapter, _MAX_TASK_BYTES


_ACTIVE = ("dispatching", "queued", "running", "submission_uncertain")
_SCHEMA = """CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY, task_key TEXT NOT NULL UNIQUE,
    task_digest TEXT NOT NULL, state TEXT NOT NULL,
    worker_pid INTEGER, worker_identity TEXT, result_json TEXT NOT NULL
)"""


def _valid_id(value: str) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def _not_reparse(path: Path) -> bool:
    try:
        return not path.is_symlink() and not (getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def _process_identity(pid: int) -> str | None:
    """Return a creation-time identity; no process is stopped or signalled."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *[ctypes.byref(value) for value in times]):
                return None
            return f"{pid}:{times[0].dwHighDateTime}:{times[0].dwLowDateTime}"
        finally:
            kernel.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else f"{pid}:{fields[19]}"
    except (OSError, IndexError):
        return None  # Unsupported platforms never claim worker liveness.


@dataclass(frozen=True)
class _StoredJob:
    job_id: str
    state: str
    worker_pid: int | None
    worker_identity: str | None
    result: dict


class _Ledger:
    def __init__(self, root: Path):
        if not root.is_absolute() or not root.is_dir() or not _not_reparse(root):
            raise ValueError("state directory must already exist and not be a link")
        self.root = root
        self.jobs_dir = root / "grok-jobs"
        self.jobs_dir.mkdir(mode=0o700, exist_ok=True)
        if not self.jobs_dir.is_dir() or not _not_reparse(self.jobs_dir):
            raise ValueError("invalid job directory")
        self.db = root / "grok-jobs.sqlite3"
        if self.db.exists() and not _not_reparse(self.db):
            raise ValueError("invalid job database")
        with self._connect() as con:
            con.execute(_SCHEMA)

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(str(self.db), timeout=5)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def reserve(self, task_id: str, digest: str, max_concurrency: int) -> tuple[str, str | None]:
        # A hash is not encryption; state_root must be protected by the OS.
        task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT job_id, task_digest FROM jobs WHERE task_key=?", (task_key,)).fetchone()
            if old:
                return ("existing" if old["task_digest"] == digest else "conflict", old["job_id"])
            placeholders = ",".join("?" for _ in _ACTIVE)
            active = con.execute(f"SELECT count(*) FROM jobs WHERE state IN ({placeholders})", _ACTIVE).fetchone()[0]
            if active >= max_concurrency:
                return "busy", None
            job_id = str(uuid.uuid4())
            result = json.dumps({"status": "submission_uncertain", "job_id": job_id,
                                 "error": "worker_start_unconfirmed"})
            con.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                        (job_id, task_key, digest, "dispatching", result))
            return "reserved", job_id

    def write_request(self, job_id: str, request: dict) -> None:
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(mode=0o700, exist_ok=False)
        if not _not_reparse(job_dir):
            raise ValueError("invalid new job directory")
        descriptor = os.open(job_dir / "request.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(request, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())

    def claim(self, job_id: str) -> bool:
        identity = _process_identity(os.getpid())
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            changed = con.execute("UPDATE jobs SET worker_pid=?, worker_identity=?, state=? "
                                  "WHERE job_id=? AND worker_pid IS NULL AND state IN (?, ?)",
                                  (os.getpid(), identity, "queued", job_id,
                                   "dispatching", "submission_uncertain")).rowcount
            return changed == 1

    def store(self, job_id: str, state: dict) -> None:
        # Store full results only under user-selected state_root, never stdout.
        text = json.dumps(state, ensure_ascii=True)
        with self._connect() as con:
            con.execute("UPDATE jobs SET state=?, result_json=? WHERE job_id=?",
                        (state["status"], text, job_id))

    def mark_start_uncertain_if_unclaimed(self, job_id: str) -> None:
        state = {"status": "submission_uncertain", "job_id": job_id,
                 "error": "worker_start_unconfirmed"}
        with self._connect() as con:
            # Never overwrite progress/result if a worker was in fact created
            # and claimed this task while the parent observed a start failure.
            con.execute("UPDATE jobs SET state=?, result_json=? WHERE job_id=? "
                        "AND state=? AND worker_pid IS NULL",
                        (state["status"], json.dumps(state), job_id, "dispatching"))

    def get(self, job_id: str) -> _StoredJob | None:
        if not _valid_id(job_id):
            return None
        with self._connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        return _StoredJob(job_id, row["state"], row["worker_pid"], row["worker_identity"],
                          json.loads(row["result_json"]))

    def capacity(self) -> int:
        placeholders = ",".join("?" for _ in _ACTIVE)
        with self._connect() as con:
            return con.execute(f"SELECT count(*) FROM jobs WHERE state IN ({placeholders})", _ACTIVE).fetchone()[0]

    def find_job(self, task_id: str) -> str | None:
        if not isinstance(task_id, str) or not 1 <= len(task_id) <= 128:
            return None
        try:
            task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
        except UnicodeError:
            return None
        try:
            with self._connect() as con:
                row = con.execute("SELECT job_id FROM jobs WHERE task_key=?", (task_key,)).fetchone()
        except (OSError, sqlite3.Error):
            return None
        return row[0] if row else None

    def occupancy(self) -> tuple[int, int]:
        """Return known-running and uncertain counts; never release slots."""
        placeholders = ",".join("?" for _ in _ACTIVE)
        with self._connect() as con:
            rows = con.execute(f"SELECT state,worker_pid,worker_identity FROM jobs WHERE state IN ({placeholders})", _ACTIVE).fetchall()
        def is_observed(row):
            if row["state"] not in ("queued", "running") or not row["worker_identity"]:
                return False
            try:
                return _process_identity(row["worker_pid"]) == row["worker_identity"]
            except (OSError, ValueError):
                return False
        observed = sum(is_observed(row) for row in rows)
        return observed, len(rows) - observed


def _worker_command(root: Path, job_id: str) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--worker", str(root), job_id]


def _worker_env() -> dict[str, str]:
    # Only these baseline variables are inherited; no key/config values.
    names = ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "NO_COLOR")
    return {name: value for name in names if isinstance((value := os.environ.get(name)), str)}


class DurableGrokAdapter:
    """One detached process per job, with persistent deduplication and polling."""

    def __init__(self, cli: str, model: str, effort: str, profile_dir: str,
                 state_root: str, timeout_seconds: int = 600, max_concurrency: int = 2):
        # Reuse config validation, but no real client is started in __init__.
        self.adapter = GrokAdapter(cli, model, effort, profile_dir, timeout_seconds, max_concurrency)
        self.ledger = _Ledger(Path(state_root))
        self.max_concurrency = max_concurrency

    def login(self) -> int:
        return self.adapter.login()

    def find_job(self, task_id: str) -> str | None:
        """Find a recorded job without dispatching or retrying it."""
        return self.ledger.find_job(task_id)

    def __call__(self, task: str, task_id: str) -> dict:
        return self.submit(task, task_id)

    def submit(self, task: str, task_id: str) -> dict:
        if not isinstance(task, str) or not task.strip() or not isinstance(task_id, str) or not 1 <= len(task_id) <= 128:
            return {"status": "not_submitted", "reason": "invalid_task"}
        try:
            data = task.encode("utf-8")
            task_id.encode("utf-8")
        except UnicodeError:
            return {"status": "not_submitted", "reason": "invalid_task"}
        if len(data) > _MAX_TASK_BYTES or not self.adapter._paths_ready():
            return {"status": "not_submitted", "reason": "invalid_task_or_configuration"}
        try:
            result, job_id = self.ledger.reserve(task_id, hashlib.sha256(data).hexdigest(), self.max_concurrency)
        except (OSError, sqlite3.Error):
            return {"status": "not_submitted", "reason": "state_unavailable"}
        if result == "busy":
            return {"status": "not_submitted", "reason": "provider_busy"}
        if result == "conflict":
            return {"status": "not_submitted", "reason": "task_id_conflict"}
        if result == "existing":
            old = self.poll(job_id)
            if old["status"] not in _ACTIVE:
                return {"status": "not_submitted", "reason": "already_submitted", "job_id": job_id, "replayed": True}
            return {"status": old["status"], "job_id": job_id, "replayed": True}
        request = {"task": task, "cli": str(self.adapter.cli), "model": self.adapter.model,
                   "effort": self.adapter.effort, "profile_dir": str(self.adapter.profile),
                   "timeout_seconds": self.adapter.timeout, "max_concurrency": self.max_concurrency}
        try:
            self.ledger.write_request(job_id, request)
        except (OSError, ValueError):
            try:
                self.ledger.store(job_id, {"status": "not_submitted", "job_id": job_id, "reason": "state_write_failed"})
            except (OSError, sqlite3.Error):
                pass  # No worker was launched; stale reservation stays conservative.
            return {"status": "not_submitted", "reason": "state_write_failed", "job_id": job_id}
        options = {"cwd": str(self.ledger.root), "env": _worker_env(), "stdin": subprocess.DEVNULL,
                   "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "shell": False, "close_fds": True}
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        else:
            options["start_new_session"] = True
        try:
            subprocess.Popen(_worker_command(self.ledger.root, job_id), **options)
        except OSError:
            # The process-creation result is ambiguous. Preserve the record and
            # original request; never auto-spawn a replacement.
            try:
                self.ledger.mark_start_uncertain_if_unclaimed(job_id)
            except (OSError, sqlite3.Error):
                pass  # An unresolved reservation already blocks resubmission.
            return {"status": "submission_uncertain", "job_id": job_id}
        return {"status": "queued", "job_id": job_id}

    def poll(self, job_id: str) -> dict:
        try:
            job = self.ledger.get(job_id)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            job = None
        if job is None:
            return {"status": "submission_uncertain", "job_id": job_id, "error": "job_state_unavailable"}
        if job.state in {"dispatching", "queued", "running"}:
            if not job.worker_identity or _process_identity(job.worker_pid) != job.worker_identity:
                return {"status": "submission_uncertain", "job_id": job_id, "error": "worker_identity_unconfirmed"}
        return dict(job.result)

    def status(self) -> dict:
        observed, uncertain = self.ledger.occupancy()
        active = observed + uncertain
        return {"provider": "grok", "local_active_jobs": active, "local_max_concurrency": self.max_concurrency,
                "local_observed_inflight": observed, "local_uncertain": uncertain,
                "local_available_slots": max(0, self.max_concurrency - active), "official_quota_verified": False,
                "login_verified": False}


def worker_main(root: str, job_id: str) -> int:
    if not _valid_id(job_id):
        return 2
    ledger = _Ledger(Path(root))
    if not ledger.claim(job_id):
        return 2  # A duplicate worker must never run the same task.
    request_path = ledger.jobs_dir / job_id / "request.json"
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        adapter = GrokAdapter(request["cli"], request["model"], request["effort"], request["profile_dir"],
                              request["timeout_seconds"], request["max_concurrency"])
        task = request["task"]
        # From this point the original task is in worker memory. If the worker
        # crashes, the ledger stays uncertain and must not be resubmitted.
        request_path.unlink()
    except (OSError, ValueError, KeyError, TypeError):
        ledger.store(job_id, {"status": "failed", "job_id": job_id, "error": "worker_request_invalid", "submitted": False})
        return 2
    adapter._jobs[job_id] = {"status": "queued", "job_id": job_id}
    adapter._active_job_ids.add(job_id)
    original_update = adapter._update

    def persist_update(current_id: str, **fields) -> None:
        original_update(current_id, **fields)
        try:
            ledger.store(current_id, adapter.poll(current_id))
        except (OSError, sqlite3.Error, ValueError, TypeError):
            # A metadata-write problem must not kill a live child. Polling
            # remains conservative until a later update can be persisted.
            pass

    adapter._update = persist_update
    # The CLI prompt/output files are created inside this tool-owned job folder,
    # not in the parent's global temp. No other jobs or credentials are touched.
    old_tempdir = tempfile.tempdir
    try:
        tempfile.tempdir = str(ledger.jobs_dir / job_id)
        adapter._execute(job_id, task)
    finally:
        tempfile.tempdir = old_tempdir
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--worker":
        raise SystemExit(2)
    raise SystemExit(worker_main(sys.argv[2], sys.argv[3]))
