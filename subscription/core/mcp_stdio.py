"""Minimal stdio MCP front end for the portable ledger.

The standalone entry point has no provider adapters. The main ai_team.py
entry point supplies them and uses a separate local state directory.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

from coordinator import Coordinator, InputError


TOOLS = [
    {"name": "collab_status", "description": "Show adapter configuration; not proof of login or billing.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "collab_submit", "description": "Reserve a request and ask its adapter to submit it once.",
     "inputSchema": {"type": "object", "properties": {
         "provider": {"type": "string", "enum": ["claude", "grok"]},
         "task": {"type": "string", "minLength": 1, "maxLength": 100000},
         "request_key": {"type": "string", "minLength": 1, "maxLength": 200}},
         "required": ["provider", "task", "request_key"], "additionalProperties": False}},
    {"name": "collab_check", "description": "Read an existing request; never submit it again.",
     "inputSchema": {"type": "object", "properties": {"request_key": {"type": "string"}},
                     "required": ["request_key"], "additionalProperties": False}},
    {"name": "collab_result", "description": "Query the existing provider job and return available output without re-submitting.",
     "inputSchema": {"type": "object", "properties": {"provider": {"type": "string", "enum": ["claude", "grok"]},
         "request_key": {"type": "string"}}, "required": ["provider", "request_key"], "additionalProperties": False}},
]


def _tool_call(core: Coordinator, name: str, arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise InputError("arguments must be an object")
    if name == "collab_status" and not arguments:
        return core.status()
    if name == "collab_submit" and set(arguments) == {"provider", "task", "request_key"}:
        return core.submit(**arguments)
    if name == "collab_check" and set(arguments) == {"request_key"}:
        return core.get(**arguments)
    if name == "collab_result" and set(arguments) == {"provider", "request_key"}:
        return core.result(**arguments)
    raise InputError("unknown tool or invalid arguments")


def handle(core: Coordinator, message: object) -> dict | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method = message["method"]
    if "id" not in message:
        return None
    request_id = message["id"]
    params = message.get("params", {})
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Invalid params"}}
    try:
        if method == "initialize":
            requested = params.get("protocolVersion")
            supported = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
            result = {"protocolVersion": requested if requested in supported else "2025-03-26",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "local-collaboration", "version": "0.1.0"},
                      "instructions": ("Coordinate the user's task: perform substantive work yourself, "
                          "assign suitable independent work or verification to Claude and Grok, and retrieve "
                          "and assess their actual results. Share only task-relevant information, use a unique "
                          "request key for each task, and never automatically resubmit uncertain requests. "
                          "If a provider is unavailable, continue feasible work and report the missing "
                          "collaboration; do not treat login, configuration, or a queued job as a verified result. "
                          "The user manages provider accounts and billing. Do not claim a particular model, "
                          "reasoning strength, billing channel, or execution unless supported by evidence.")}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            output = _tool_call(core, params.get("name"), params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=True)}],
                      "structuredContent": output, "isError": False}
        else:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    except InputError:
        result = {"content": [{"type": "text", "text": "Invalid request or conflicting request key."}], "isError": True}
    except Exception:
        # Never echo unknown exceptions, tasks, or local paths to the client.
        result = {"content": [{"type": "text", "text": "Operation failed; submission status may be uncertain."}], "isError": True}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(core: Coordinator, stdin, stdout, *, per_provider: int = 2, control_workers: int = 2) -> None:
    """Handle tool calls concurrently so one slow adapter cannot block status.

    Workers are bounded, in-process and daemonized, not durable provider jobs.
    On process death an existing dispatch reservation stays uncertain. This
    preview does not cancel or resubmit it. No worker may report receipt before
    its adapter actually provides a valid receipt.
    """
    # A saturated provider must not consume another provider's dispatch slots
    # or the independent status/check capacity.
    submit_slots = {name: threading.BoundedSemaphore(per_provider) for name in ("claude", "grok")}
    result_slots = {name: threading.BoundedSemaphore(per_provider) for name in ("claude", "grok")}
    control_slots = threading.BoundedSemaphore(control_workers)
    output_lock = threading.Lock()
    workers = []

    def write(response):
        if response is not None:
            with output_lock:
                stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n")
                stdout.flush()

    def work(message, slots):
        try:
            write(handle(core, message))
        finally:
            slots.release()

    for line in stdin:
        if len(line) > 500_000:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request too large"}}
        else:
            try:
                message = json.loads(line)
                if (isinstance(message, dict) and message.get("method") == "tools/call"
                        and "id" in message):
                    params = message.get("params", {})
                    arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
                    provider = arguments.get("provider") if isinstance(arguments, dict) else None
                    tool_name = params.get("name") if isinstance(params, dict) else None
                    if isinstance(provider, str) and provider in submit_slots and tool_name == "collab_submit":
                        slots = submit_slots[provider]
                    elif isinstance(provider, str) and provider in result_slots and tool_name == "collab_result":
                        slots = result_slots[provider]
                    else:
                        slots = control_slots
                    if not slots.acquire(blocking=False):
                        response = {"jsonrpc": "2.0", "id": message["id"], "error": {
                            "code": -32000, "message": "Server busy; this call was not processed."}}
                    else:
                        worker = threading.Thread(target=work, args=(message, slots), daemon=True)
                        try:
                            worker.start()
                        except Exception:
                            slots.release()
                            response = {"jsonrpc": "2.0", "id": message["id"], "error": {
                                "code": -32000, "message": "Worker unavailable; this call was not processed."}}
                        else:
                            workers = [item for item in workers if item.is_alive()]
                            workers.append(worker)
                            continue
                else:
                    response = handle(core, message)
            except (ValueError, TypeError):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        write(response)
    # stdin can close while worker calls are still in flight. Allow quick
    # replies to flush, then exit without assuming that slow submits failed.
    deadline = time.monotonic() + 1
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline collaboration MCP preview; no provider adapters are included.")
    parser.add_argument("--db", required=True, help="Path for the local SQLite request ledger")
    args = parser.parse_args()
    core = Coordinator(args.db)
    # MCP stdio is UTF-8 even when Windows uses a different console codepage.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    serve(core, sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
