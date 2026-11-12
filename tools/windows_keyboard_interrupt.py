#!/usr/bin/env python3
"""Synthetic Windows Ctrl+C check. No provider calls or credentials are used.

Run with --scenario normal or startup_failure, then physically press Ctrl+C
ONCE in the new window after READY. startup_failure_no_signal needs no input.
SIGINT alone cannot prove keyboard origin: the user must confirm that separately.
Each launch writes evidence in a unique temporary directory and exits promptly.
"""
import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

SCENARIOS = ("normal", "startup_failure", "startup_failure_no_signal")
ROOT = Path(__file__).resolve().parents[1]
EARLY = "synthetic completed: \u4e2d\u6587"
CONTROL = "synthetic completed after start failure"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def source_hash():
    return hashlib.sha256((ROOT / "collaborate.py").read_bytes()).hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="ascii")


def minimal_environment():
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "COMSPEC"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def validate(scenario, code, result, meta, stderr, expected_hash):
    """Validate captured evidence; this does not certify who sent SIGINT."""
    control = scenario == "startup_failure_no_signal"
    require(scenario in SCENARIOS, "unknown scenario")
    require(code == (1 if control else 130), "unexpected exit code")
    require(meta["sigint_count"] == (0 if control else 1), "unexpected SIGINT count")
    require(meta["network_attempts"] == 0, "network path was reached")
    require(meta["source_sha256"] == expected_hash, "source changed")
    require(meta["start_attempts"] == 2, "unexpected worker start count")
    require(meta["second_start_failed"] == (scenario != "normal"), "start failure mismatch")
    require(sorted(meta["provider_calls"]) == (["claude", "grok"] if scenario == "normal" else ["claude"]),
            "unexpected provider call count")
    require(not stderr, "worker stderr was not empty")
    providers = result["providers"]
    require(set(providers) == {"claude", "grok"}, "unexpected providers")
    for name, value in providers.items():
        if name == "claude" and scenario != "startup_failure":
            require(value == {"status": "complete_unreviewed", "output": CONTROL if control else EARLY},
                    "completed result was not retained")
        else:
            require(value == {"status": "submission_uncertain", "retry": "not_attempted",
                              "reason": "worker_start_unconfirmed" if control else "interrupted_submission_status_unknown"},
                    "unfinished result or retry policy mismatch")
    require("No retry was attempted." in result["note"], "missing retry disclosure")
    require(all(secret not in json.dumps(result) for secret in ("sk-ant-synthetic", "xai-synthetic")),
            "synthetic credential leaked")


def worker(args):
    run = Path(args.run_dir)
    require(source_hash() == args.source_sha256, "source changed before worker start")
    require(ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False), "cannot enable Ctrl+C")
    meta = {"sigint_count": 0, "network_attempts": 0, "provider_calls": [],
            "start_attempts": 0, "second_start_failed": False, "source_sha256": args.source_sha256}

    def interrupt(_signum, _frame):
        meta["sigint_count"] += 1
        meta["sigint_monotonic"] = time.monotonic()
        (run / "signal_received").write_text(str(meta["sigint_count"]), encoding="ascii")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupt)
    os.environ.update(CLAUDE_MODEL="synthetic-model", GROK_MODEL="synthetic-model",
                      COLLAB_ANTHROPIC_KEY="sk-ant-synthetic", COLLAB_XAI_KEY="xai-synthetic")
    sys.path.insert(0, str(ROOT))
    import collaborate as app
    sys.stdin = io.StringIO("synthetic task")
    started, release = threading.Event(), threading.Event()
    control = args.scenario == "startup_failure_no_signal"

    def forbidden_network(*_args, **_kwargs):
        meta["network_attempts"] += 1
        raise RuntimeError("network disabled in synthetic test")

    def provider(name, *_args):
        meta["provider_calls"].append(name)
        if name == "claude" and args.scenario == "normal":
            return {"status": "complete_unreviewed", "output": EARLY}
        started.set()
        release.wait(args.timeout + 30)
        return {"status": "complete_unreviewed", "output": CONTROL if control else "late"}

    real_wait, real_start = app._wait_for_workers, threading.Thread.start

    def ready_then_wait(workers):
        if args.scenario == "normal":
            workers[0].join(timeout=5)
            require(not workers[0].is_alive(), "first worker did not finish")
        require(started.wait(5), "synthetic slow worker did not start")
        if args.scenario != "normal":
            require(meta["second_start_failed"] and workers[0].is_alive(), "startup failure not ready")
        (run / "ready").write_text("ready", encoding="ascii")
        if control:
            release.set()
        real_wait(workers)

    def start(thread):
        meta["start_attempts"] += 1
        if args.scenario != "normal" and meta["start_attempts"] == 2:
            meta["second_start_failed"] = True
            raise RuntimeError("synthetic second worker start failure")
        return real_start(thread)

    app.request_json, app.run_provider = forbidden_network, provider
    app._wait_for_workers, threading.Thread.start = ready_then_wait, start
    try:
        return app.main(["--confirm-paid", "--stdin-utf8"])
    finally:
        threading.Thread.start = real_start
        write_json(run / "worker.json", meta)


