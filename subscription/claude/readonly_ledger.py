"""Read prior Claude jobs while new submissions are disabled.

This adapter never constructs a provider client, changes the ledger, starts a
process, or requests login.  A missing or unreadable ledger is not evidence
that an old job was never submitted.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time

if __package__:
    from .claude_adapter import _stored_model_quality, _stored_session_quality
else:
    from claude_adapter import _stored_model_quality, _stored_session_quality

_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_ACTIVE = ("reserved", "queued", "running", "submission_uncertain")
_EXIT_UNCONFIRMED = ("process_exit_unconfirmed", "process_still_running",
                     "worker_identity_unconfirmed", "operator_released")
_BLOCKING_WHERE = ("(status IN (?,?,?,?) OR "
                   "(status='finished_uncertain' AND error IN (?,?,?,?)))")
_BLOCKING_PARAMS = _ACTIVE + _EXIT_UNCONFIRMED


class ReadOnlyClaudeLedger:
    """Reject new tasks while allowing conservative reads of existing jobs."""

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.db = self.state_dir / "jobs.sqlite3"

    @contextmanager
    def _connection(self):
        # Do not create an absent ledger or follow a replacement/junction at
        # the ledger or state directory.  SQLite's mode=ro forbids DB writes.
        for path in (self.state_dir, self.db):
            stat = path.lstat()
            if path.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                raise OSError("unsafe ledger")
        if not self.state_dir.is_dir() or not self.db.is_file():
            raise OSError("ledger unavailable")
        connection = sqlite3.connect(self.db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def submit(self, task: str, task_id: str) -> dict:
        return {"status": "not_submitted", "reason": "session_evidence_model_invalid"}

    def status(self) -> dict:
        result = {"provider": "claude", "configured": False, "ready": False,
                  "verification": "not_verified"}
        try:
            with self._connection() as con:
                rows = con.execute("SELECT status,heartbeat FROM jobs WHERE " + _BLOCKING_WHERE, _BLOCKING_PARAMS).fetchall()
                finished = con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('finished_uncertain','operator_released') "
                                       "AND NOT (status='finished_uncertain' AND COALESCE(error,'') IN (?,?,?,?))", _EXIT_UNCONFIRMED).fetchone()[0]
            now = time.time()
            observed = sum(row[0] in ("queued", "running") and row[1] is not None
                           and 0 <= now - row[1] <= 10 for row in rows)
            result.update(local_active=len(rows), local_observed_inflight=observed,
                          local_uncertain=len(rows) - observed, local_finished_unresolved=finished)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            pass
        return result

    def find_job(self, task_id: str) -> str | None:
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            return None
        try:
            with self._connection() as con:
                row = con.execute("SELECT id FROM jobs WHERE id=?", (task_id,)).fetchone()
            return row[0] if row else None
        except (OSError, sqlite3.Error, ValueError):
            return None

    def poll(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not _TASK_ID.fullmatch(job_id):
            return {"status": "submission_uncertain", "error": "unknown_job"}
        try:
            with self._connection() as con:
                row = con.execute("SELECT status,error,heartbeat,output_file FROM jobs WHERE id=?", (job_id,)).fetchone()
                exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                     ("session_evidence_intents",)).fetchone()
                evidence = (con.execute("SELECT quality_json FROM session_evidence_intents WHERE job_id=?", (job_id,)).fetchone()
                            if exists else None)
        except (OSError, sqlite3.Error, ValueError):
            return {"status": "submission_uncertain", "error": "job_state_unavailable"}
        if row is None:
            return {"status": "submission_uncertain", "error": "unknown_job"}
        session_quality = None
        if evidence is not None:
            try:
                session_quality = _stored_session_quality(json.loads(evidence[0])) if evidence[0] else _stored_session_quality(None)
            except (ValueError, TypeError):
                session_quality = _stored_session_quality(None)

        def attach(result):
            if session_quality is not None:
                result["quality_verification"] = {**_stored_model_quality(None), **session_quality}
            return result

        status, error, heartbeat, output_file = row
        if status == "operator_released":
            return attach({"status": "submission_uncertain", "error": "operator_released",
                           "local_exit_confirmed": False, "release_basis": "operator_confirmed"})
        if status == "finished_uncertain":
            return attach({"status": "submission_uncertain", "error": error, "local_exit_confirmed": error not in _EXIT_UNCONFIRMED})
        if status == "succeeded":
            expected = job_id + ".result.json"
            if output_file != expected:
                return attach({"status": "submission_uncertain", "error": "result_unavailable"})
            path = self.state_dir / expected
            try:
                if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                    raise ValueError("unsafe result")
                result = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(result, dict) or not isinstance(result.get("output"), str):
                    raise ValueError("invalid result")
                quality = _stored_model_quality(result.get("quality_verification"))
                if session_quality is not None:
                    quality.update(session_quality)
                projected = {"status": "succeeded", "output": result["output"],
                             "model_verification": "unverified", "effort_verification": "unverified",
                             "quality_verification": quality}
                # Only a boolean explicitly written in this result can be
                # projected. Older files must not acquire inferred evidence.
                local_exit = result.get("local_exit_confirmed")
                if type(local_exit) is bool:
                    projected["local_exit_confirmed"] = (
                        local_exit and error not in _EXIT_UNCONFIRMED
                        and result.get("error") not in _EXIT_UNCONFIRMED
                        and result.get("release_basis") != "operator_confirmed")
                return projected
            except (OSError, ValueError):
                return attach({"status": "submission_uncertain", "error": "result_unavailable"})
        if status == "reserved" or (status in ("queued", "running") and (not heartbeat or time.time() - heartbeat > 10)):
            return attach({"status": "submission_uncertain", "error": "job_state_unavailable"})
        result = {"status": status}
        if error:
            result["error"] = error
        return attach(result)
