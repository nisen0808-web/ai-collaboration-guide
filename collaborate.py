#!/usr/bin/env python3
"""Small, opt-in, API-billed Claude/Grok pair. Python >= 3.10; no dependencies.

Set COLLAB_ANTHROPIC_KEY, COLLAB_XAI_KEY, CLAUDE_MODEL and GROK_MODEL privately.
Alternatively pass --ask-keys to enter both keys in an interactive terminal.
Read a nonsensitive UTF-8 task file with: python collaborate.py --task-file TASK --confirm-paid
This is a synchronous demonstration, not an MCP server or a durable job queue.
"""
import argparse
import getpass
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import warnings
from pathlib import Path

MAX_TASK_CHARS = 20000
MAX_RESPONSE_BYTES = 2097152
MAX_ERROR_BYTES = 16384
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def safe_identifier(value):
    """Keep only bounded, printable identifier metadata from a provider."""
    return value if isinstance(value, str) and SAFE_ID.fullmatch(value) else None


def response_request_id(headers):
    """Extract an allowlisted header without exposing arbitrary response headers."""
    if headers is None:
        return None
    return safe_identifier(headers.get("request-id") or headers.get("x-request-id"))


class InvalidProviderResponse(ValueError):
    def __init__(self, reason, request_id=None):
        super().__init__(reason)
        self.reason = reason
        self.request_id = request_id


def sanitized_usage(value):
    """Allowlist numeric usage fields only; do not copy arbitrary server data."""
    if not isinstance(value, dict):
        return None
    counters = ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens",
                "completion_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                "cost_in_usd_ticks", "num_sources_used", "num_server_side_tools_used")
    detail_keys = ("input_tokens_details", "output_tokens_details", "prompt_tokens_details",
                   "completion_tokens_details")
    def valid_number(number):
        return type(number) is int and 0 <= number <= 2**63 - 1
    result = {key: value[key] for key in counters if valid_number(value.get(key))}
    for key in detail_keys:
        details = value.get(key)
        if isinstance(details, dict):
            filtered = {field: details[field] for field in ("cached_tokens", "reasoning_tokens", "text_tokens", "audio_tokens", "image_tokens")
                        if valid_number(details.get(field))}
            if filtered:
                result[key] = filtered
    return result or None


MAX_TASK_BYTES = MAX_TASK_CHARS * 4 + 4


def valid_key(key):
    """Headers must be non-empty printable ASCII, without whitespace or controls."""
    return isinstance(key, str) and 1 <= len(key) <= 4096 and all(33 <= ord(ch) <= 126 for ch in key)


# Commonly issued key prefixes. Used only to catch a key handed to the wrong
# provider; a key carrying neither prefix is still accepted, and a matching
# prefix proves nothing about validity.
KEY_PREFIXES = {"claude": "sk-ant-", "grok": "xai-"}


def key_for_other_provider(provider, key):
    """True when a key carries the other provider's prefix, e.g. entered in swapped order."""
    return any(key.startswith(prefix) for name, prefix in KEY_PREFIXES.items() if name != provider)


def read_task(task_file, stdin_utf8):
    """Read UTF-8 strictly; Windows pipes require explicit source verification."""
    if task_file:
        with Path(task_file).open("rb") as stream:
            raw = stream.read(MAX_TASK_BYTES + 1)
        if len(raw) > MAX_TASK_BYTES:
            raise ValueError("task_too_large")
        return raw.decode("utf-8-sig", errors="strict")
    if sys.stdin.isatty():
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        print("Enter the task on one line and press Enter:", file=sys.stderr, flush=True)
        return sys.stdin.readline(MAX_TASK_CHARS + 1)
    # PowerShell may already have replaced Chinese with ASCII question marks.
    # UTF-8 decoding cannot recover the original text; explicit opt-in only.
    if os.name == "nt" and not stdin_utf8:
        raise ValueError("windows_pipe_requires_explicit_utf8")
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    value = stream.read(MAX_TASK_BYTES + 1)
    if len(value) > MAX_TASK_BYTES:
        raise ValueError("task_too_large")
    return value.decode("utf-8-sig", errors="strict") if isinstance(value, bytes) else value


def reject_preflight(issues):
    """Both providers stay unsubmitted when any local prerequisite fails."""
    result = {name: {"status": "not_submitted", "reason": issues.get(name, "peer_not_ready")}
              for name in ("claude", "grok")}
    print(json.dumps({"providers": result, "note": "No request was sent."}, ensure_ascii=True, indent=2))
    return 2