def console_pids():
    items = (ctypes.c_ulong * 32)()
    count = ctypes.windll.kernel32.GetConsoleProcessList(items, len(items))
    require(0 < count <= len(items), "console process list unavailable")
    return set(items[:count])


def supervisor(args):
    run = Path(args.run_dir)
    # Explicit console output is necessary because the launching shell may capture stdout.
    with open("CONOUT$", "w", encoding="ascii", errors="backslashreplace", buffering=1) as display:
        kernel = ctypes.windll.kernel32
        require(kernel.SetConsoleCtrlHandler(None, True), "cannot protect supervisor from Ctrl+C")
        kernel.SetConsoleTitleW("AI-COLLAB KEYBOARD TEST - " + args.scenario + " - " + run.name)
        summary = {"scenario": args.scenario, "passed": False, "synthetic_only": True,
                   "keyboard_origin": "not_applicable" if args.scenario == SCENARIOS[2] else "requires_user_confirmation",
                   "source_sha256": args.source_sha256}
        child = None
        stage = "console_preflight"
        try:
            import msvcrt
            require(console_pids() == {os.getpid()}, "test console is not isolated")
            with open("CONIN$", "rb", buffering=0) as console_input:
                mode = ctypes.c_ulong()
                handle = ctypes.c_void_p(msvcrt.get_osfhandle(console_input.fileno()))
                require(kernel.GetConsoleMode(handle, ctypes.byref(mode)), "console input unavailable")
                require(mode.value & 1, "processed console input is disabled")
            summary.update(isolation_checked_before_launch=True, processed_input_verified=True,
                           isolation_checked_at_ready=False)
            stage = "worker_start"
            command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker",
                       "--scenario", args.scenario, "--run-dir", str(run),
                       "--source-sha256", args.source_sha256, "--timeout", str(args.timeout)]
            child = subprocess.Popen(command, env=minimal_environment(), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 10
            while not (run / "ready").exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            require((run / "ready").exists(), "worker did not become ready")
            if args.scenario != SCENARIOS[2]:
                time.sleep(0.3)
                require(console_pids() == {os.getpid(), child.pid}, "unexpected process shares test console")
                require(child.poll() is None, "worker exited before keyboard input")
                summary["isolation_checked_at_ready"] = True
                print("READY: Press Ctrl+C ONCE in THIS test window now. Do not select text.", file=display)
                print("Synthetic workers only. No network or real credentials. Timeout: %s seconds." % args.timeout,
                      file=display)
            else:
                print("CONTROL: No key press required.", file=display)
            stage = "awaiting_result"
            out, err = child.communicate(timeout=args.timeout)
            exit_observed = time.monotonic()
            (run / "stdout.json").write_bytes(out)
            (run / "stderr.txt").write_bytes(err)
            result = json.loads(out.decode("ascii"))
            meta = json.loads((run / "worker.json").read_text(encoding="ascii"))
            stage = "validation"
            validate(args.scenario, child.returncode, result, meta, err, args.source_sha256)
            if args.scenario != SCENARIOS[2]:
                latency = exit_observed - meta["sigint_monotonic"]
                require(0 <= latency <= 5, "interrupt exit latency exceeded five seconds")
                summary["signal_to_exit_observed_seconds"] = round(latency, 4)
            require(source_hash() == args.source_sha256, "source changed during test")
            summary.update(passed=True, child_exit_code=child.returncode,
                           sigint_count=meta["sigint_count"], provider_calls=meta["provider_calls"],
                           network_attempts=meta["network_attempts"], second_start_failed=meta["second_start_failed"])
        except subprocess.TimeoutExpired:
            summary["error_code"] = "control_timeout" if args.scenario == SCENARIOS[2] else "keyboard_timeout"
        except Exception as error:
            summary.update(error_type=type(error).__name__, error_code=stage + "_failed")
            if stage == "worker_start" and (run / "signal_received").exists():
                summary["error_code"] = "signal_before_ready"
        finally:
            if child is not None and child.poll() is None:
                child.kill()  # Only this supervisor's synthetic child.
                child.wait(timeout=5)
            write_json(run / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=True, indent=2), file=display)
        print("Evidence: " + str(run), file=display)
        print("The launcher printed this folder too. This window closes in 5 seconds.", file=display)
        time.sleep(5)
        return 0 if summary["passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--supervisor", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", help=argparse.SUPPRESS)
    parser.add_argument("--source-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    require(os.name == "nt", "this manual test requires Windows")
    require(5 <= args.timeout <= 300, "timeout must be 5 through 300 seconds")
    if args.worker:
        return worker(args)
    if args.supervisor:
        return supervisor(args)
    run = Path(tempfile.mkdtemp(prefix="ai-collab-keyboard-", dir=args.output_root))
    command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--supervisor",
               "--scenario", args.scenario, "--run-dir", str(run),
               "--source-sha256", source_hash(), "--timeout", str(args.timeout)]
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 1  # Win32 SW_SHOWNORMAL; not exported by subprocess.
    subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup, env=minimal_environment(),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Opened AI-COLLAB KEYBOARD TEST: " + args.scenario)
    print("Evidence directory: " + str(run))
    print("Run summary.json is authoritative; SIGINT origin requires your separate confirmation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
