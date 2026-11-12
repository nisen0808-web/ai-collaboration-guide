"""Offline, explicitly attested recovery of a single legacy slot.

This command cannot verify the operator's statements about other processes,
other hosts, remote execution or a full restart. It does not stop anything.
Use only with a private, exclusively local ledger while every old dispatcher
is disabled and cannot restart until the operation is complete.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

_CLAUDE_ACTIVE = {"reserved", "queued", "running", "submission_uncertain"}
_GROK_ACTIVE = {"dispatching", "queued", "running", "submission_uncertain", "not_submitted"}
_EXIT_UNCONFIRMED = {"process_exit_unconfirmed", "process_still_running",
                     "worker_identity_unconfirmed", "operator_released"}


def _uptime_ms():
    """Weak corroboration only; VM rollback and another host can mimic a reboot."""
    if os.name == "nt":
        import ctypes
        api = ctypes.WinDLL("kernel32", use_last_error=True).GetTickCount64
        api.argtypes = []
        api.restype = ctypes.c_ulonglong
        value = api()
    elif sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
        value = int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1000)
    else:
        raise ValueError("uptime unavailable")
    if type(value) is not int or value <= 0:
        raise ValueError("uptime unavailable")
    return value


def _safe_ancestors(path: Path) -> bool:
    try:
        if str(path).startswith("\\\\"):
            return False
        for item in (path, *path.parents):
            info = item.lstat()
            if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                return False
        return True
    except OSError:
        return False


def _safe_file(path: Path) -> bool:
    try:
        return _safe_ancestors(path) and path.is_file() and path.stat().st_ino != 0
    except OSError:
        return False


def _encoded(value):
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if value is None or type(value) in (str, int, float):
        return value
    raise ValueError("unsupported record")


def _snapshot(con, provider, job_id):
    if provider == "claude":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id):
            raise ValueError("invalid job")
        required = {"id", "fingerprint", "status", "error", "heartbeat", "output_file"}
        key, state_key, active = "id", "status", _CLAUDE_ACTIVE
    elif provider == "grok":
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError("invalid job")
        required = {"job_id", "task_key", "task_digest", "state", "worker_pid", "worker_identity", "result_json"}
        key, state_key, active = "job_id", "state", _GROK_ACTIVE
    else:
        raise ValueError("invalid provider")
    info = con.execute("PRAGMA table_xinfo(jobs)").fetchall()
    if any(r[6] != 0 for r in info):
        raise ValueError("generated or hidden columns unsupported")
    names = [r[1] for r in info]
    if not required.issubset(names) or len(names) != len(set(names)):
        raise ValueError("unsupported schema")
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' "
                   "AND tbl_name IN ('jobs','legacy_reconcile_audit')").fetchone():
        raise ValueError("triggers require manual investigation")
    row = con.execute(f'SELECT * FROM jobs WHERE "{key}"=?', (job_id,)).fetchone()
    if row is None or len(row) != len(names):
        raise ValueError("job does not hold a slot")
    state = row[names.index(state_key)]
    contradictory_exit = (provider == "claude" and state == "finished_uncertain"
                          and row[names.index("error")] in _EXIT_UNCONFIRMED)
    # Grok's durable reader treats a finished state whose body lacks a strict
    # true exit flag as inconsistent and therefore still slot-blocking. Body,
    # job identity and lack of worker identity are checked below before this
    # narrow legacy recovery path may proceed.
    grok_contradictory_exit = provider == "grok" and state == "finished_uncertain"
    if state not in active and not contradictory_exit and not grok_contradictory_exit:
        raise ValueError("job does not hold a slot")
    if provider == "claude":
        claims = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_claims'").fetchone()
        if claims and con.execute("SELECT 1 FROM worker_claims WHERE job_id=?", (job_id,)).fetchone():
            raise ValueError("job has a worker identity")
    if provider == "grok":
        if row[names.index("worker_pid")] is not None or row[names.index("worker_identity")] is not None:
            raise ValueError("job has a worker identity")
        body = json.loads(row[names.index("result_json")])
        if not isinstance(body, dict) or body.get("job_id") != job_id:
            raise ValueError("invalid prior result")
        allowed = {"dispatching": {"submission_uncertain"},
                   "queued": {"queued", "submission_uncertain"},
                   "running": {"running"}, "submission_uncertain": {"submission_uncertain"},
                   "not_submitted": {"not_submitted"},
                   "finished_uncertain": {"submission_uncertain"}}
        if (body.get("status") not in allowed[row[names.index("state")]] or
            ("local_exit_confirmed" in body and body["local_exit_confirmed"] is not False)):
            raise ValueError("contradictory prior result")
    data = [[name, _encoded(value)] for name, value in zip(names, row)]
    payload = json.dumps({"schema": [list(r) for r in info], "row": data},
                         ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return payload, row, names


def _identity(db):
    stat = db.stat()
    # File identity prevents a ticket for one ledger being applied to another.
    # This is not proof that a network or cloned ledger is exclusively local.
    normalized = os.path.normcase(str(db.absolute())).encode("utf-8")
    return [hashlib.sha256(normalized).hexdigest(), stat.st_dev, stat.st_ino]


def prepare(provider, db, job_id, ticket):
    db, ticket = Path(os.path.abspath(db)), Path(os.path.abspath(ticket))
    if not _safe_file(db) or not _safe_ancestors(ticket.parent):
        raise ValueError("ledger unavailable")
    identity = _identity(db)
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
        con.execute("BEGIN")
        if not _safe_file(db) or identity != _identity(db):
            raise ValueError("ledger changed")
        payload, _, _ = _snapshot(con, provider, job_id)
        if not _safe_file(db) or identity != _identity(db):
            raise ValueError("ledger changed")
    if not _safe_file(db) or identity != _identity(db):
        raise ValueError("ledger changed")
    ticket_data = {"version": 1, "provider": provider, "job_id": job_id,
                   "ledger_identity": identity,
                   "uptime_ms": _uptime_ms(),
                   "row_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                   "operation_id": uuid.uuid4().hex}
    fd = os.open(str(ticket), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(ticket_data, stream, ensure_ascii=True)
        stream.flush()
        os.fsync(stream.fileno())


def release(db, ticket, *, same_machine_local, stopped_dispatchers,
            full_restart, cli_exited, updated_only):
    if not all(v is True for v in (same_machine_local, stopped_dispatchers, full_restart, cli_exited, updated_only)):
        raise ValueError("operator attestations required")
    db, ticket = Path(os.path.abspath(db)), Path(os.path.abspath(ticket))
    if not _safe_file(db) or not _safe_file(ticket):
        raise ValueError("ledger or ticket unavailable")
    if ticket.stat().st_size > 8192:
        raise ValueError("ticket too large")
    record = json.loads(ticket.read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("version") != 1 or
        record.get("ledger_identity") != _identity(db) or
        not re.fullmatch(r"[a-f0-9]{32}", record.get("operation_id", "")) or
        not re.fullmatch(r"[a-f0-9]{64}", record.get("row_hash", "")) or
        type(record.get("uptime_ms")) is not int or record["uptime_ms"] <= 0):
        raise ValueError("ticket mismatch")
    if _uptime_ms() >= record["uptime_ms"]:
        raise ValueError("no corroborating uptime reset")
    provider, job_id = record.get("provider"), record.get("job_id")
    con = sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, timeout=5)
    try:
        con.execute("BEGIN IMMEDIATE")
        # Detect replacement between the initial check and transaction.
        if not _safe_file(db) or record["ledger_identity"] != _identity(db):
            raise ValueError("ledger changed")
        payload, row, names = _snapshot(con, provider, job_id)
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != record["row_hash"]:
            raise ValueError("job changed since snapshot")
        # Retain only replaced control fields and the row digest. Partial
        # provider output remains in its original result file/JSON.
        con.execute("CREATE TABLE IF NOT EXISTS legacy_reconcile_audit "
                    "(operation_id TEXT PRIMARY KEY, provider TEXT NOT NULL, job_id TEXT NOT NULL, "
                    "attestation TEXT NOT NULL, prior_control TEXT NOT NULL, row_hash TEXT NOT NULL)")
        audit_info = con.execute("PRAGMA table_xinfo(legacy_reconcile_audit)").fetchall()
        if ([r[1] for r in audit_info] != ["operation_id", "provider", "job_id", "attestation", "prior_control", "row_hash"]
                or any(r[6] != 0 for r in audit_info)):
            raise ValueError("unsupported audit schema")
        if provider == "claude":
            prior = {"status": _encoded(row[names.index("status")]), "error": _encoded(row[names.index("error")])}
        else:
            original = json.loads(row[names.index("result_json")])
            prior = {key: original.get(key) for key in ("status", "error", "local_exit_confirmed", "release_basis") if key in original}
            prior["state"] = row[names.index("state")]
        con.execute("INSERT INTO legacy_reconcile_audit VALUES (?,?,?,?,?,?)",
                    (record["operation_id"], provider, job_id, "operator_confirmed",
                     json.dumps(prior, ensure_ascii=True), record["row_hash"]))
        if provider == "claude":
            changed = con.execute("UPDATE jobs SET status='operator_released', error='operator_released' "
                                  "WHERE id=? AND status=?", (job_id, row[names.index("status")])).rowcount
        else:
            body = json.loads(row[names.index("result_json")])
            body.update(status="submission_uncertain", job_id=job_id, error="operator_released",
                        local_exit_confirmed=False, release_basis="operator_confirmed")
            changed = con.execute("UPDATE jobs SET state='operator_released', result_json=? "
                                  "WHERE job_id=? AND state=? AND result_json IS ?",
                                  (json.dumps(body, ensure_ascii=True), job_id,
                                   row[names.index("state")], row[names.index("result_json")])).rowcount
        if changed != 1:
            raise ValueError("job changed")
        con.commit()
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=(
        "Recover a legacy slot only after all other tasks finish naturally, all old "
        "dispatch services and their automatic restarts are disabled, and this "
        "same machine is fully restarted (not sleep or fast-start shutdown). "
        "The ledger must be private, local and not shared with any other machine. "
        "Install the new version before recovery and permanently prevent old "
        "dispatchers and cached old MCP services from accessing this ledger; "
        "after recovery only the new version may use it. The operator must "
        "confirm the specific CLI has exited. These facts are operator attestations; "
        "this program does not observe or verify them. A remote provider may still "
        "be running and its result remains uncertain. No request is resent. "
        "Windows/Linux uptime must have decreased; this rejects same-boot recovery "
        "but does not prove a restart, since virtual-machine rollback or a different "
        "host can also change uptime. Other platforms are unsupported. "
        "Store the ledger and ticket in an OS-protected private directory; on "
        "Windows this command does not verify access permissions. File identity "
        "checks are not protection against a malicious account with write access."))
    sub = parser.add_subparsers(dest="mode", required=True)
    before = sub.add_parser("prepare", help="Take a read-only snapshot of one blocking record")
    before.add_argument("provider", choices=("claude", "grok"))
    before.add_argument("ledger")
    before.add_argument("job_id")
    before.add_argument("ticket")
    after = sub.add_parser("release", help="Release one slot after an operator-attested restart")
    after.add_argument("ledger")
    after.add_argument("ticket")
    after.add_argument("--attest-same-machine-local", action="store_true")
    after.add_argument("--attest-dispatchers-stopped", action="store_true")
    after.add_argument("--attest-full-restart", action="store_true")
    after.add_argument("--attest-cli-exited", action="store_true")
    after.add_argument("--attest-new-version-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.mode == "prepare":
            prepare(args.provider, args.ledger, args.job_id, args.ticket)
            print("Snapshot saved. No job changed. Keep the ticket private.")
        else:
            release(args.ledger, args.ticket, same_machine_local=args.attest_same_machine_local,
                    stopped_dispatchers=args.attest_dispatchers_stopped,
                    full_restart=args.attest_full_restart, cli_exited=args.attest_cli_exited,
                    updated_only=args.attest_new_version_only)
            print("Slot released from operator attestations. Provider outcome remains unknown; no task was resent.")
        return 0
    except (ValueError, TypeError, OSError, sqlite3.Error, json.JSONDecodeError):
        print("Operation not confirmed. Inspect the specified ledger and ticket; do not retry automatically.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
