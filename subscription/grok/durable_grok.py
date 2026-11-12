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
import threading
import uuid

if __package__:
    from .grok_adapter import GrokAdapter, _MAX_TASK_BYTES, _NETWORK_ENV_NAMES
else:
    # The detached worker executes this file directly.
    from grok_adapter import GrokAdapter, _MAX_TASK_BYTES, _NETWORK_ENV_NAMES


# Legacy ``not_submitted`` rows predate atomic pre-launch proof.  Earlier
# versions could write that state after a competing worker claimed the row;
# treat every such row as occupied until separately reconciled, even if its
# identity fields are empty. New proven failures use _PRELAUNCH_ABORTED.
_ACTIVE = ("dispatching", "queued", "running", "submission_uncertain", "not_submitted")
_PRELAUNCH_ABORTED = "prelaunch_aborted_v1"
_TERMINAL = ("succeeded", "failed")
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
    result: object


def _consistent_record(job_id: str, state: str, worker_pid: int | None,
                       worker_identity: str | None, result: object) -> bool:
    """Validate the local state/result pairing before releasing or reporting it.

    A record with an unknown state, malformed body or conflicting status is not
    evidence of a finished process. This check does not infer remote outcome.
    """
    if not _valid_id(job_id) or not isinstance(result, dict) or result.get("job_id") != job_id:
        return False
    status = result.get("status")
    if state == _PRELAUNCH_ABORTED:
        return (worker_pid is None and worker_identity is None and
                result == {"status": "not_submitted", "job_id": job_id, "reason": "state_write_failed"})
    if state == "not_submitted":
        return False  # Legacy generic terminal is never proof of no launch.
    if state == "dispatching":
        return status == "submission_uncertain"
    if state == "queued":
        # claim() sets queued before the worker writes its first update.
        return status in ("queued", "submission_uncertain")
    if state in ("running", "submission_uncertain"):
        return status == state
    if state == "finished_uncertain":
        return status == "submission_uncertain" and result.get("local_exit_confirmed") is True
    return state in _TERMINAL and status == state


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
            active = len(self._active_rows(con))
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

    def store(self, job_id: str, state: dict) -> bool:
        """Commit an update to its bound job; never overwrite a terminal result.

        A successful result may be enriched once with evidence after its
        answer has been committed. False means nothing was committed; storage
        exceptions propagate so a worker can preserve its output and continue
        conservatively without resubmitting.
        """
        if (not _valid_id(job_id) or not isinstance(state, dict) or state.get("job_id") != job_id or
                state.get("status") not in ("queued", "running", "succeeded", "failed", "submission_uncertain")):
            return False
        # Store full results only under user-selected state_root, never stdout.
        text = json.dumps(state, ensure_ascii=True)
        # The remote outcome can remain unknown even after the direct local
        # CLI process exited. Keep the original result/status for polling,
        # but do not reserve a local execution slot for that finished process.
        durable_state = ("finished_uncertain" if state.get("status") == "submission_uncertain"
                         and state.get("local_exit_confirmed") is True else state["status"])
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT state,worker_pid,worker_identity,result_json FROM jobs WHERE job_id=?",
                              (job_id,)).fetchone()
            if row is None:
                return False
            try:
                previous = json.loads(row["result_json"])
            except (ValueError, TypeError):
                return False
            if not _consistent_record(job_id, row["state"], row["worker_pid"], row["worker_identity"], previous):
                return False
            old_state = row["state"]
            if old_state in ("failed", "finished_uncertain", _PRELAUNCH_ABORTED, "not_submitted"):
                return False
            if old_state == "succeeded":
                if durable_state != "succeeded":
                    return False
                before, after = dict(previous), dict(state)
                old_quality = before.pop("quality_verification", None)
                new_quality = after.pop("quality_verification", None)
                if before != after:
                    return False
                if old_quality != new_quality:
                    if (not isinstance(old_quality, dict) or old_quality.get("client_evidence_status") != "pending" or
                            not isinstance(new_quality, dict) or
                            new_quality.get("client_evidence_status") not in ("matched", "mismatch", "unavailable")):
                        return False
            else:
                transitions = {
                    "dispatching": {"queued", "running", "succeeded", "failed", "submission_uncertain", "finished_uncertain"},
                    "queued": {"queued", "running", "succeeded", "failed", "submission_uncertain", "finished_uncertain"},
                    "running": {"running", "succeeded", "failed", "submission_uncertain", "finished_uncertain"},
                    "submission_uncertain": {"submission_uncertain", "finished_uncertain", "succeeded"},
                }
                if durable_state not in transitions.get(old_state, set()):
                    return False
            # Bind update to the same previously read state and body. The
            # surrounding transaction commits before this method returns True.
            changed = con.execute("UPDATE jobs SET state=?, result_json=? WHERE job_id=? AND state=? AND result_json=?",
                                  (durable_state, text, job_id, old_state, row["result_json"])).rowcount
        return changed == 1

    def mark_start_uncertain_if_unclaimed(self, job_id: str) -> None:
        state = {"status": "submission_uncertain", "job_id": job_id,
                 "error": "worker_start_unconfirmed"}
        with self._connect() as con:
            # Never overwrite progress/result if a worker was in fact created
            # and claimed this task while the parent observed a start failure.
            con.execute("UPDATE jobs SET state=?, result_json=? WHERE job_id=? "
                        "AND state=? AND worker_pid IS NULL",
                        (state["status"], json.dumps(state), job_id, "dispatching"))

    def finish_known_unstarted(self, job_id: str) -> bool:
        """Persist a caller-observed pre-launch failure without overwriting a claim."""
        state = {"status": "not_submitted", "job_id": job_id, "reason": "state_write_failed"}
        with self._connect() as con:
            changed = con.execute("UPDATE jobs SET state=?, result_json=? WHERE job_id=? "
                                  "AND state=? AND worker_pid IS NULL AND worker_identity IS NULL",
                                  (_PRELAUNCH_ABORTED, json.dumps(state), job_id, "dispatching")).rowcount
        return changed == 1

    def proves_prelaunch_aborted(self, task_id: str, job_id: str) -> bool:
        """Check local immutable-state evidence bound to the original task/job."""
        if not _valid_id(job_id) or not isinstance(task_id, str) or not 1 <= len(task_id) <= 128:
            return False
        try:
            task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
            with self._connect() as con:
                row = con.execute("SELECT result_json FROM jobs WHERE job_id=? AND task_key=? "
                                  "AND state=? AND worker_pid IS NULL AND worker_identity IS NULL",
                                  (job_id, task_key, _PRELAUNCH_ABORTED)).fetchone()
            return bool(row and json.loads(row["result_json"]) == {
                "status": "not_submitted", "job_id": job_id, "reason": "state_write_failed"})
        except (OSError, sqlite3.Error, ValueError, TypeError, UnicodeError):
            return False

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
        with self._connect() as con:
            return len(self._active_rows(con))

    @staticmethod
    def _active_rows(con: sqlite3.Connection) -> list[sqlite3.Row]:
        # Inspect all persisted state names: unknown or contradictory rows must
        # not be silently excluded from the local concurrency limit.
        rows = con.execute("SELECT job_id,state,worker_pid,worker_identity,result_json FROM jobs").fetchall()
        active = []
        for row in rows:
            try:
                result = json.loads(row["result_json"])
                consistent = _consistent_record(row["job_id"], row["state"], row["worker_pid"],
                                                row["worker_identity"], result)
            except (ValueError, TypeError):
                consistent = False
            if row["state"] in _ACTIVE or not consistent:
                active.append(row)
        return active

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
        with self._connect() as con:
            rows = self._active_rows(con)
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
    # Network settings may contain sensitive proxy URLs; never log values.
    # API key and provider config variables remain excluded.
    names = ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "NO_COLOR") + _NETWORK_ENV_NAMES
    return {name: value for name in names
            if isinstance((value := os.environ.get(name)), str) and "\0" not in value}


