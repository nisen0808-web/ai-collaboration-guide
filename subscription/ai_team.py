"""Run the local Claude/Grok collaboration server or the official login flow."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "core"))

from coordinator import Coordinator  # noqa: E402
from mcp_stdio import serve  # noqa: E402
from claude.durable_claude import DurableClaudeAdapter as ClaudeAdapter  # noqa: E402
from claude import durable_claude as claude_ledger  # noqa: E402
from grok import durable_grok as grok_ledger  # noqa: E402
from claude.readonly_ledger import ReadOnlyClaudeLedger  # noqa: E402
from grok.durable_grok import DurableGrokAdapter as GrokAdapter  # noqa: E402
from claude.claude_adapter import ClaudeAdapter as ClaudeLoginAdapter, valid_session_evidence_model  # noqa: E402
from grok.grok_adapter import GrokAdapter as GrokLoginAdapter  # noqa: E402


def _cli_path(path: str | Path) -> str:
    """Keep launcher links stable while expanding Windows short names when possible."""
    absolute = os.path.abspath(os.fspath(path))
    if os.name != "nt":
        return absolute
    try:
        import ctypes
        from ctypes import wintypes
        convert = ctypes.WinDLL("kernel32", use_last_error=True).GetLongPathNameW
        convert.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        convert.restype = wintypes.DWORD
        size = convert(absolute, None, 0)
        if not size or size > 32768:
            return absolute
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = convert(absolute, buffer, len(buffer))
        return buffer.value if 0 < written < len(buffer) else absolute
    except (OSError, AttributeError, ValueError):
        return absolute


def executable(name: str) -> str | None:
    """Resolve a named CLI or an explicitly supplied executable path."""
    found = shutil.which(name)
    if found:
        return _cli_path(found)
    path = Path(name).expanduser()
    return _cli_path(path) if path.is_file() else None


def installation_executable(name: str) -> tuple[str, bool]:
    """Pin a found client or explicit path; keep an absent bare CLI name."""
    found = executable(name)
    if found:
        return found, True
    path = Path(name).expanduser()
    # An explicitly supplied path must retain its original meaning when the
    # server is started later in another directory, even if not installed yet.
    if path.is_absolute() or len(path.parts) > 1 or name.startswith((".", "~")):
        return _cli_path(path), False
    # A missing bare executable may become available on PATH later.
    return name, False


def codex_registry_available(cli: str, env: dict[str, str]) -> bool:
    """Conservatively inspect existing registrations without exposing them."""
    try:
        completed = subprocess.run([cli, "mcp", "list", "--json"], env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=15,
                                   check=False, shell=False)
        if completed.returncode != 0 or not isinstance(completed.stdout, bytes):
            return False
        if len(completed.stdout) > 1024 * 1024:
            return False

        def distinct_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        servers = json.loads(completed.stdout.decode("utf-8"), object_pairs_hook=distinct_pairs)
        if not isinstance(servers, list):
            return False
        for server in servers:
            if not isinstance(server, dict) or not isinstance(server.get("name"), str) or not server["name"]:
                return False
            name = server["name"].casefold()
            if name == "ai_team" or name.startswith("ai_team_"):
                return False
        return True
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return False


def _setup_issue(error: Exception) -> str:
    if isinstance(error, (ValueError, TypeError)):
        return "invalid_configuration"
    return "local_state_unavailable"


def build_adapters(args: argparse.Namespace, issues: dict | None = None) -> dict:
    """Prepare provider adapters without logging in or submitting tasks."""
    issues = {} if issues is None else issues
    data = Path(args.data_dir).expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    adapters = {}
    invalid_claude = args.claude_session_evidence and not valid_session_evidence_model(args.claude_model)
    if invalid_claude:
        issues["claude"] = "session_evidence_model_invalid"
        try:
            adapters["claude"] = ReadOnlyClaudeLedger(data / "claude-jobs")
        except Exception:
            # The historical ledger may be unavailable; Grok can still start.
            issues["claude"] = "local_state_unavailable"
    try:
        claude_cli = None if invalid_claude else executable(args.claude_cli)
        if claude_cli is None and not invalid_claude:
            issues["claude"] = "cli_not_found"
        if claude_cli:
            profile = (Path(args.claude_profile).expanduser().resolve()
                       if args.claude_profile else data / "claude-profile")
            work = data / "claude-work"
            state = data / "claude-jobs"
            profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            work.mkdir(exist_ok=True, mode=0o700)
            state.mkdir(exist_ok=True, mode=0o700)
            adapters["claude"] = ClaudeAdapter(
                claude_cli, str(profile), str(work), str(state), args.claude_model,
                args.claude_effort, enabled=True, timeout_seconds=args.timeout,
                session_evidence=args.claude_session_evidence,
            )
    except Exception as error:
        # One unavailable provider, including a broken local database, must
        # not prevent the other starting. Do not expose exception details.
        issues["claude"] = _setup_issue(error)
    try:
        grok_cli = executable(args.grok_cli)
        if grok_cli is None:
            issues["grok"] = "cli_not_found"
        if grok_cli:
            profile = (Path(args.grok_profile).expanduser().resolve()
                       if args.grok_profile else data / "grok-profile")
            profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            adapters["grok"] = GrokAdapter(
                grok_cli, args.grok_model, args.grok_effort, str(profile), str(data),
                timeout_seconds=args.timeout,
            )
    except Exception as error:
        issues["grok"] = _setup_issue(error)
    return adapters


def build_login_adapter(args: argparse.Namespace):
    """Prepare only the chosen official login without opening job databases.

    None means the executable could not be found. Local setup errors are
    raised separately and converted to a fixed, path-free message by main.
    """
    name = args.claude_cli if args.provider == "claude" else args.grok_cli
    cli = executable(name)
    if cli is None:
        return None
    data = Path(args.data_dir).expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.provider == "claude":
        profile = (Path(args.claude_profile).expanduser().resolve()
                   if args.claude_profile else data / "claude-profile")
        work = data / "claude-work"
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        work.mkdir(exist_ok=True, mode=0o700)
        return ClaudeLoginAdapter(cli, str(profile), str(work), args.claude_model,
                                  args.claude_effort, enabled=True, timeout_seconds=args.timeout)
    profile = (Path(args.grok_profile).expanduser().resolve()
               if args.grok_profile else data / "grok-profile")
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    return GrokLoginAdapter(cli, args.grok_model, args.grok_effort, str(profile),
                            timeout_seconds=args.timeout)


_RELEASE_MESSAGES = {
    "released": "Released by operator confirmation. The remote outcome remains uncertain; no request was resent.",
    "not_blocking": "This job does not hold a local slot; nothing changed.",
    "unknown_job": "No matching job was found in this provider ledger; nothing changed.",
    "ledger_unavailable": "No accessible safe job ledger; existing jobs were not checked.",
    "worker_running": "The bound worker is still running; nothing changed.",
    "identity_unconfirmed": "The worker identity or exit cannot be confirmed; nothing changed.",
    "unsafe_state": "This job state cannot be safely released; nothing changed.",
}


def reconcile(args: argparse.Namespace) -> int:
    """Read or carefully release one job; never submit or kill a process."""
    data = Path(os.path.abspath(os.fspath(Path(args.data_dir).expanduser())))
    ledger, state = ((claude_ledger, data / "claude-jobs") if args.provider == "claude"
                     else (grok_ledger, data))
    if args.release is None:
        jobs = ledger.blocking_jobs(str(state))
        if jobs is None:
            print("No accessible safe job ledger; existing jobs were not checked.")
            return 1
        elif not jobs:
            print("No job holds a local slot.")
        else:
            for job in jobs:
                if not isinstance(job, dict):
                    print("invalid_record  state=unknown  worker=unknown")
                    continue
                job_id, state_value = job.get("job_id"), job.get("state")
                valid_id = (isinstance(job_id, str) and
                            (bool(claude_ledger._TASK_ID.fullmatch(job_id)) if args.provider == "claude"
                             else grok_ledger._valid_id(job_id)))
                state_value = state_value if state_value in ("reserved", "dispatching", "queued", "running", "submission_uncertain", "finished_uncertain", "not_submitted") else "unknown"
                worker = job.get("worker_state", "unknown")
                if worker not in ("running", "exited", "unknown"):
                    worker = "unknown"
                print(f"{job_id if valid_id else 'invalid_record'}  {state_value}  worker={worker}")
            print("Check the specific job and provider client process before any release.", file=sys.stderr)
        return 0
    if not args.confirm_process_exited:
        print("Nothing changed. The operator must check the job's client process and add --confirm-process-exited.", file=sys.stderr)
        return 1
    outcome = ledger.operator_release(str(state), args.release)
    print(_RELEASE_MESSAGES.get(outcome, "Nothing changed; the job requires manual investigation."))
    return 0 if outcome == "released" else 1


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    # Expand only when a mode actually needs the state directory. A check
    # using an explicit CODEX_HOME must work even when HOME is unavailable.
    common.add_argument("--data-dir", default="~/.local-ai-team",
                        help="Private local state directory")
    common.add_argument("--claude-cli", default="claude", help="Claude Code executable")
    common.add_argument("--grok-cli", default="grok", help="Grok Build executable")
    common.add_argument("--claude-profile", help="Optional existing Claude Code profile directory")
    common.add_argument("--grok-profile", help="Optional existing Grok Build profile directory")
    common.add_argument("--claude-model", default="sonnet", help="Model or official Claude alias")
    common.add_argument("--grok-model", default="grok-build", help="Model supported by your Grok account")
    common.add_argument("--claude-effort", default="medium", help="Claude thinking level")
    common.add_argument("--claude-session-evidence", action="store_true",
                        help="Explicit Windows-only opt-in: the official client may store task text and answers in a private session; cleanup is not guaranteed")
    common.add_argument("--grok-effort", default="medium", help="Grok thinking level")
    common.add_argument("--timeout", type=int, default=600, help="Seconds before a slow task is marked uncertain")
    common.add_argument("--codex-cli", default="codex", help="Codex executable for installation")
    result = argparse.ArgumentParser(description=__doc__)
    modes = result.add_subparsers(dest="mode", required=True)
    modes.add_parser("serve", parents=[common], help="Start the local MCP server")
    modes.add_parser("install", parents=[common], help="Add this service to Codex without changing existing entries")
    modes.add_parser("setup-check", parents=[common], help="Check local prerequisites without logging in or changing configuration")
    login = modes.add_parser("login", parents=[common], help="Run a provider's official login")
    login.add_argument("provider", choices=("claude", "grok"))
    recon = modes.add_parser("reconcile", parents=[common], help="List or carefully release stuck jobs")
    recon.add_argument("provider", choices=("claude", "grok"))
    recon.add_argument("--release", metavar="JOB_ID", help="Request release of one local slot")
    recon.add_argument("--confirm-process-exited", action="store_true", help="Operator confirms the job's client process has exited")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 30 <= args.timeout <= 900:
        parser().error("--timeout must be between 30 and 900 seconds")
    invalid_claude = args.claude_session_evidence and not valid_session_evidence_model(args.claude_model)
    if args.mode == "install" and invalid_claude:
        print("Claude session evidence requires an explicit full claude-* model ID; bare aliases are not supported.", file=sys.stderr)
        return 1
    if args.mode == "serve" and invalid_claude:
        print("Claude session evidence requires an explicit full claude-* model ID; new Claude tasks are disabled.", file=sys.stderr)
    try:
        if args.mode == "setup-check":
            # A presence check is not proof of login, account entitlement,
            # MCP registration, rule loading or a successful model request.
            codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
            report = {
                "python_supported": sys.version_info >= (3, 10),
                "clients_found": {"codex": executable(args.codex_cli) is not None,
                                  "claude": executable(args.claude_cli) is not None,
                                  "grok": executable(args.grok_cli) is not None},
                "global_rules": {"agents_file_present": (codex_home / "AGENTS.md").is_file(),
                                 "override_file_present": (codex_home / "AGENTS.override.md").is_file()},
                "login_checked": False, "connection_checked": False,
                "rules_loaded_checked": False, "provider_requests_sent": False,
            }
            print(json.dumps(report, ensure_ascii=True, sort_keys=True))
            return 0
        if args.mode == "install":
            codex_cli = executable(args.codex_cli)
            if codex_cli is None:
                print("Codex was not found. Install it or specify its path.", file=sys.stderr)
                return 1
            # A data-directory marker alone cannot guard attempts made with a
            # different data directory. Reserve once per actual Codex home,
            # and retain the reservation even when later steps are uncertain.
            requested_home = os.environ.get("CODEX_HOME")
            codex_home = Path(requested_home).expanduser() if requested_home else Path.home() / ".codex"
            if not codex_home.is_absolute():
                print("Codex home must be an absolute path. Check configuration before installing.", file=sys.stderr)
                return 1
            codex_home = codex_home.resolve()
            codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            global_marker = codex_home / ".ai-collaboration-guide-install-attempt"
            try:
                global_fd = os.open(global_marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                print("A previous installation attempt exists. No server was added; "
                      "check Codex and the installation markers before making changes.", file=sys.stderr)
                return 1
            with os.fdopen(global_fd, "w", encoding="utf-8") as stream:
                stream.write("attempt\n")
                stream.flush()
                os.fsync(stream.fileno())
            codex_env = os.environ.copy()
            codex_env["CODEX_HOME"] = str(codex_home)
            if not codex_registry_available(codex_cli, codex_env):
                print("Could not safely verify existing Codex services. Check Codex before "
                      "changing installation markers or trying again.", file=sys.stderr)
                return 1
            # Reserve the installation attempt before invoking Codex. An
            # interrupted command may already have changed Codex, so a
            # subsequent invocation must not silently add another server.
            # This tool-owned marker is not proof the server still exists.
            data = Path(args.data_dir).expanduser().resolve()
            claude_cli, claude_found = installation_executable(args.claude_cli)
            grok_cli, grok_found = installation_executable(args.grok_cli)
            claude_profile = str(Path(args.claude_profile).expanduser().resolve()) if args.claude_profile else None
            grok_profile = str(Path(args.grok_profile).expanduser().resolve()) if args.grok_profile else None
            data.mkdir(parents=True, exist_ok=True, mode=0o700)
            marker = data / "codex-install-attempt.txt"
            name = "ai_team_" + uuid.uuid4().hex
            try:
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                print("A previous installation attempt exists. No server was added; "
                      "check Codex before removing codex-install-attempt.txt "
                      "from this tool's data directory and trying again.", file=sys.stderr)
                return 1
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(name + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            command = [codex_cli, "mcp", "add", name, "--", str(Path(sys.executable).resolve()),
                       str(Path(__file__).resolve()), "serve", "--data-dir", str(data),
                       "--claude-cli", claude_cli, "--grok-cli", grok_cli,
                       "--claude-model", args.claude_model, "--grok-model", args.grok_model,
                       "--claude-effort", args.claude_effort, "--grok-effort", args.grok_effort,
                       "--timeout", str(args.timeout)]
            if args.claude_session_evidence:
                command.append("--claude-session-evidence")
            for flag, value in (("--claude-profile", claude_profile), ("--grok-profile", grok_profile)):
                if value:
                    command.extend((flag, value))
            completed = subprocess.run(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       check=False, shell=False, env=codex_env)
            if completed.returncode:
                print("Could not confirm the MCP server was added. Check Codex before "
                      "removing the local installation marker or retrying.", file=sys.stderr)
                return 1
            print(f"Added MCP server {name}. Restart the Codex connection to load it.")
            for label, found in (("Claude", claude_found), ("Grok", grok_found)):
                if not found:
                    print(f"{label} client was not found during installation; "
                          "its availability must be checked when the server starts.", file=sys.stderr)
            return 0
        if args.mode == "reconcile":
            try:
                return reconcile(args)
            except Exception:
                print("Operation not confirmed. Check the original job before acting; do not retry automatically.", file=sys.stderr)
                return 1
        if args.mode == "login":
            try:
                provider = build_login_adapter(args)
            except Exception:
                print("Unable to prepare official login. Check local configuration or permissions.", file=sys.stderr)
                return 1
            if provider is None:
                print("Official client not found. Install it or specify its path.", file=sys.stderr)
                return 1
            try:
                result = provider.login()
                ok = (result.get("status") == "finished" if isinstance(result, dict)
                      else type(result) is int and result == 0)
            except Exception:
                ok = False
            if not ok:
                print("Official login did not complete. Check the client and local configuration.", file=sys.stderr)
            return 0 if ok else 1
        issues: dict = {}
        adapters = build_adapters(args, issues)
        for name in sorted(issues):
            print(f"{name} adapter not started: {issues[name]}", file=sys.stderr)
        data = Path(args.data_dir).expanduser().resolve()
        # The read-only Claude ledger exists solely to query old tasks. This
        # startup fact must survive an unavailable or locked status probe.
        core = Coordinator(data / "requests.sqlite3", adapters=adapters,
                           unconfigured_providers=("claude",) if invalid_claude else (),
                           setup_issues=issues)
        for stream in (sys.stdin, sys.stdout):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="strict")
        serve(core, sys.stdin, sys.stdout)
        return 0
    except Exception:
        # Do not echo local paths, credentials, or exception text in MCP logs.
        message = {
            "setup-check": "Unable to check local prerequisites. Check your Codex home and permissions.",
            "install": "Unable to complete installation. Check Codex and the installation markers before trying again.",
            "login": "Unable to prepare official login. Check local configuration or permissions.",
        }.get(args.mode, "Unable to start the local collaboration service.")
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
