"""Entry-point tests with synthetic clients; no provider calls are made."""
from __future__ import annotations

import json
import io
import os
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import hashlib
import unittest
from unittest.mock import patch

import ai_team


class EntryPointTests(unittest.TestCase):
    def test_setup_check_is_read_only_and_does_not_report_private_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp, "codex-home")
            home.mkdir()
            original = "synthetic existing rules"
            (home / "AGENTS.override.md").write_text(original, encoding="utf-8")
            output = io.StringIO()
            with patch.dict(os.environ, {"CODEX_HOME": str(home)}), \
                 patch.object(ai_team, "executable", side_effect=lambda name: "/synthetic/client" if name != "grok" else None), \
                 patch.object(ai_team, "build_adapters", side_effect=AssertionError("must not build")), \
                 patch.object(ai_team, "build_login_adapter", side_effect=AssertionError("must not log in")), \
                 patch.object(ai_team.subprocess, "run", side_effect=AssertionError("must not run")), \
                 redirect_stdout(output):
                self.assertEqual(ai_team.main(["setup-check", "--data-dir", str(Path(tmp, "unused"))]), 0)
            result = json.loads(output.getvalue())
            self.assertTrue(result["global_rules"]["override_file_present"])
            self.assertFalse(result["global_rules"]["agents_file_present"])
            self.assertEqual(result["clients_found"], {"codex": True, "claude": True, "grok": False})
            self.assertFalse(result["login_checked"])
            self.assertFalse(result["connection_checked"])
            self.assertFalse(result["rules_loaded_checked"])
            self.assertFalse(result["provider_requests_sent"])
            self.assertEqual((home / "AGENTS.override.md").read_text(encoding="utf-8"), original)
            self.assertFalse(Path(tmp, "unused").exists())
            self.assertNotIn(tmp, output.getvalue())

    def test_setup_check_uses_default_codex_home_when_env_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp, ".codex")
            config.mkdir()
            (config / "AGENTS.md").write_text("existing", encoding="utf-8")
            output = io.StringIO()
            with patch.dict(os.environ, {"CODEX_HOME": ""}), \
                 patch.object(Path, "home", return_value=Path(tmp)), \
                 patch.object(ai_team, "executable", return_value=None), redirect_stdout(output):
                self.assertEqual(ai_team.main(["setup-check"]), 0)
            self.assertTrue(json.loads(output.getvalue())["global_rules"]["agents_file_present"])
            self.assertEqual((config / "AGENTS.md").read_text(encoding="utf-8"), "existing")

    def test_setup_check_can_use_codex_home_without_a_system_home(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"CODEX_HOME": tmp}), \
             patch.object(Path, "home", side_effect=RuntimeError("synthetic private path")), \
             patch.object(ai_team, "executable", return_value=None), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(ai_team.main(["setup-check"]), 0)
            self.assertFalse(json.loads(output.getvalue())["rules_loaded_checked"])

    def test_setup_check_failure_is_specific_and_hides_details(self):
        stderr = io.StringIO()
        with patch.object(ai_team, "executable", side_effect=OSError("synthetic private path")), \
             redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            self.assertEqual(ai_team.main(["setup-check"]), 1)
        self.assertIn("Unable to check local prerequisites", stderr.getvalue())
        self.assertNotIn("synthetic private path", stderr.getvalue())
        self.assertNotIn("start the local collaboration service", stderr.getvalue())

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

    def test_client_session_evidence_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            for flags, expected in (([], False), (["--claude-session-evidence"], True)):
                args = ai_team.parser().parse_args(["serve", "--data-dir", tmp, "--claude-model", "claude-sonnet-4-5", *flags])
                with patch.object(ai_team, "executable", side_effect=lambda n: "/synthetic/" + n), \
                     patch.object(ai_team, "ClaudeAdapter") as claude, patch.object(ai_team, "GrokAdapter"):
                    ai_team.build_adapters(args)
                self.assertIs(claude.call_args.kwargs["session_evidence"], expected)

    def test_session_evidence_rejects_alias_before_install(self):
        for mode in ("install",):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                stderr = io.StringIO()
                with patch.object(ai_team, "build_adapters", side_effect=AssertionError("must not build")), \
                     patch.object(ai_team.subprocess, "run", side_effect=AssertionError("must not run")), \
                     redirect_stderr(stderr):
                    result = ai_team.main([mode, "--data-dir", tmp, "--claude-session-evidence"])
                self.assertEqual(result, 1)
                self.assertIn("explicit full claude-* model ID", stderr.getvalue())
                self.assertNotIn(tmp, stderr.getvalue())
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_invalid_session_model_serves_grok_and_rejects_new_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with patch.object(ai_team, "executable", side_effect=lambda name: "/synthetic/grok" if name == "grok" else None), \
                 patch.object(ai_team, "ClaudeAdapter", side_effect=AssertionError("no Claude process")), \
                 patch.object(ai_team, "GrokAdapter") as grok, \
                 patch.object(ai_team, "serve") as serve, redirect_stderr(stderr):
                grok.return_value.submit.return_value = {"status": "queued", "job_id": "grokjob"}
                self.assertEqual(ai_team.main(["serve", "--data-dir", tmp, "--claude-session-evidence"]), 0)
                core = serve.call_args.args[0]
                claude = core.submit("claude", "synthetic task", "claude-key")
                self.assertEqual(claude["status"], "not_submitted")
                self.assertEqual(claude["reason"], "session_evidence_model_invalid")
                self.assertEqual(core.submit("grok", "synthetic task", "grok-key")["status"], "queued")
                grok.return_value.submit.assert_called_once()
                self.assertFalse(Path(tmp, "claude-jobs", "jobs.sqlite3").exists())
                self.assertIn("new Claude tasks are disabled", stderr.getvalue())

    def test_invalid_session_model_status_stays_disabled_if_ledger_probe_hangs(self):
        with tempfile.TemporaryDirectory() as tmp:
            entered, release = threading.Event(), threading.Event()
            def blocked_status():
                entered.set()
                release.wait(3)
                return {"configured": True, "ready": True, "loggedIn": True}
            with patch.object(ai_team, "executable", side_effect=lambda name: "/synthetic/grok" if name == "grok" else None), \
                 patch.object(ai_team, "ClaudeAdapter", side_effect=AssertionError("no Claude process")), \
                 patch.object(ai_team, "GrokAdapter") as grok, patch.object(ai_team, "serve") as serve, \
                 redirect_stderr(io.StringIO()):
                grok.return_value.status.return_value = {"ready": True}
                self.assertEqual(ai_team.main(["serve", "--data-dir", tmp, "--claude-session-evidence"]), 0)
                core = serve.call_args.args[0]
                core.status_timeout = 0.1
                with patch.object(core.adapters["claude"], "status", side_effect=blocked_status):
                    try:
                        for _ in range(2):
                            values = core.status()["providers"]
                            self.assertTrue(entered.is_set())
                            self.assertIs(values["claude"]["configured"], False)
                            self.assertIs(values["claude"]["ready"], False)
                            self.assertNotIn("loggedIn", values["claude"])
                            self.assertIs(values["grok"]["ready"], True)
                    finally:
                        release.set()

    def test_invalid_session_model_reads_original_ledger_and_wal_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp, "claude-jobs")
            state.mkdir()
            db = state / "jobs.sqlite3"
            writer = sqlite3.connect(str(db))
            self.addCleanup(writer.close)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL, error TEXT, heartbeat REAL, output_file TEXT)")
            writer.commit()
            writer.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)", ("oldjob", "fingerprint", "running", None, time.time(), None))
            writer.commit()
            args = ai_team.parser().parse_args(["serve", "--data-dir", tmp, "--claude-session-evidence"])
            with patch.object(ai_team, "executable", return_value=None), patch.object(ai_team, "ClaudeAdapter", side_effect=AssertionError("no client")):
                adapter = ai_team.build_adapters(args)["claude"]
            self.assertEqual(adapter.find_job("oldjob"), "oldjob")
            self.assertEqual(adapter.poll("oldjob")["status"], "running")
            result = state / "oldjob.result.json"
            result.write_text(json.dumps({"output": "synthetic prior result"}), encoding="utf-8")
            writer.execute("UPDATE jobs SET status=?, output_file=? WHERE id=?", ("succeeded", result.name, "oldjob"))
            writer.commit()
            before = writer.execute("SELECT * FROM jobs").fetchall()
            self.assertEqual(adapter.poll("oldjob")["output"], "synthetic prior result")
            self.assertEqual(writer.execute("SELECT * FROM jobs").fetchall(), before)
            self.assertEqual(adapter.poll("../oldjob")["status"], "submission_uncertain")
            writer.execute("UPDATE jobs SET output_file=? WHERE id=?", ("../other", "oldjob"))
            writer.commit()
            self.assertEqual(adapter.poll("oldjob")["error"], "result_unavailable")
            writer.close()

    def test_invalid_session_model_missing_or_corrupt_ledger_is_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ai_team.ReadOnlyClaudeLedger(Path(tmp, "claude-jobs"))
            self.assertEqual(adapter.poll("oldjob")["status"], "submission_uncertain")
            self.assertIsNone(adapter.find_job("oldjob"))
            self.assertFalse(adapter.db.exists())
            adapter.state_dir.mkdir()
            adapter.db.write_bytes(b"not a sqlite database")
            before = hashlib.sha256(adapter.db.read_bytes()).digest()
            self.assertEqual(adapter.poll("oldjob")["error"], "job_state_unavailable")
            self.assertIsNone(adapter.find_job("oldjob"))
            self.assertEqual(hashlib.sha256(adapter.db.read_bytes()).digest(), before)

    def test_read_only_ledger_reads_real_durable_adapter_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp, "claude-jobs")
            state.mkdir()
            durable = ai_team.ClaudeAdapter(str(Path(tmp, "synthetic-cli")), tmp, tmp,
                                            str(state), "claude-synthetic-1", "medium", enabled=False)
            output = state / "oldjob.result.json"
            model_quality = {"model_usage_matches_requested": True, "recorded_model": "claude-synthetic-1",
                             "evidence_source": "cli_result_model_usage"}
            session_quality = {"client_evidence_status": "matched", "session_cleanup": "deletion_requested",
                               "client_model_matches_requested": True, "client_effort_matches_requested": True}
            output.write_text(json.dumps({"output": "synthetic result", "quality_verification": model_quality}), encoding="utf-8")
            db = sqlite3.connect(durable.db)
            try:
                with db:
                    db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
                               ("oldjob", "synthetic-fingerprint", "succeeded", None, None, output.name))
                    db.execute("INSERT INTO session_evidence_intents VALUES (?, ?, ?)",
                               ("oldjob", "synthetic-session", json.dumps(session_quality)))
            finally:
                db.close()
            old = ai_team.ReadOnlyClaudeLedger(state)
            self.assertEqual(old.find_job("oldjob"), "oldjob")
            self.assertEqual(old.poll("oldjob")["output"], "synthetic result")
            self.assertEqual(old.poll("oldjob")["status"], "succeeded")
            quality = old.poll("oldjob")["quality_verification"]
            self.assertIs(quality["model_usage_matches_requested"], True)
            self.assertIs(quality["client_effort_matches_requested"], True)
            self.assertFalse(old.status()["configured"])

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

    def test_broken_provider_database_does_not_block_the_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ai_team.parser().parse_args(["serve", "--data-dir", tmp])
            for failed, available in (("ClaudeAdapter", "grok"), ("GrokAdapter", "claude")):
                with self.subTest(failed=failed), \
                     patch.object(ai_team, "executable", return_value="/synthetic/client"), \
                     patch.object(ai_team, "ClaudeAdapter") as claude, \
                     patch.object(ai_team, "GrokAdapter") as grok:
                    (claude if failed == "ClaudeAdapter" else grok).side_effect = sqlite3.DatabaseError("private path")
                    self.assertEqual(set(ai_team.build_adapters(args)), {available})

    def test_coordinator_database_error_does_not_print_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with patch.object(ai_team, "build_adapters", return_value={}), \
                 patch.object(ai_team, "Coordinator", side_effect=sqlite3.DatabaseError("private path")), \
                 redirect_stderr(stderr):
                self.assertEqual(ai_team.main(["serve", "--data-dir", tmp]), 1)
            self.assertIn("Unable to start", stderr.getvalue())
            self.assertNotIn("private path", stderr.getvalue())
            self.assertNotIn(tmp, stderr.getvalue())

    def test_unexpected_service_error_does_not_print_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with patch.object(ai_team, "build_adapters", return_value={}), \
                 patch.object(ai_team, "Coordinator"), \
                 patch.object(ai_team, "serve", side_effect=RuntimeError("private path")), \
                 redirect_stderr(stderr):
                self.assertEqual(ai_team.main(["serve", "--data-dir", tmp]), 1)
            self.assertNotIn("private path", stderr.getvalue())
            self.assertNotIn(tmp, stderr.getvalue())

    def test_existing_profiles_are_used_only_when_explicitly_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Windows may return an 8.3 short path for the temporary parent;
            # the entry point intentionally stores resolved absolute paths.
            tmp = str(Path(tmp).resolve())
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

    @unittest.skipUnless(os.name == "nt", "Windows short-path behavior")
    def test_existing_profiles_expand_windows_short_paths(self):
        import ctypes
        from ctypes import wintypes
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "synthetic profile with a long name"
            profile.mkdir()
            get_short = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
            get_short.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
            get_short.restype = wintypes.DWORD
            buf = ctypes.create_unicode_buffer(32768)
            length = get_short(str(profile), buf, len(buf))
            if not length or length >= len(buf) or os.path.normcase(buf.value) == os.path.normcase(str(profile)):
                self.skipTest("Windows short names are unavailable")
            short_profile = str(Path(buf.value) / "existing-claude")
            args = ai_team.parser().parse_args(["serve", "--data-dir", str(Path(tmp) / "state"),
                                                 "--claude-profile", short_profile])
            with patch.object(ai_team, "executable", return_value="/synthetic/client"), \
                 patch.object(ai_team, "ClaudeAdapter") as claude, \
                 patch.object(ai_team, "GrokAdapter"):
                ai_team.build_adapters(args)
            self.assertEqual(claude.call_args.args[1], str((profile / "existing-claude").resolve()))

    def test_login_invokes_only_selected_official_login(self):
        class Fake:
            def login(self):
                return 0
        with patch.object(ai_team, "build_login_adapter", return_value=Fake()), \
             patch.object(ai_team, "build_adapters", side_effect=AssertionError("opened job databases")):
            self.assertEqual(ai_team.main(["login", "grok"]), 0)

    def test_claude_login_uses_structured_result(self):
        class Fake:
            def login(self):
                return {"status": "finished"}
        with patch.object(ai_team, "build_login_adapter", return_value=Fake()), \
             patch.object(ai_team, "build_adapters", side_effect=AssertionError("opened job databases")):
            self.assertEqual(ai_team.main(["login", "claude"]), 0)

    def test_login_initializes_only_selected_adapter_without_job_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            for selected in ("claude", "grok"):
                with self.subTest(selected=selected), \
                     patch.object(ai_team, "executable", return_value=str(Path(tmp, "client"))), \
                     patch.object(ai_team, "ClaudeLoginAdapter") as claude, \
                     patch.object(ai_team, "GrokLoginAdapter") as grok, \
                     patch.object(ai_team, "ClaudeAdapter", side_effect=AssertionError("durable database")), \
                     patch.object(ai_team, "GrokAdapter", side_effect=AssertionError("durable database")), \
                     patch.object(ai_team, "Coordinator", side_effect=AssertionError("coordinator database")):
                    if selected == "claude":
                        claude.return_value.login.return_value = {"status": "finished"}
                    else:
                        grok.return_value.login.return_value = 0
                    self.assertEqual(ai_team.main(["login", selected, "--data-dir", tmp]), 0)
                    self.assertEqual(claude.call_count, int(selected == "claude"))
                    self.assertEqual(grok.call_count, int(selected == "grok"))
                    self.assertFalse(Path(tmp, "requests.sqlite3").exists())
                    self.assertFalse(Path(tmp, "claude-jobs").exists())
                    self.assertFalse(Path(tmp, "grok-jobs.sqlite3").exists())

    def test_login_missing_client_and_bad_configuration_are_distinct_and_private(self):
        for setup, expected in ((None, "Official client not found"),
                                (ValueError("synthetic private path"), "Unable to prepare official login")):
            with self.subTest(setup=type(setup).__name__), tempfile.TemporaryDirectory() as tmp:
                stderr = io.StringIO()
                with patch.object(ai_team, "build_login_adapter", side_effect=setup) if isinstance(setup, Exception) else \
                     patch.object(ai_team, "build_login_adapter", return_value=setup):
                    with redirect_stderr(stderr):
                        self.assertEqual(ai_team.main(["login", "claude", "--data-dir", tmp]), 1)
                self.assertIn(expected, stderr.getvalue())
                self.assertNotIn(tmp, stderr.getvalue())
                self.assertNotIn("synthetic private path", stderr.getvalue())
                if isinstance(setup, Exception):
                    self.assertNotIn("Official client not found", stderr.getvalue())

    def test_failed_login_output_does_not_expose_exception(self):
        stderr = io.StringIO()
        class Fake:
            def login(self):
                raise OSError("synthetic private path")
        with patch.object(ai_team, "build_login_adapter", return_value=Fake()), redirect_stderr(stderr):
            self.assertEqual(ai_team.main(["login", "claude"]), 1)
        self.assertIn("Official login did not complete", stderr.getvalue())
        self.assertNotIn("synthetic private path", stderr.getvalue())

    def test_install_registers_new_name_without_starting_provider(self):
        completed = subprocess.CompletedProcess([], 0)
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(ai_team, "executable", return_value="/synthetic/codex"), \
             patch.object(ai_team.subprocess, "run", return_value=completed) as run, \
             patch.object(ai_team, "build_adapters", side_effect=AssertionError("provider started")), \
             redirect_stdout(output):
            self.assertEqual(ai_team.main(["install", "--data-dir", tmp]), 0)
            self.assertTrue(Path(tmp, "codex-install-attempt.txt").exists())
        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["mcp", "add"])
        self.assertTrue(command[3].startswith("ai_team_"))
        self.assertEqual(command[4], "--")
        self.assertIn("serve", command)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertIn("Added MCP server ai_team_", output.getvalue())

    def test_install_repeat_does_not_add_or_read_existing_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp, "codex-install-attempt.txt")
            marker.write_text("opaque existing content", encoding="utf-8")
            stderr = io.StringIO()
            with patch.object(ai_team, "executable", return_value="/synthetic/codex"), \
                 patch.object(ai_team.subprocess, "run", side_effect=AssertionError("must not add")), \
                 redirect_stderr(stderr):
                self.assertEqual(ai_team.main(["install", "--data-dir", tmp]), 1)
            self.assertEqual(marker.read_text(encoding="utf-8"), "opaque existing content")
            self.assertIn("No server was added", stderr.getvalue())
            self.assertNotIn("opaque existing content", stderr.getvalue())

    def test_install_pins_relative_paths_for_a_different_startup_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            install_dir, later_dir = root / "install", root / "later"
            install_dir.mkdir()
            later_dir.mkdir()
            binaries = install_dir / "bin"
            binaries.mkdir()
            claude_cli, grok_cli = binaries / "claude-test", binaries / "grok-test"
            claude_cli.touch()
            grok_cli.touch()
            previous = Path.cwd()
            try:
                os.chdir(install_dir)
                def fake_executable(name):
                    if name == "codex":
                        return str(root / "codex-test")
                    path = Path(name)
                    return str(path.resolve()) if path.is_file() else None
                with patch.object(ai_team, "executable", side_effect=fake_executable), \
                     patch.object(ai_team.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(ai_team.main([
                        "install", "--data-dir", "data", "--claude-cli", "bin/claude-test",
                        "--grok-cli", "bin/grok-test", "--claude-profile", "profiles/claude",
                        "--grok-profile", "profiles/grok"]), 0)
                command = run.call_args.args[0]
                os.chdir(later_dir)
                stored = ai_team.parser().parse_args(command[7:])
                self.assertEqual(Path(stored.data_dir), install_dir / "data")
                self.assertEqual(Path(stored.claude_cli), claude_cli)
                self.assertEqual(Path(stored.grok_cli), grok_cli)
                self.assertEqual(Path(stored.claude_profile), install_dir / "profiles/claude")
                self.assertEqual(Path(stored.grok_profile), install_dir / "profiles/grok")
                with patch.object(ai_team, "executable", side_effect=lambda name: name), \
                     patch.object(ai_team, "ClaudeAdapter") as claude, \
                     patch.object(ai_team, "GrokAdapter") as grok:
                    self.assertEqual(set(ai_team.build_adapters(stored)), {"claude", "grok"})
                self.assertEqual(claude.call_args.args[1], str(install_dir / "profiles/claude"))
                self.assertEqual(grok.call_args.args[3], str(install_dir / "profiles/grok"))
            finally:
                os.chdir(previous)

    def test_install_keeps_missing_bare_cli_name_and_pins_missing_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = str(Path(tmp).resolve())
            previous = Path.cwd()
            stderr = io.StringIO()
            try:
                os.chdir(tmp)
                with patch.object(ai_team, "executable", side_effect=lambda name: str(Path(tmp, "codex")) if name == "codex" else None), \
                     patch.object(ai_team.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                     redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    self.assertEqual(ai_team.main([
                        "install", "--data-dir", "data", "--claude-cli", "future-claude",
                        "--grok-cli", "bin/future-grok"]), 0)
                stored = ai_team.parser().parse_args(run.call_args.args[0][7:])
                self.assertEqual(stored.claude_cli, "future-claude")
                self.assertEqual(Path(stored.grok_cli), Path(tmp, "bin", "future-grok"))
                self.assertIn("Claude client was not found", stderr.getvalue())
                self.assertIn("Grok client was not found", stderr.getvalue())
            finally:
                os.chdir(previous)

    def test_failed_install_retains_marker_and_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            stdout, stderr = io.StringIO(), io.StringIO()
            failed = subprocess.CompletedProcess([], 1)
            with patch.object(ai_team, "executable", return_value="/synthetic/codex"), \
                 patch.object(ai_team.subprocess, "run", return_value=failed) as run, \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(ai_team.main(["install", "--data-dir", tmp]), 1)
                self.assertEqual(ai_team.main(["install", "--data-dir", tmp]), 1)
            self.assertEqual(run.call_count, 1)
            self.assertTrue(Path(tmp, "codex-install-attempt.txt").exists())
            self.assertNotIn("Added MCP server", stdout.getvalue())
            self.assertIn("Could not confirm", stderr.getvalue())

    def test_concurrent_install_attempt_only_invokes_codex_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            entered, finish = threading.Event(), threading.Event()
            result = []
            def run_once(*_args, **_kwargs):
                entered.set()
                if not finish.wait(5):
                    raise TimeoutError("synthetic install did not finish")
                return subprocess.CompletedProcess([], 0)
            with patch.object(ai_team, "executable", return_value="/synthetic/codex"), \
                 patch.object(ai_team.subprocess, "run", side_effect=run_once) as run, \
                 patch("builtins.print"):
                first = threading.Thread(target=lambda: result.append(ai_team.main(["install", "--data-dir", tmp])))
                first.start()
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertEqual(ai_team.main(["install", "--data-dir", tmp]), 1)
                finally:
                    finish.set()
                    first.join(5)
                self.assertFalse(first.is_alive())
                self.assertEqual(result, [0])
                self.assertEqual(run.call_count, 1)

    def test_interrupted_install_retains_marker_and_never_readds(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ai_team, "executable", return_value="/synthetic/codex"), \
                 patch.object(ai_team.subprocess, "run", side_effect=OSError("synthetic")) as run, \
                 patch("builtins.print"):
                self.assertEqual(ai_team.main(["install", "--data-dir", tmp]), 1)
                self.assertEqual(ai_team.main(["install", "--data-dir", tmp]), 1)
            self.assertEqual(run.call_count, 1)
            self.assertTrue(Path(tmp, "codex-install-attempt.txt").exists())

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
