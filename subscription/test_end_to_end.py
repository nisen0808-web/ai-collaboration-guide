"""Exercise the real adapters and coordinator against simulated official CLIs."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import ai_team
import mcp_stdio
from coordinator import Coordinator
from claude.claude_adapter import ClaudeAdapter
from grok.grok_adapter import GrokAdapter


class EndToEndTests(unittest.TestCase):
    def test_grok_client_evidence_reaches_mcp_without_claiming_server_attestation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli, profile = root / "synthetic-grok", root / "profile"
            cli.touch()
            profile.mkdir()
            adapter = GrokAdapter(str(cli), "grok-4.7", "xhigh", str(profile))
            core = Coordinator(root / "ledger.sqlite", {"grok": adapter})
            report = {k: [] for k in ("hooks", "plugins", "mcpServers", "skills", "lspServers", "projectInstructions")}
            class Child:
                def __init__(self, argv, **kwargs):
                    kwargs["stdout"].write(b"synthetic answer")
                    kwargs["stdout"].flush()
                def wait(self, timeout=None):
                    return 0
            evidence = {"client_evidence_status": "matched", "client_model_matches_requested": True,
                        "client_effort_matches_requested": True, "observed_model": "grok-4.7",
                        "observed_effort": "xhigh", "evidence_source": "official_grok_client_session",
                        "server_effort_verified": False}
            def call(name, arguments):
                response = mcp_stdio.handle(core, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}})
                self.assertFalse(response["result"]["isError"])
                return response["result"]["structuredContent"]
            with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(report).encode())), \
                 patch("subprocess.Popen", side_effect=Child) as launch, \
                 patch("grok.session_evidence.preflight", return_value=True), \
                 patch("grok.session_evidence.collect", return_value=evidence):
                receipt = call("collab_submit", {"provider": "grok", "task": "synthetic", "request_key": "new-key"})
                self.assertEqual(receipt["status"], "queued")
                deadline = time.monotonic() + 3
                result = {}
                while time.monotonic() < deadline:
                    result = call("collab_result", {"provider": "grok", "request_key": "new-key"})
                    if result.get("quality_verification", {}).get("client_record_verified") is True:
                        break
                    time.sleep(0.01)
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["output"], "synthetic answer")
                quality = result["quality_verification"]
                self.assertTrue(quality["client_record_verified"])
                self.assertEqual(quality["observed_model"], "grok-4.7")
                self.assertEqual(quality["observed_effort"], "xhigh")
                self.assertFalse(quality["verified"])
                self.assertFalse(quality["server_effort_verified"])
                self.assertEqual(launch.call_count, 1)

    def test_two_simulated_clis_submit_and_return_independent_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude_cli = root / "synthetic-claude"
            grok_cli = root / "synthetic-grok"
            claude_cli.touch()
            grok_cli.touch()
            args = ai_team.parser().parse_args([
                "serve", "--data-dir", str(root / "state"),
                "--claude-cli", str(claude_cli), "--grok-cli", str(grok_cli),
                "--claude-model", "test-claude", "--grok-model", "test-grok",
            ])
            # Test the official-client adapters independently of detached
            # worker launch; the durable wrappers have their own process tests.
            state = root / "state"
            state.mkdir()
            for name in ("claude-profile", "claude-work", "grok-profile"):
                (state / name).mkdir()
            adapters = {
                "claude": ClaudeAdapter(str(claude_cli), str(state / "claude-profile"),
                                        str(state / "claude-work"), args.claude_model,
                                        args.claude_effort, enabled=True),
                "grok": GrokAdapter(str(grok_cli), args.grok_model, args.grok_effort,
                                    str(state / "grok-profile")),
            }
            core = Coordinator(root / "ledger.sqlite", adapters)
            invocations = []

            def fake_run(argv, **kwargs):
                env = kwargs.get("env", {})
                self.assertNotIn("ANTHROPIC_API_KEY", env)
                self.assertNotIn("XAI_API_KEY", env)
                if argv[0] == str(claude_cli) and argv[1:3] == ["auth", "status"]:
                    return subprocess.CompletedProcess(argv, 0, json.dumps({
                        "loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"}), "")
                if argv[0] == str(grok_cli) and argv[1:3] == ["inspect", "--json"]:
                    report = {k: [] for k in ("hooks", "plugins", "mcpServers", "skills",
                                              "lspServers", "projectInstructions")}
                    return subprocess.CompletedProcess(argv, 0, json.dumps(report).encode(), b"")
                raise AssertionError("unexpected simulated command")

            class FakeGrokProcess:
                def wait(self, timeout):
                    return 0

            class FakeClaudeProcess:
                returncode = 0

                def communicate(self, input=None, timeout=None):
                    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "claude result"}), ""

            def fake_popen(argv, **kwargs):
                if argv[0] == str(claude_cli):
                    self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
                    invocations.append("claude")
                    return FakeClaudeProcess()
                self.assertEqual(argv[0], str(grok_cli))
                self.assertNotIn("XAI_API_KEY", kwargs["env"])
                invocations.append("grok")
                kwargs["stdout"].write(b"grok result")
                return FakeGrokProcess()

            with patch("subprocess.run", side_effect=fake_run), patch("subprocess.Popen", side_effect=fake_popen):
                for provider in ("claude", "grok"):
                    result = core.submit(provider, "synthetic task", provider + "-request")
                    self.assertEqual(result["status"], "queued")
                results = {}
                deadline = time.monotonic() + 3
                while len(results) < 2 and time.monotonic() < deadline:
                    for provider in ("claude", "grok"):
                        observed = core.result(provider, provider + "-request")
                        if observed.get("status") == "succeeded":
                            results[provider] = observed["output"]
                    if len(results) < 2:
                        time.sleep(0.01)
                self.assertEqual(results, {"claude": "claude result", "grok": "grok result"})
                for provider in ("claude", "grok"):
                    self.assertTrue(core.submit(provider, "synthetic task", provider + "-request")["replayed"])
                self.assertCountEqual(invocations, ["claude", "grok"])


if __name__ == "__main__":
    unittest.main()
