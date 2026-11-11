"""Run the local Claude/Grok collaboration server or the official login flow."""
from __future__ import annotations

import argparse
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
from grok.durable_grok import DurableGrokAdapter as GrokAdapter  # noqa: E402


def executable(name: str) -> str | None:
    """Resolve a named CLI or an explicitly supplied executable path."""
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())
    path = Path(name).expanduser()
    return str(path.resolve()) if path.is_file() else None


def build_adapters(args: argparse.Namespace) -> dict:
    """Prepare provider adapters without logging in or submitting tasks."""
    data = Path(args.data_dir).expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    adapters = {}
    claude_cli = executable(args.claude_cli)
    if claude_cli:
        try:
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
            )
        except (OSError, ValueError, TypeError):
            # One unavailable provider must not prevent the other starting.
            pass
    grok_cli = executable(args.grok_cli)
    if grok_cli:
        try:
            profile = (Path(args.grok_profile).expanduser().resolve()
                       if args.grok_profile else data / "grok-profile")
            profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            adapters["grok"] = GrokAdapter(
                grok_cli, args.grok_model, args.grok_effort, str(profile), str(data),
                timeout_seconds=args.timeout,
            )
        except (OSError, ValueError, TypeError):
            pass
    return adapters


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=str(Path.home() / ".local-ai-team"),
                        help="Private local state directory")
    common.add_argument("--claude-cli", default="claude", help="Claude Code executable")
    common.add_argument("--grok-cli", default="grok", help="Grok Build executable")
    common.add_argument("--claude-profile", help="Optional existing Claude Code profile directory")
    common.add_argument("--grok-profile", help="Optional existing Grok Build profile directory")
    common.add_argument("--claude-model", default="sonnet", help="Model or official Claude alias")
    common.add_argument("--grok-model", default="grok-build", help="Model supported by your Grok account")
    common.add_argument("--claude-effort", default="medium", help="Claude thinking level")
    common.add_argument("--grok-effort", default="medium", help="Grok thinking level")
    common.add_argument("--timeout", type=int, default=600, help="Seconds before a slow task is marked uncertain")
    common.add_argument("--codex-cli", default="codex", help="Codex executable for installation")
    result = argparse.ArgumentParser(description=__doc__)
    modes = result.add_subparsers(dest="mode", required=True)
    modes.add_parser("serve", parents=[common], help="Start the local MCP server")
    modes.add_parser("install", parents=[common], help="Add this service to Codex without changing existing entries")
    login = modes.add_parser("login", parents=[common], help="Run a provider's official login")
    login.add_argument("provider", choices=("claude", "grok"))
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 30 <= args.timeout <= 900:
        parser().error("--timeout must be between 30 and 900 seconds")
    try:
        if args.mode == "install":
            codex_cli = executable(args.codex_cli)
            if codex_cli is None:
                print("Codex was not found. Install it or specify its path.", file=sys.stderr)
                return 1
            # A unique name avoids overwriting any existing MCP server.
            name = "ai_team_" + uuid.uuid4().hex[:16]
            command = [codex_cli, "mcp", "add", name, "--", sys.executable,
                       str(Path(__file__).resolve()), "serve", "--data-dir", args.data_dir,
                       "--claude-cli", args.claude_cli, "--grok-cli", args.grok_cli,
                       "--claude-model", args.claude_model, "--grok-model", args.grok_model,
                       "--claude-effort", args.claude_effort, "--grok-effort", args.grok_effort,
                       "--timeout", str(args.timeout)]
            for flag, value in (("--claude-profile", args.claude_profile), ("--grok-profile", args.grok_profile)):
                if value:
                    command.extend((flag, value))
            completed = subprocess.run(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       check=False, shell=False)
            if completed.returncode:
                print("Could not add the MCP server; check the Codex installation.", file=sys.stderr)
                return 1
            print(f"Added MCP server {name}. Restart the Codex connection to load it.")
            return 0
        adapters = build_adapters(args)
        if args.mode == "login":
            provider = adapters.get(args.provider)
            if provider is None:
                print("Official client not found. Install it or specify its path.", file=sys.stderr)
                return 1
            result = provider.login()
            if isinstance(result, dict):
                return 0 if result.get("status") == "finished" else 1
            return int(result)
        data = Path(args.data_dir).expanduser().resolve()
        core = Coordinator(data / "requests.sqlite3", adapters=adapters)
        for stream in (sys.stdin, sys.stdout):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="strict")
        serve(core, sys.stdin, sys.stdout)
        return 0
    except (OSError, ValueError, TypeError):
        # Do not echo local paths, credentials, or exception text in MCP logs.
        print("Unable to start the local collaboration service.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
