"""Optional, narrowly scoped Claude Code client-session evidence.

This module does not launch a provider. Local session metadata is not a
server attestation of effective effort. Enabling the caller's corresponding
mode may cause the official client to write prompts, answers, paths and other
files; safe deletion of every copy or sidecar cannot be guaranteed.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import uuid

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")
_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def _empty(status="unavailable"):
    return {"client_evidence_status": status, "client_model_matches_requested": None,
            "client_effort_matches_requested": None, "session_cleanup": "not_attempted",
            "server_effort_verified": False, "other_client_artifacts": "not_examined"}


def _inputs(profile_dir, work_dir, session_id):
    if not all(isinstance(value, str) for value in (profile_dir, work_dir, session_id)):
        raise ValueError("invalid input")
    profile, work = Path(profile_dir), Path(work_dir)
    if (not profile.is_absolute() or not work.is_absolute() or
            any(part in (".", "..") for part in profile.parts + work.parts)):
        raise ValueError("invalid path")
    if str(uuid.UUID(session_id)) != session_id:
        raise ValueError("invalid session")
    return profile, work


def _native():
    if os.name != "nt":
        raise OSError("unsupported platform")
    if __package__:
        from . import win_session_handle as handles, win_private_acl as acl
    else:
        import win_session_handle as handles
        import win_private_acl as acl
    return handles, acl


def _hold_chain(path, handles, opened):
    current = Path(path.anchor)
    handle, _ = handles._open(current, directory=True)
    opened.append(handle)
    for part in path.parts[1:]:
        current /= part
        handle, _ = handles._open(current, directory=True)
        opened.append(handle)
    return handle


def _context(profile, work, handles, acl, opened):
    profile_handle = _hold_chain(profile, handles, opened)
    if not acl.protected_private_acl(profile_handle):
        raise ValueError("private profile unavailable")
    work_handle = _hold_chain(work, handles, opened)
    if not acl.protected_private_acl(work_handle):
        raise ValueError("private work unavailable")
    # Resolve only while every component is held without delete sharing and
    # was independently opened with OPEN_REPARSE_POINT.
    resolved_work = work.resolve(strict=True)
    if os.path.normcase(str(resolved_work)) != os.path.normcase(str(work)):
        raise ValueError("noncanonical work")
    projects = profile / "projects"
    project = projects / re.sub(r"[^a-zA-Z0-9]", "-", str(resolved_work))
    for path in (projects, project):
        handle = _hold_chain(path, handles, opened)
        if not acl.protected_private_acl(handle, require_protected=False, allow_inherited=True):
            raise ValueError("private session directory unavailable")
    return project


def _close(handles, opened):
    success = True
    for handle in reversed(opened):
        success = bool(handles._kernel.CloseHandle(handle)) and success
    return success


def prepare_session_evidence(profile_dir, work_dir, session_id, *, enabled=False):
    """Read-only preflight; absence is a snapshot, not a global lock.

    The caller must coordinate its own ledger and dedicated profile. Missing
    directories, unsupported systems, non-private ACLs or uncertainty return
    False; this helper neither creates directories nor inspects other sessions.
    """
    if enabled is not True:
        return False
    opened = []
    handles = None
    ok = False
    try:
        profile, work = _inputs(profile_dir, work_dir, session_id)
        handles, acl = _native()
        project = _context(profile, work, handles, acl, opened)
        get_attributes = handles._kernel.GetFileAttributesW
        get_attributes.argtypes = [ctypes.c_wchar_p]
        get_attributes.restype = ctypes.c_uint32
        ctypes.set_last_error(0)
        value = get_attributes(str(project / (session_id + ".jsonl")))
        ok = value == 0xFFFFFFFF and ctypes.get_last_error() == 2
    except Exception:
        ok = False
    finally:
        if handles is not None and not _close(handles, opened):
            ok = False
    return ok


def _unique_json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=unique)


def _projection(raw, session_id, expected_model, expected_effort):
    """Return only session binding and fixed booleans; never return content."""
    assistants = []
    for line in raw.decode("utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = _unique_json(line)
        if not isinstance(row, dict):
            raise ValueError("invalid record")
        if "sessionId" in row and row["sessionId"] != session_id:
            raise ValueError("session mismatch")
        if row.get("type") == "assistant":
            if row.get("sessionId") != session_id:
                raise ValueError("session mismatch")
            assistants.append(row)
    if not assistants:
        raise ValueError("no bound assistant")
    model_ok = all(isinstance(row.get("message"), dict) and
                   row["message"].get("model") == expected_model for row in assistants)
    effort_ok = all(row.get("effort") == expected_effort for row in assistants)
    return model_ok, effort_ok


def collect_session_evidence(profile_dir, work_dir, session_id, expected_model, expected_effort,
                             *, process_exit_confirmed, enabled=False, durable_result_saved=False):
    """Read only one client session; delete only after durable result commit.

    Caller must already bind this UUID to its own successful final result;
    this function checks client session records, not the final result or
    server-side execution. The caller must set durable_result_saved=True only
    after the result and terminal state have been durably committed. Unknown,
    unsafe or incomplete data stays unknown.
    """
    out = _empty("disabled" if enabled is not True else "unavailable")
    if enabled is not True or process_exit_confirmed is not True:
        return out
    opened = []
    handles = None
    try:
        if not isinstance(expected_model, str) or not _MODEL.fullmatch(expected_model):
            return out
        if expected_effort not in _EFFORTS:
            return out
        profile, work = _inputs(profile_dir, work_dir, session_id)
        handles, acl = _native()
        project = _context(profile, work, handles, acl, opened)
        projects = profile / "projects"
        required = {profile: True, projects: False, project: False}

        def directory_ok(path, handle):
            if path not in required:
                return True
            protected = required[path]
            return acl.protected_private_acl(handle, require_protected=protected,
                                              allow_inherited=not protected)

        def file_ok(handle):
            return acl.protected_private_acl(handle, require_protected=False, allow_inherited=True)

        projection = []
        def bound_record(raw):
            try:
                projection.append(_projection(raw, session_id, expected_model, expected_effort))
                # Rejecting before SetFileInformationByHandle retains the
                # session while still allowing its safe in-memory projection.
                return durable_result_saved is True
            except (TypeError, ValueError, UnicodeError):
                return False

        out["session_cleanup"] = "unconfirmed"
        usable_projection = False
        try:
            handles.read_then_mark_exact_session_for_deletion(project, session_id,
                validate_before_delete=bound_record, validate_directory=directory_ok, validate_file=file_ok)
        except handles.UnsafeSessionFile as error:
            if str(error) in {"session_identity_unconfirmed", "session_too_large", "file_acl_unconfirmed",
                              "directory_acl_unconfirmed", "reparse_point", "identity_or_links_unconfirmed",
                              "unexpected_file_type"}:
                out["session_cleanup"] = "retained"
            if (str(error) == "session_identity_unconfirmed" and durable_result_saved is not True
                    and len(projection) == 1):
                usable_projection = True
            else:
                raise
        else:
            out["session_cleanup"] = "deletion_requested"
            usable_projection = True
        if usable_projection and len(projection) == 1 and projection[0] == (True, True):
            out.update(client_evidence_status="matched", client_model_matches_requested=True,
                       client_effort_matches_requested=True)
    except Exception:
        # Avoid paths, IDs, filesystem diagnostics and session content.
        pass
    finally:
        if handles is not None and not _close(handles, opened):
            out["session_cleanup"] = "unconfirmed"
            out["client_evidence_status"] = "unavailable"
            out["client_model_matches_requested"] = None
            out["client_effort_matches_requested"] = None
    return out
