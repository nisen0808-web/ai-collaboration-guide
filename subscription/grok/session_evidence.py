"""Read tightly bound Grok client metadata; never open conversation files.

An official client session may contain the complete task and answer regardless
of this reader. Evidence is local client metadata, not a signed server claim.
No directories, files, sessions or provider requests are created or deleted.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote
import uuid

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
_MAX_BYTES = 1_000_000


def _empty(status="unavailable"):
    return {"client_evidence_status": status, "client_model_matches_requested": None,
            "client_effort_matches_requested": None, "observed_model": None,
            "observed_effort": None, "evidence_source": "official_grok_client_session",
            "server_effort_verified": False}


def _unique_json(raw):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out
    def invalid_constant(_):
        raise ValueError("invalid constant")
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique,
                       parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("invalid json")
    return value


def _usage_models(usage):
    """Use explicit model identifiers only; absent or malformed is unknown."""
    models = []
    used_record_present = False
    for obj in (usage, usage.get("session", {})):
        if not isinstance(obj, dict):
            raise ValueError("invalid usage")
        for key in ("modelId", "primaryModelId"):
            if key in obj:
                if not isinstance(obj[key], str) or not _MODEL.fullmatch(obj[key]):
                    raise ValueError("invalid model")
                models.append(obj[key])
        if "modelUsage" in obj:
            data = obj["modelUsage"]
            if not isinstance(data, dict) or not data:
                raise ValueError("invalid usage")
            for model, counters in data.items():
                if not isinstance(model, str) or not _MODEL.fullmatch(model) or not isinstance(counters, dict):
                    raise ValueError("invalid usage")
                values = [counters[key] for key in ("inputTokens", "outputTokens", "totalTokens") if key in counters]
                if any(type(value) is not int or value < 0 for value in values):
                    raise ValueError("invalid token count")
                used_record_present = used_record_present or any(value > 0 for value in values)
                models.append(model)
    signals = usage.get("signals", {})
    if not isinstance(signals, dict):
        raise ValueError("invalid usage")
    if "modelsUsed" in signals:
        value = signals["modelsUsed"]
        if not isinstance(value, list) or not value or not all(isinstance(x, str) and _MODEL.fullmatch(x) for x in value):
            raise ValueError("invalid usage")
        models.extend(value)
    if not models or not used_record_present:
        raise ValueError("usage missing model")
    return models


def _projection(summary_raw, usage_raw, session_id, work, expected_model, expected_effort):
    summary, usage = _unique_json(summary_raw), _unique_json(usage_raw)
    info = summary.get("info")
    if not isinstance(info, dict) or info.get("id") != session_id or not isinstance(info.get("cwd"), str):
        raise ValueError("session binding unavailable")
    if os.path.normcase(os.path.normpath(info["cwd"])) != os.path.normcase(os.path.normpath(str(work))):
        raise ValueError("working directory mismatch")
    if "sessionId" in usage and usage["sessionId"] != session_id:
        raise ValueError("usage binding mismatch")
    session_usage = usage.get("session", {})
    if isinstance(session_usage, dict) and "sessionId" in session_usage and session_usage["sessionId"] != session_id:
        raise ValueError("usage binding mismatch")
    observed, effort = summary.get("current_model_id"), summary.get("reasoning_effort")
    if not isinstance(observed, str) or not _MODEL.fullmatch(observed) or effort not in _EFFORTS:
        raise ValueError("metadata unavailable")
    allowed = {expected_model}
    if expected_model == "grok-4.7":
        allowed.add("grok-4.7-build")  # Only this previously observed exact alias.
    model_matches = observed in allowed and all(item in allowed for item in _usage_models(usage))
    effort_matches = effort == expected_effort
    out = _empty("matched" if model_matches and effort_matches else "mismatch")
    out.update(client_model_matches_requested=model_matches,
               client_effort_matches_requested=effort_matches,
               observed_model=observed if observed in allowed else None,
               observed_effort=effort)
    return out


def _inputs(profile_dir, work_dir, session_id):
    if not all(isinstance(x, str) for x in (profile_dir, work_dir, session_id)):
        raise ValueError("invalid input")
    profile, work = Path(profile_dir), Path(work_dir)
    if not profile.is_absolute() or not work.is_absolute() or any(part in (".", "..") for part in profile.parts + work.parts):
        raise ValueError("invalid paths")
    if str(uuid.UUID(session_id)) != session_id:
        raise ValueError("invalid session")
    # UNC/device roots and CLI's long-cwd slug/hash scheme are deliberately
    # unsupported until their exact official encoding can be verified.
    if os.name == "nt" and (not re.fullmatch(r"[A-Za-z]:\\", profile.anchor) or
                            not re.fullmatch(r"[A-Za-z]:\\", work.anchor)):
        raise ValueError("unsupported root")
    encoded = quote(str(work), safe="")
    if len(encoded.encode("ascii")) > 255:
        raise ValueError("unsupported cwd encoding")
    return profile, work, encoded


def _native():
    if os.name != "nt":
        raise OSError("unsupported platform")
    # A detached Grok worker can be executed as a script, so ensure the
    # sibling package is available without importing a real user profile.
    parent = str(Path(__file__).resolve().parents[1])
    if parent not in sys.path:
        sys.path.insert(0, parent)
    from claude import session_evidence as ce
    handles, acl = ce._native()
    return ce, handles, acl


def _posix_reader():
    if not sys.platform.startswith("linux"):
        raise OSError("unsupported platform")
    if __package__:
        from . import posix_session_reader as reader
    else:
        import posix_session_reader as reader
    return reader


def _extended(path):
    return Path("\\\\?\\" + str(path))


def _context(profile, work, group, ce, handles, acl, opened):
    ph = ce._hold_chain(_extended(profile), handles, opened)
    if not acl.protected_private_acl(ph):
        raise ValueError("private profile unavailable")
    wh = ce._hold_chain(_extended(work), handles, opened)
    if not acl.protected_private_acl(wh, require_protected=False, allow_inherited=True):
        raise ValueError("private work unavailable")
    # Hold checked chains while establishing the exact cwd representation.
    if (os.path.normcase(str(work.resolve(strict=True))) != os.path.normcase(str(work)) or
            os.path.normcase(str(profile.resolve(strict=True))) != os.path.normcase(str(profile))):
        raise ValueError("noncanonical cwd")
    return profile / "sessions", profile / "sessions" / group


def _attributes(path, handles):
    get = handles._kernel.GetFileAttributesW
    get.argtypes = [ctypes.c_wchar_p]
    get.restype = ctypes.c_uint32
    ctypes.set_last_error(0)
    value = get(str(_extended(path)))
    if value == 0xFFFFFFFF:
        error = ctypes.get_last_error()
        if error in (2, 3):
            return None
        raise OSError("attributes unavailable")
    if value & 0x400:
        raise ValueError("reparse point")
    return value


def _hold_private_dir(path, ce, handles, acl, opened):
    handle = ce._hold_chain(_extended(path), handles, opened)
    if not acl.protected_private_acl(handle, require_protected=False, allow_inherited=True):
        raise ValueError("private session unavailable")


def preflight(profile_dir, work_dir, session_id):
    """Snapshot absence before launch. The caller coordinates its own job.

    Absence is not a global lock against other programs running as the same
    account; the UUID and caller's exclusive private profile remain essential.
    """
    opened, handles, ce, ok = [], None, None, False
    try:
        profile, work, group = _inputs(profile_dir, work_dir, session_id)
        if os.name != "nt":
            return _posix_reader().preflight(profile_dir, work_dir, session_id) is True
        ce, handles, acl = _native()
        sessions, parent = _context(profile, work, group, ce, handles, acl, opened)
        for path in (sessions, parent):
            if _attributes(path, handles) is None:
                ok = True
                break
            _hold_private_dir(path, ce, handles, acl, opened)
        else:
            ok = _attributes(parent / session_id, handles) is None
    except Exception:
        ok = False
    finally:
        if handles is not None and not ce._close(handles, opened):
            ok = False
    return ok


def _read_exact(path, handles, acl, opened):
    """No delete permission; deny write/delete sharing and reject hard links."""
    kernel = handles._kernel
    flags = handles._OPEN_REPARSE_POINT
    handle = kernel.CreateFileW(str(_extended(path)), handles._READ | handles._READ_CONTROL,
                                handles._SHARE_READ, None, handles._OPEN_EXISTING, flags, None)
    if handle == handles._INVALID:
        raise OSError("file unavailable")
    opened.append(handle)
    info = handles._Info()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise OSError("identity unavailable")
    if (info.attributes & (handles._REPARSE | handles._DIRECTORY) or info.links != 1 or
            not (info.index_high or info.index_low) or
            not acl.protected_private_acl(handle, require_protected=False, allow_inherited=True)):
        raise ValueError("file safety unavailable")
    size = (info.size_high << 32) | info.size_low
    if not 0 < size <= _MAX_BYTES:
        raise ValueError("invalid size")
    data = bytearray()
    while len(data) < size:
        amount = min(65536, size - len(data))
        buf = ctypes.create_string_buffer(amount)
        count = ctypes.c_uint32()
        if not kernel.ReadFile(handle, buf, amount, ctypes.byref(count), None) or not count.value:
            raise OSError("read unavailable")
        data.extend(buf.raw[:count.value])
    return bytes(data)


def collect(profile_dir, work_dir, session_id, expected_model, expected_effort,
            *, preflight_ok, process_exit_confirmed):
    """Read only matching summary/usage after confirmed successful exit.

    preflight_ok and process_exit_confirmed must be true only for this exact
    new caller-owned job. A mismatch or any unsafe/unknown input fails closed.
    """
    out = _empty()
    if preflight_ok is not True or process_exit_confirmed is not True:
        return out
    opened, handles, ce = [], None, None
    try:
        if not isinstance(expected_model, str) or not _MODEL.fullmatch(expected_model) or expected_effort not in _EFFORTS:
            return out
        profile, work, group = _inputs(profile_dir, work_dir, session_id)
        if os.name != "nt":
            result = _posix_reader().read_records(profile_dir, work_dir, session_id, max_bytes=_MAX_BYTES)
            if not isinstance(result, tuple) or len(result) != 2:
                return out
            return _projection(result[0], result[1], session_id, work, expected_model, expected_effort)
        ce, handles, acl = _native()
        sessions, parent = _context(profile, work, group, ce, handles, acl, opened)
        for path in (sessions, parent, parent / session_id):
            _hold_private_dir(path, ce, handles, acl, opened)
        summary = _read_exact(parent / session_id / "summary.json", handles, acl, opened)
        usage = _read_exact(parent / session_id / "usage.json", handles, acl, opened)
        out = _projection(summary, usage, session_id, work, expected_model, expected_effort)
    except Exception:
        out = _empty()
    finally:
        if handles is not None and not ce._close(handles, opened):
            out = _empty()
    return out