def ask_keys():
    """Request secrets only with a terminal; never fall back to echoed input."""
    try:
        if not sys.stderr.isatty():
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            claude = getpass.getpass("Claude API key (input hidden): ")
            grok = getpass.getpass("xAI API key (input hidden): ")
        if not claude or not grok:
            return None
        return claude, grok
    except (getpass.GetPassWarning, EOFError, OSError, ValueError, KeyboardInterrupt):
        return None


def redact_keys(value, secrets):
    """Avoid echoing known credentials in data returned by either service."""
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact_keys(item, secrets) for item in value]
    if isinstance(value, dict):
        return {redact_keys(key, secrets): redact_keys(item, secrets) for key, item in value.items()}
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward an authorization header to a redirect destination."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, headers, payload, timeout):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    # This opener has no retry logic. A timeout or broken connection is ambiguous:
    # the provider may already have executed and billed the request.
    with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
        request_id = response_request_id(getattr(response, "headers", None))
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise InvalidProviderResponse("response_too_large", request_id)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidProviderResponse("invalid_json", request_id) from exc
    return data, request_id


def build_request(provider, prompt, model, key, max_tokens):
    common = {"Content-Type": "application/json"}
    if provider == "claude":
        return ("https://api.anthropic.com/v1/messages",
                {**common, "x-api-key": key, "anthropic-version": "2023-06-01"},
                {"model": model, "max_tokens": max_tokens,
                 "messages": [{"role": "user", "content": prompt}]})
    if provider == "grok":
        return ("https://api.x.ai/v1/responses",
                {**common, "Authorization": "Bearer " + key},
                {"model": model, "max_output_tokens": max_tokens, "store": False,
                 "input": [{"role": "user", "content": prompt}]})
    raise ValueError("unknown_provider")