class DurableGrokAdapter:
    """One detached process per job, with persistent deduplication and polling."""

    def __init__(self, cli: str, model: str, effort: str, profile_dir: str,
                 state_root: str, timeout_seconds: int = 600, max_concurrency: int = 2):
        # Reuse config validation, but no real client is started in __init__.
        self.adapter = GrokAdapter(cli, model, effort, profile_dir, timeout_seconds, max_concurrency)
        self.ledger = _Ledger(Path(state_root))
        self.max_concurrency = max_concurrency
        # Only this live adapter can know that its write failed before Popen.
        # Never reconstruct this evidence from an unclaimed database row.
        self._known_unstarted: set[str] = set()
        self._known_unstarted_lock = threading.Lock()

    def _flush_known_unstarted(self) -> None:
        # Retrying a local state write is not retrying a provider request.
        # If the process dies before this succeeds, the persisted reservation
        # remains uncertain and continues to occupy its slot conservatively.
        with self._known_unstarted_lock:
            for job_id in tuple(self._known_unstarted):
                try:
                    self.ledger.finish_known_unstarted(job_id)
                except (OSError, sqlite3.Error):
                    continue
                self._known_unstarted.remove(job_id)

    def login(self) -> int:
        return self.adapter.login()

    def find_job(self, task_id: str) -> str | None:
        """Find a recorded job without dispatching or retrying it."""
        return self.ledger.find_job(task_id)

    def proves_prelaunch_aborted(self, task_id: str, job_id: str) -> bool:
        """Verify a versioned local ledger state, not an adapter-supplied flag."""
        if type(self.ledger) is not _Ledger:
            return False
        return _Ledger.proves_prelaunch_aborted(self.ledger, task_id, job_id)

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
        self._flush_known_unstarted()
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
            if (old.get("status") == "not_submitted" and old.get("reason") == "state_write_failed"
                    and _Ledger.proves_prelaunch_aborted(self.ledger, task_id, job_id)):
                # This exact persisted state is written only before any worker
                # launch is attempted. Preserve the truthful outcome without
                # launching again or implying an earlier provider submission.
                # Omit job_id so the coordinator does not treat it as a
                # possibly submitted provider job.
                return {"status": "not_submitted", "reason": "state_write_failed", "replayed": True}
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
                unclaimed = self.ledger.finish_known_unstarted(job_id)
            except (OSError, sqlite3.Error):
                with self._known_unstarted_lock:
                    self._known_unstarted.add(job_id)
                # This caller did not launch a worker, but the failed commit
                # cannot exclude a competing claimant. Keep the original ID.
                return {"status": "submission_uncertain", "job_id": job_id,
                        "error": "worker_start_unconfirmed"}
            if unclaimed:
                return {"status": "not_submitted", "reason": "state_write_failed"}
            # A worker may have claimed the reservation while this caller's
            # request write was failing; never overwrite its state or assert
            # that the provider was not reached.
            return {"status": "submission_uncertain", "job_id": job_id,
                    "error": "worker_start_unconfirmed"}
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
        self._flush_known_unstarted()
        try:
            job = self.ledger.get(job_id)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            job = None
        if job is None:
            return {"status": "submission_uncertain", "job_id": job_id, "error": "job_state_unavailable"}
        if not _consistent_record(job_id, job.state, job.worker_pid, job.worker_identity, job.result):
            return {"status": "submission_uncertain", "job_id": job_id,
                    "error": "worker_start_unconfirmed"}
        if job.state in {"dispatching", "queued", "running"}:
            if not job.worker_identity or _process_identity(job.worker_pid) != job.worker_identity:
                return {"status": "submission_uncertain", "job_id": job_id, "error": "worker_identity_unconfirmed"}
        return dict(job.result)

    def status(self) -> dict:
        self._flush_known_unstarted()
        observed, uncertain = self.ledger.occupancy()
        active = observed + uncertain
        with self.ledger._connect() as con:
            rows = con.execute("SELECT job_id,state,worker_pid,worker_identity,result_json FROM jobs "
                               "WHERE state='finished_uncertain'").fetchall()
        finished_unresolved = 0
        for row in rows:
            try:
                result = json.loads(row["result_json"])
                if _consistent_record(row["job_id"], row["state"], row["worker_pid"], row["worker_identity"], result):
                    finished_unresolved += 1
            except (ValueError, TypeError):
                continue  # Corrupt rows remain part of uncertain occupancy.
        return {"provider": "grok", "local_active_jobs": active, "local_max_concurrency": self.max_concurrency,
                "local_observed_inflight": observed, "local_uncertain": uncertain,
                "local_finished_unresolved": finished_unresolved,
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

    def persist_update(current_id: str, **fields) -> bool:
        try:
            if original_update(current_id, **fields) is not True:
                return False
            return ledger.store(current_id, adapter.poll(current_id)) is True
        except Exception:
            # A metadata-write problem must not kill a live child. Polling
            # remains conservative until a later update can be persisted.
            return False

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
