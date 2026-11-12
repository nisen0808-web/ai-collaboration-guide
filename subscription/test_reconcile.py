"""Entry-point checks for setup diagnostics and reconcile; fully offline."""
from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import ai_team
from coordinator import Coordinator
from claude.readonly_ledger import ReadOnlyClaudeLedger


class SetupIssueTests(unittest.TestCase):
    def test_each_startup_failure_gets_a_fixed_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ai_team.parser().parse_args(["serve", "--data-dir", tmp])
            issues: dict = {}
            with patch.object(ai_team, "executable", side_effect=lambda n: None if n == "claude" else "/synthetic/grok"), \
                 patch.object(ai_team, "GrokAdapter", side_effect=sqlite3.DatabaseError("private path")):
                self.assertEqual(ai_team.build_adapters(args, issues), {})
            self.assertEqual(issues, {"claude": "cli_not_found", "grok": "local_state_unavailable"})
            issues = {}
            with patch.object(ai_team, "executable", return_value="/synthetic/client"), \
                 patch.object(ai_team, "ClaudeAdapter", side_effect=ValueError("private detail")), \
                 patch.object(ai_team, "GrokAdapter"):
                self.assertEqual(set(ai_team.build_adapters(args, issues)), {"grok"})
            self.assertEqual(issues, {"claude": "invalid_configuration"})

    def test_status_reports_only_known_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = Coordinator(Path(tmp) / "requests.sqlite3",
                               setup_issues={"claude": "cli_not_found", "grok": "C:/private/path",
                                             "other": "cli_not_found"})
            providers = core.status()["providers"]
            self.assertEqual(providers["claude"]["setup_issue"], "cli_not_found")
            self.assertNotIn("setup_issue", providers["grok"])
            self.assertEqual(set(providers), {"claude", "grok"})

    @unittest.skipIf(os.name == "nt", "POSIX launcher links")
    def test_cli_launcher_link_is_not_replaced_by_its_versioned_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target, launcher = root / "versions" / "1.0.0", root / "bin" / "client"
            target.parent.mkdir()
            launcher.parent.mkdir()
            target.touch(mode=0o700)
            launcher.symlink_to(target)
            self.assertEqual(ai_team.executable(str(launcher)), str(launcher))
            self.assertEqual(ai_team.installation_executable(str(launcher)), (str(launcher), True))

    @unittest.skipUnless(os.name == "nt", "Windows short paths")
    def test_windows_short_cli_path_expands_to_long(self):
        import ctypes
        from ctypes import wintypes
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp, "a long synthetic launcher directory")
            parent.mkdir()
            launcher = parent / "synthetic launcher.exe"
            launcher.touch()
            short = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
            short.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
            short.restype = wintypes.DWORD
            required = short(str(launcher), None, 0)
            if not required:
                self.skipTest("short paths unavailable")
            buffer = ctypes.create_unicode_buffer(required + 1)
            written = short(str(launcher), buffer, len(buffer))
            if not written or os.path.normcase(buffer.value) == os.path.normcase(str(launcher)):
                self.skipTest("short paths disabled")
            self.assertEqual(os.path.normcase(ai_team._cli_path(buffer.value)), os.path.normcase(str(launcher)))


class ReconcileTests(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ai_team.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_no_ledger_changes_nothing_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "absent"
            for provider in ("claude", "grok"):
                code, out, _ = self.run_main("reconcile", provider, "--data-dir", str(data))
                self.assertEqual(code, 1)
                self.assertIn("existing jobs were not checked", out)
            self.assertFalse(data.exists())

    def test_release_requires_confirmation_and_legacy_claude_stays_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp).resolve()
            state = data / "claude-jobs"
            state.mkdir()
            with closing(sqlite3.connect(state / "jobs.sqlite3")) as con, con:
                con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                            "status TEXT NOT NULL, error TEXT, heartbeat REAL, output_file TEXT)")
                con.execute("INSERT INTO jobs VALUES ('stale','digest','running',NULL,?,NULL)", (time.time() - 3600,))
            code, out, _ = self.run_main("reconcile", "claude", "--data-dir", str(data))
            self.assertEqual(code, 0)
            self.assertIn("stale  running  worker=unknown", out)
            code, _, err = self.run_main("reconcile", "claude", "--data-dir", str(data), "--release", "stale")
            self.assertEqual(code, 1)
            self.assertIn("--confirm-process-exited", err)
            code, out, _ = self.run_main("reconcile", "claude", "--data-dir", str(data),
                                         "--release", "stale", "--confirm-process-exited")
            self.assertEqual(code, 1)
            self.assertIn("cannot be confirmed", out)
            with closing(sqlite3.connect(state / "jobs.sqlite3")) as con:
                self.assertEqual(con.execute("SELECT status,error FROM jobs").fetchone(),
                                 ("running", None))
            code, _, _ = self.run_main("reconcile", "claude", "--data-dir", str(data),
                                       "--release", "stale", "--confirm-process-exited")
            self.assertEqual(code, 1)

    def test_ledger_error_is_reported_without_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ai_team.grok_ledger, "blocking_jobs", side_effect=sqlite3.DatabaseError("private path")):
                code, out, err = self.run_main("reconcile", "grok", "--data-dir", tmp)
            self.assertEqual(code, 1)
            self.assertNotIn("private", out + err)

    def test_corrupt_job_records_do_not_echo_secrets_or_terminal_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [{"job_id": "private\x1b[31m", "state": "sensitive state", "worker_state": "unknown"}]
            with patch.object(ai_team.grok_ledger, "blocking_jobs", return_value=rows):
                code, out, err = self.run_main("reconcile", "grok", "--data-dir", tmp)
            self.assertEqual(code, 0)
            self.assertIn("invalid_record  unknown  worker=unknown", out)
            self.assertNotIn("private", out + err)
            self.assertNotIn("sensitive", out + err)
            self.assertNotIn("\x1b", out + err)

    def test_readonly_legacy_access_reports_operator_assertion_without_claiming_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp).resolve()
            with closing(sqlite3.connect(state / "jobs.sqlite3")) as con, con:
                con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, error TEXT, heartbeat REAL, output_file TEXT)")
                con.execute("INSERT INTO jobs VALUES ('job','operator_released','operator_released',NULL,NULL)")
            reader = ReadOnlyClaudeLedger(state)
            result = reader.poll("job")
            self.assertEqual(result["status"], "submission_uncertain")
            self.assertEqual(result["release_basis"], "operator_confirmed")
            self.assertIs(result["local_exit_confirmed"], False)
            self.assertEqual(reader.status()["local_finished_unresolved"], 1)


if __name__ == "__main__":
    unittest.main()