def classify_response(provider, data):
    """Classify without discarding text from an incomplete/refusal response."""
    if provider not in ("claude", "grok"):
        raise ValueError("unknown_provider")
    if not isinstance(data, dict):
        return {"status": "invalid_response", "reason": "non_object_response"}
    texts, refusal_texts = [], []
    if provider == "claude":
        stop = data.get("stop_reason")
        blocks = data.get("content", [])
        if not isinstance(blocks, list):
            return {"status": "invalid_response", "reason": "invalid_content"}
        texts = [b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        if not isinstance(stop, str) or not stop:
            status = "invalid_response"
        elif stop == "end_turn":
            status = "complete_unreviewed"
        elif stop in ("max_tokens", "model_context_window_exceeded"):
            status = "truncated"
        elif stop == "refusal":
            status = "refused"
        else:
            status = "incomplete"
    else:
        stop = data.get("status")
        blocks = data.get("output", [])
        if not isinstance(blocks, list):
            return {"status": "invalid_response", "reason": "invalid_output"}
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "message" and isinstance(b.get("content"), list):
                texts.extend(c.get("text") for c in b["content"]
                             if isinstance(c, dict) and c.get("type") == "output_text")
                refusal_texts.extend(c.get("refusal") for c in b["content"]
                                     if isinstance(c, dict) and c.get("type") == "refusal")
        details = data.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        if not isinstance(stop, str) or not stop:
            status = "invalid_response"
        elif refusal_texts or reason == "content_filter":
            status = "refused"
        elif stop == "completed":
            status = "complete_unreviewed"
        elif stop == "incomplete" and reason in ("max_output_tokens", "max_tokens", "length"):
            status = "truncated"
        elif stop == "failed":
            status = "failed"
        else:
            status = "incomplete"
    texts = [t for t in texts if isinstance(t, str) and t.strip()]
    refusal_texts = [t for t in refusal_texts if isinstance(t, str) and t.strip()]
    if status == "complete_unreviewed" and not texts:
        return {"status": "invalid_response", "reason": "completed_without_text"}
    result = {"status": status}
    if status == "invalid_response":
        result["reason"] = "missing_status"
    if texts or refusal_texts:
        result["output"] = "\n".join(texts + refusal_texts)
    if status in ("truncated", "refused", "incomplete"):
        result["reason"] = safe_identifier(stop) or "unknown"
        if provider == "grok" and safe_identifier(reason):
            result["reason"] = reason
    return result


def parse_response(provider, data):
    """Backward-compatible strict helper; runtime keeps partial text separately."""
    result = classify_response(provider, data)
    if result["status"] != "complete_unreviewed":
        raise ValueError("incomplete_or_no_text")
    return result["output"]


def safe_http_error_type(error, provider):
    """Read at most 16 KiB of a 400 JSON body; return only a fixed, known type.

    The provider message and arbitrary fields are never returned. HTTP errors
    may come from intermediaries, so type is a reported label, not proof of cause.
    """
    allow = {
        "claude": frozenset({"invalid_request_error"}),
        "grok": frozenset({"invalid_request_error"}),
    }
    if error.code != 400 or provider not in allow:
        return None
    try:
        raw = error.read(MAX_ERROR_BYTES + 1)
        if not isinstance(raw, bytes) or len(raw) > MAX_ERROR_BYTES:
            return None
        body = json.loads(raw.decode("utf-8", errors="strict"))
        details = body.get("error") if isinstance(body, dict) else None
        candidate = details.get("type") if isinstance(details, dict) else None
        return candidate if isinstance(candidate, str) and candidate in allow[provider] else None
    except Exception:
        # Error body is untrusted and optional; never expose it or its exceptions.
        return None


def run_provider(provider, prompt, model, key, timeout, max_tokens):
    if provider not in ("claude", "grok"):
        return {"status": "not_submitted", "reason": "invalid_provider"}
    if not key or not model:
        return {"status": "not_submitted", "reason": "missing_credential_or_model"}
    if not isinstance(model, str) or not MODEL.fullmatch(model):
        return {"status": "not_submitted", "reason": "invalid_model_identifier"}
    if not valid_key(key):
        return {"status": "not_submitted", "reason": "missing_or_invalid_credential"}
    if key_for_other_provider(provider, key):
        return {"status": "not_submitted", "reason": "credential_for_other_provider"}
    start = time.monotonic()
    try:
        url, headers, payload = build_request(provider, prompt, model, key, max_tokens)
        received = request_json(url, headers, payload, timeout)
        # Mock adapters can return a plain dict; actual HTTP returns body + request ID.
        response, request_id = received if isinstance(received, tuple) and len(received) == 2 else (received, None)
        result = classify_response(provider, response)
        if isinstance(response, dict):
            result["response_id"] = safe_identifier(response.get("id"))
            result["returned_model"] = safe_identifier(response.get("model"))
            result["usage"] = sanitized_usage(response.get("usage"))
        result.update({"request_id": safe_identifier(request_id), "effort": "not_verified",
                       "elapsed_seconds": round(time.monotonic() - start, 3)})
        return result
    except urllib.error.HTTPError as error:
        # Do not print server error bodies: they may contain user data or tokens.
        status = "submission_uncertain" if error.code == 408 or error.code >= 500 else "failed"
        result = {"status": status, "reason": "http_error", "http_status": error.code,
                  "request_id": response_request_id(error.headers), "retry": "not_attempted"}
        error_type = safe_http_error_type(error, provider)
        if error_type is not None:
            result["error_type"] = error_type
        return result
    except InvalidProviderResponse as error:
        return {"status": "invalid_response", "reason": error.reason,
                "request_id": safe_identifier(error.request_id), "retry": "not_attempted"}
    except (ValueError, UnicodeDecodeError):
        return {"status": "invalid_response", "reason": "incomplete_or_invalid_response",
                "retry": "not_attempted"}
    except Exception:
        # Includes network timeout; do not print exception text or retry.
        return {"status": "submission_uncertain", "reason": "transport_error",
                "retry": "not_attempted"}


def _wait_for_workers(workers):
    # Bounded waits let Python handle Ctrl+C while either provider is slow.
    for worker in workers:
        while worker.is_alive():
            worker.join(timeout=0.1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent Claude and Grok API calls; may incur charges.")
    parser.add_argument("--confirm-paid", action="store_true", help="Confirm sending the task to both paid API services")
    parser.add_argument("--ask-keys", action="store_true", help="Read keys with hidden input from an interactive terminal")
    parser.add_argument("--task-file", help="Read the task from a UTF-8 file")
    parser.add_argument("--stdin-utf8", action="store_true", help="On Windows, assert that piped input is UTF-8 and was not converted by the shell")
    parser.add_argument("--allow-suspect-question-marks", action="store_true", help="Allow intentional consecutive question marks in a Windows pipe after verifying its original text")
    parser.add_argument("--timeout", type=int, default=60, help="Per-socket network timeout in seconds (1–300)")
    parser.add_argument("--max-tokens", type=int, default=512, help="Maximum output tokens per service (1–4096)")
    args = parser.parse_args(argv)
    if not args.confirm_paid:
        parser.print_help(sys.stderr)
        print("No requests sent. Read the README and add --confirm-paid when ready.", file=sys.stderr)
        return 2
    if not 1 <= args.timeout <= 300 or not 1 <= args.max_tokens <= 4096:
        print("Invalid timeout or token limit; no requests sent.", file=sys.stderr)
        return 2
    try:
        task = read_task(args.task_file, args.stdin_utf8)
        # Reject isolated surrogate characters before a network request is sent.
        task.encode("utf-8", errors="strict")
    except (OSError, UnicodeError, ValueError):
        print("Task input invalid or not verified as UTF-8; no requests sent. "
              "On Windows use --task-file or an interactive terminal; "
              "--stdin-utf8 only when the pipe is verified.", file=sys.stderr)
        return 2
    if not task.strip() or len(task) > MAX_TASK_CHARS:
        print("Task is empty or exceeds the input limit; no requests sent.", file=sys.stderr)
        return 2
    # PowerShell 5.1 can replace non-ASCII text before Python receives it.
    # This heuristic is incomplete and must not be presented as proof of UTF-8 integrity.
    if (os.name == "nt" and not args.task_file and not sys.stdin.isatty()
            and "??" in task and not args.allow_suspect_question_marks):
        print("Piped input contains consecutive question marks and may be corrupted; "
              "no requests sent. Use a UTF-8 task file, or verify the original text "
              "and explicitly add --allow-suspect-question-marks.", file=sys.stderr)
        return 2
    claude_model, grok_model = os.getenv("CLAUDE_MODEL"), os.getenv("GROK_MODEL")
    model_issues = {}
    for name, model in (("claude", claude_model), ("grok", grok_model)):
        if not model or not MODEL.fullmatch(model):
            model_issues[name] = "missing_or_invalid_model"
    if model_issues:
        return reject_preflight(model_issues)
    if args.ask_keys:
        keys = ask_keys()
        if keys is None:
            print("A secure interactive terminal and both keys are required; no requests sent.", file=sys.stderr)
            return 2
        claude_key, grok_key = keys
    else:
        # Never use standard provider env vars: these can change CLI billing.
        claude_key, grok_key = os.getenv("COLLAB_ANTHROPIC_KEY"), os.getenv("COLLAB_XAI_KEY")
    key_issues = {}
    for name, key in (("claude", claude_key), ("grok", grok_key)):
        if not valid_key(key):
            key_issues[name] = "missing_or_invalid_credential"
        elif key_for_other_provider(name, key):
            # Sending it would disclose one provider's key to the other.
            key_issues[name] = "credential_for_other_provider"
    if key_issues:
        return reject_preflight(key_issues)
    secrets = [key for key in (claude_key, grok_key) if key]
    if any(key in task for key in secrets):
        print("Task contains a configured key; no requests sent.", file=sys.stderr)
        return 2
    # Daemon threads do not keep the CLI alive after Ctrl+C. Ending the local
    # process does not prove a request was cancelled or that remote work ended.
    # When called as a library, a still-running thread can continue after main
    # returns until the network operation completes or times out.
    providers = {"claude": (claude_model, claude_key), "grok": (grok_model, grok_key)}
    recorded, result_lock, workers = {}, threading.Lock(), []

    def run_and_record(name, model, key):
        try:
            value = run_provider(name, task, model, key, args.timeout, args.max_tokens)
        except BaseException:
            # No traceback or exception text: it may contain credentials.
            value = {"status": "submission_uncertain", "reason": "worker_error", "retry": "not_attempted"}
        with result_lock:
            recorded[name] = value

    interrupted = False
    start_error = False
    try:
        for name, (model, key) in providers.items():
            worker = threading.Thread(target=run_and_record, args=(name, model, key), daemon=True)
            workers.append(worker)
            worker.start()
        _wait_for_workers(workers)
    except KeyboardInterrupt:
        interrupted = True
    except Exception:
        # Thread start/wait errors are not proof that either request did not start.
        start_error = True
    with result_lock:
        # Snapshot into a separate dictionary: a late-running daemon may still
        # record a result after interruption, but cannot alter this output.
        results = {name: recorded.get(name, {"status": "submission_uncertain",
                   "reason": "interrupted_submission_status_unknown" if interrupted else "worker_start_unconfirmed",
                   "retry": "not_attempted"}) for name in providers}
    # ASCII-safe JSON avoids losing completed, potentially billed results when
    # redirected stdout uses a legacy Windows encoding. JSON decoding restores Unicode.
    note = ("Interrupted or local worker unavailable; unfinished requests may have been sent and "
            "remote work may continue. No retry was attempted." if interrupted or start_error else
            "Independent, unreviewed responses. Model effort is not verified; "
            "the coordinating AI must evaluate both results.")
    print(json.dumps(redact_keys({"providers": results, "note": note}, secrets), ensure_ascii=True, indent=2), flush=True)
    if interrupted:
        return 130
    if start_error:
        return 1
    return 0 if all(v["status"] == "complete_unreviewed" for v in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())

