"""Entry-point tests with synthetic clients; no provider calls are made."""
from __future__ import annotations

import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ai_team


class EntryPointTests(unittest.TestCase):
    def test_builds_both_adapters_without_starting_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ai_team.parser().parse_args(["serve", "--data-dir", tmp,
                                                 "--claude-model", "test-claude",
                                                 "--grok-model", "test-grok"])
            with patch.object(ai_team, "executable", side_effect=lambda n: "/synthetic/" + n), \
                 patch.object(ai_team, "ClaudeAdapter") as claude, \
                 patch.object(ai_team, "GrokAdapter") as grok:
                adapters = ai_team.build_adapters(args)
            self.assertEqual(set(adapters), {"claude", "grok"})
            self.assertEqual(claude.call_args.args[4], "test-claude")
            self.assertEqual(grok.call_args.args[1], "test-grok")
            self.assertTrue(Path(tmp, "claude-profile").is_dir())
            self.assertTrue(Path(tmp, "grok-profile").is_dir())

    def test_missing_client_leaves_other_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ai_team.parser().parse_args(["serve", "--data-dir", tmp])
            with patch.object(ai_team, "executable", side_effect=lambda n: None if n == "claude" else "/synthetic/grok"), \
                 patch.object(ai_team, "GrokAdapter"):
                self.assertEqual(set(ai_team.build_adapters(args)), {"grok"})

    def test_invalid_provider_configuration_leaves_other_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ai_team.parser().parse_args(["serve", "--data-dir", tmp])
            with patch.object(ai_team, "executable", return_value="/synthetic/client"), \
                 patch.object(ai_team, "ClaudeAdapter", side_effect=ValueError("invalid")), \
                 patch.object(ai_team, "GrokAdapter"):
                self.assertEqual(set(ai_team.build_adapters(args)), {"grok"})

    def test_existing_profiles_are_used_only_when_explicitly_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude_profile = str(Path(tmp, "existing-claude"))
            grok_profile = str(Path(tmp, "existing-grok"))
            args = ai_team.parser().parse_args(["serve", "--data-dir", str(Path(tmp, "state")),
                                                 "--claude-profile", claude_profile,
                                                 "--grok-profile", grok_profile])
            with patch.object(ai_team, "executable", return_value="/synthetic/client"), \
                 patch.object(ai_team, "ClaudeAdapter") as claude, \
                 patch.object(ai_team, "GrokAdapter") as grok:
                ai_team.build_adapters(args)
            self.assertEqual(claude.call_args.args[1], claude_profile)
            self.assertEqual(grok.call_args.args[3], grok_profile)

    def test_login_invokes_only_selected_official_login(self):
        class Fake:
            def login(self):
                return 0
        with patch.object(ai_team, "build_adapters", return_value={"grok": Fake()}):
            self.assertEqual(ai_team.main(["login", "grok"]), 0)

    def test_claude_login_uses_structured_result(self):
        class Fake:
            def login(self):
                return {"status": "finished"}
        with patch.object(ai_team, "build_adapters", return_value={"claude": Fake()}):
            self.assertEqual(ai_team.main(["login", "claude"]), 0)

    def test_install_registers_new_name_without_starting_provider(self):
        completed = subprocess.CompletedProcess([], 0)
        output = io.StringIO()
        with patch.object(ai_team, "executable", return_value="/synthetic/codex"), \
             patch.object(ai_team.subprocess, "run", return_value=completed) as run, \
             patch.object(ai_team, "build_adapters", side_effect=AssertionError("provider started")), \
             redirect_stdout(output):
            self.assertEqual(ai_team.main(["install"]), 0)
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["mcp", "add"])
        self.assertTrue(command[3].startswith("ai_team_"))
        self.assertEqual(command[4], "--")
        self.assertIn("serve", command)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertIn("Added MCP server ai_team_", output.getvalue())

    def test_real_mcp_stdio_with_missing_clients_stays_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = [sys.executable, "-B", str(Path(ai_team.__file__)), "serve",
                       "--data-dir", tmp, "--claude-cli", "missing-claude-test",
                       "--grok-cli", "missing-grok-test"]
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
            completed = subprocess.run(command, input=payload, text=True, capture_output=True, timeout=10)
            self.assertEqual(completed.returncode, 0)
            result = json.loads(completed.stdout.strip())
            self.assertIn("tools", result["result"])


if __name__ == "__main__":
    unittest.main()
