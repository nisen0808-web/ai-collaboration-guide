"""Exercise interpreter selection through the installer without real Codex calls."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ai_team


class InterpreterInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.codex_home = self.root / "codex-home"
        self.data = self.root / "data"
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)})
        environment.start()
        self.addCleanup(environment.stop)
        registry = patch.object(ai_team, "codex_registry_available", return_value=True)
        self.registry = registry.start()
        self.addCleanup(registry.stop)
        clients = patch.object(
            ai_team, "executable",
            side_effect=lambda name: str(self.root / "codex-test") if name == "codex" else None,
        )
        self.clients = clients.start()
        self.addCleanup(clients.stop)

    def launcher(self, relative="bin/stable-python.exe"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o700)
        path.chmod(0o700)
        return path

    def install(self, *extra):
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return ai_team.main([
                "install", "--grok-model", "grok-synthetic", "--data-dir", str(self.data), *extra,
            ])

    @staticmethod
    def successful_run(command, **kwargs):
        output = ai_team._PYTHON_PROBE_OUTPUT if command[1:4] == ["-I", "-S", "-c"] else b""
        return subprocess.CompletedProcess(command, 0, stdout=output)

    def registered_command(self, run):
        calls = [call for call in run.call_args_list if call.args[0][1:3] == ["mcp", "add"]]
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0].kwargs["shell"])
        self.assertEqual(calls[0].args[0][4], "--")
        return calls[0].args[0]

    def assert_no_installation(self):
        self.assertFalse(self.codex_home.exists())
        self.assertFalse(self.data.exists())
        self.registry.assert_not_called()
        self.clients.assert_not_called()
        self.assertNotIn(str(self.root), self.stderr.getvalue())

    def test_explicit_launcher_overrides_versioned_running_interpreter(self):
        stable = self.launcher()
        versioned = self.launcher("versions/python-1.0.0.exe")
        with patch.object(ai_team.sys, "executable", str(versioned)), \
             patch.object(ai_team.subprocess, "run", side_effect=self.successful_run) as run:
            self.assertEqual(self.install("--python-cli", str(stable)), 0)
        self.assertEqual(self.registered_command(run)[5], ai_team._cli_path(stable))
        probe = run.call_args_list[0]
        self.assertEqual(probe.args[0], [ai_team._cli_path(stable), "-I", "-S", "-c", ai_team._PYTHON_PROBE])
        self.assertFalse(probe.kwargs["shell"])
        self.assertEqual(probe.kwargs["timeout"], 10)
        self.assertEqual(probe.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(probe.kwargs["stderr"], subprocess.DEVNULL)
        self.assertTrue((self.codex_home / ".ai-collaboration-guide-install-attempt").is_file())
        self.assertTrue((self.data / "codex-install-attempt.txt").is_file())

    def test_default_keeps_versioned_path_without_guessing_alias_or_probing(self):
        versioned = self.launcher("versions/python-1.0.0.exe")
        self.launcher()
        with patch.object(ai_team.sys, "executable", str(versioned)), \
             patch.object(ai_team.subprocess, "run", side_effect=self.successful_run) as run:
            self.assertEqual(self.install(), 0)
        self.assertEqual(self.registered_command(run)[5], ai_team._cli_path(versioned))
        self.assertEqual(run.call_count, 1)

    def test_relative_launcher_is_registered_as_absolute_for_later_directory(self):
        stable = self.launcher()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(ai_team.subprocess, "run", side_effect=self.successful_run) as run:
                self.assertEqual(self.install("--python-cli", "bin/stable-python.exe"), 0)
            later = self.root / "later"
            later.mkdir()
            os.chdir(later)
            registered = self.registered_command(run)[5]
            self.assertTrue(Path(registered).is_absolute())
            self.assertEqual(registered, ai_team._cli_path(stable))
            self.assertTrue(Path(registered).is_file())
        finally:
            os.chdir(previous)

    def test_bare_launcher_command_is_pinned_to_its_path(self):
        stable = self.launcher()
        with patch.dict(os.environ, {"PATH": str(stable.parent)}), \
             patch.object(ai_team.subprocess, "run", side_effect=self.successful_run) as run:
            self.assertEqual(self.install("--python-cli", stable.name), 0)
        self.assertEqual(self.registered_command(run)[5], ai_team._cli_path(stable))

    def test_explicit_real_python_passes_offline_probe_before_registration(self):
        real_run = subprocess.run
        selected = sys.executable

        def run_local_probe(command, **kwargs):
            if command[1:4] == ["-I", "-S", "-c"]:
                self.assertFalse(self.codex_home.exists())
                self.assertFalse(self.data.exists())
                self.registry.assert_not_called()
                self.clients.assert_not_called()
                return real_run(command, **kwargs)
            return self.successful_run(command, **kwargs)

        with patch.object(ai_team.subprocess, "run", side_effect=run_local_probe) as run:
            self.assertEqual(self.install("--python-cli", selected), 0, self.stderr.getvalue())
        self.assertEqual(self.registered_command(run)[5], ai_team._cli_path(selected))

    def test_missing_empty_and_directory_selections_create_nothing(self):
        for selected in ("", str(self.root / "missing.exe"), str(self.root)):
            with self.subTest(selected=selected), \
                 patch.object(ai_team.subprocess, "run") as run:
                self.assertEqual(self.install("--python-cli", selected), 1)
                run.assert_not_called()
                self.assert_no_installation()

    @unittest.skipIf(os.name == "nt", "POSIX execute permissions")
    def test_non_executable_selection_creates_nothing(self):
        selected = self.launcher()
        selected.chmod(0o600)
        with patch.object(ai_team.subprocess, "run") as run:
            self.assertEqual(self.install("--python-cli", str(selected)), 1)
        run.assert_not_called()
        self.assert_no_installation()

    def test_failed_or_unexpected_probe_creates_nothing_and_hides_details(self):
        selected = self.launcher()
        failures = (
            subprocess.CompletedProcess([], 1, stdout=b""),
            subprocess.CompletedProcess([], 0, stdout=ai_team._PYTHON_PROBE_OUTPUT + b"extra"),
            subprocess.CompletedProcess([], 0, stdout=b""),
            subprocess.TimeoutExpired("private command", 10),
            OSError("private command"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(ai_team.subprocess, "run", **options) as run:
                    self.assertEqual(self.install("--python-cli", str(selected)), 1)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][1:4], ["-I", "-S", "-c"])
                self.assert_no_installation()
                self.assertNotIn("private command", self.stderr.getvalue())

    def test_invalid_selection_can_be_corrected_without_removing_markers(self):
        selected = self.launcher()
        with patch.object(ai_team.subprocess, "run", side_effect=self.successful_run) as run:
            self.assertEqual(self.install("--python-cli", str(self.root / "missing.exe")), 1)
            run.assert_not_called()
            self.assert_no_installation()
            self.assertEqual(self.install("--python-cli", str(selected)), 0)
        self.assertEqual(self.registered_command(run)[5], ai_team._cli_path(selected))

    def test_unavailable_or_unsupported_default_creates_nothing(self):
        for selected, version in (("", (3, 11)), (str(self.root / "missing.exe"), (3, 11)),
                                  (sys.executable, (3, 9))):
            with self.subTest(selected=selected, version=version), \
                 patch.object(ai_team.sys, "executable", selected), \
                 patch.object(ai_team.sys, "version_info", version), \
                 patch.object(ai_team.subprocess, "run") as run:
                self.assertEqual(self.install(), 1)
                run.assert_not_called()
                self.assert_no_installation()

    @unittest.skipIf(os.name == "nt", "POSIX launcher links")
    def test_default_symlink_launcher_is_not_replaced_with_its_target(self):
        target = self.launcher("versions/python-1.0.0")
        stable = self.root / "python"
        stable.symlink_to(target)
        with patch.object(ai_team.sys, "executable", str(stable)), \
             patch.object(ai_team.subprocess, "run", side_effect=self.successful_run) as run:
            self.assertEqual(self.install(), 0)
        self.assertEqual(self.registered_command(run)[5], str(stable))

    def test_python_selection_is_install_only(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            ai_team.main(["serve", "--python-cli", sys.executable])
        self.assertEqual(raised.exception.code, 2)
        self.assert_no_installation()


if __name__ == "__main__":
    unittest.main()
