"""Read-only local boot corroboration, never proof of worker or remote exit.

The Windows marker is a retained Kernel-General startup event, not a wall-clock
estimate. Operator attestations about a full restart and local use still apply.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

_PROVIDER = "Microsoft-Windows-Kernel-General"
_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z")


def _kind() -> str:
    if sys.platform.startswith("linux"):
        return "linux_boot_id"
    if sys.platform == "win32":
        return "windows_event12"
    raise ValueError("unsupported_recovery_platform")


def _valid_uuid(value) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _one(parent, name):
    values = parent.findall(_NS + name)
    if len(values) != 1:
        raise ValueError("boot_evidence_unavailable")
    return values[0]


def _windows_event(record_id: int | None = None) -> dict:
    """Read one local System startup event; discard machine and user fields."""
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    if not root.is_absolute():
        raise ValueError("boot_evidence_unavailable")
    query = f"*[System[Provider[@Name='{_PROVIDER}'] and EventID=12"
    if record_id is not None:
        query += f" and EventRecordID={record_id}"
    query += "]]"
    command = [str(root / "System32" / "wevtutil.exe"), "qe", "System",
               "/q:" + query, "/rd:true", "/c:1", "/f:xml", "/e:Events", "/uni:true"]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                shell=False, timeout=10, check=False)
        if result.returncode or not result.stdout or len(result.stdout) > 65536:
            raise ValueError("boot_evidence_unavailable")
        document = ET.fromstring(result.stdout)
        if document.tag != "Events" or len(document) != 1 or document[0].tag != _NS + "Event":
            raise ValueError("boot_evidence_unavailable")
        event = document[0]
        system = _one(event, "System")
        if (_one(system, "Provider").get("Name") != _PROVIDER or
                _one(system, "EventID").text != "12" or
                _one(system, "Channel").text != "System"):
            raise ValueError("boot_evidence_unavailable")
        number = _one(system, "EventRecordID").text or ""
        if not re.fullmatch(r"[1-9][0-9]{0,19}", number):
            raise ValueError("boot_evidence_unavailable")
        number = int(number)
        if number > 2 ** 64 - 1 or (record_id is not None and number != record_id):
            raise ValueError("boot_evidence_unavailable")
        created = _one(system, "TimeCreated").get("SystemTime")
        starts = [item.text for item in _one(event, "EventData").findall(_NS + "Data")
                  if item.get("Name") == "StartTime"]
        if (not isinstance(created, str) or not _TIME.fullmatch(created) or len(starts) != 1 or
                not isinstance(starts[0], str) or not _TIME.fullmatch(starts[0])):
            raise ValueError("boot_evidence_unavailable")
        return {"record_id": number, "time_created": created, "start_time": starts[0]}
    except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError):
        raise ValueError("boot_evidence_unavailable") from None


def _windows_marker(event: dict) -> dict:
    fields = [_PROVIDER, 12, event["time_created"], event["start_time"]]
    digest = hashlib.sha256(json.dumps(fields, separators=(",", ":")).encode("ascii")).hexdigest()
    return {"kind": "windows_event12", "record_id": event["record_id"], "event_hash": digest}


def snapshot() -> dict:
    kind = _kind()
    if kind == "windows_event12":
        return _windows_marker(_windows_event())
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        raise ValueError("boot_evidence_unavailable") from None
    if not _valid_uuid(boot_id):
        raise ValueError("boot_evidence_unavailable")
    return {"kind": kind, "boot_id": boot_id}


def changed(saved: dict) -> bool:
    kind = _kind()
    if type(saved) is not dict or saved.get("kind") != kind:
        raise ValueError("invalid_boot_evidence")
    if kind == "linux_boot_id":
        if set(saved) != {"kind", "boot_id"} or not _valid_uuid(saved.get("boot_id")):
            raise ValueError("invalid_boot_evidence")
        return snapshot()["boot_id"] != saved["boot_id"]
    if (set(saved) != {"kind", "record_id", "event_hash"} or
            type(saved.get("record_id")) is not int or not 0 < saved["record_id"] < 2 ** 64 or
            not isinstance(saved.get("event_hash"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", saved["event_hash"])):
        raise ValueError("invalid_boot_evidence")
    previous = _windows_event(saved["record_id"])
    if _windows_marker(previous) != saved:
        raise ValueError("boot_evidence_unavailable")
    current = _windows_event()
    if current["record_id"] < previous["record_id"]:
        raise ValueError("boot_evidence_unavailable")
    if current["record_id"] == previous["record_id"]:
        if current != previous:
            raise ValueError("boot_evidence_unavailable")
        return False
    # Repeated delivery of the same startup event is not another startup.
    # Deliberately do not order timestamps: wall clocks can move either way.
    return current["start_time"] != previous["start_time"]
