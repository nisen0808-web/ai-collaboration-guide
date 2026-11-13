"""Offline checks for the model-listing command; no client or network is used."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ai_team
from grok import grok_adapter


class ModelsCommandTests(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ai_team.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_adapter_runs_models_in_the_isolated_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cli, profile = root / "cli", root / "profile"
            cli.touch()
            profile.mkdir()
            adapter = grok_adapter.GrokAdapter(str(cli), "example-model", "high", str(profile))
            with patch.dict("os.environ", {"XAI_API_KEY": "synthetic-key"}), \
                 patch.object(grok_adapter.subprocess, "call", return_value=0) as call:
                self.assertEqual(adapter.list_models(), 0)
            command = call.call_args.args[0]
            options = call.call_args.kwargs
            self.assertEqual(command, [str(cli), "models"])
            self.assertIs(options["shell"], False)
            self.assertEqual(options["env"]["GROK_HOME"], str(profile))
            self.assertNotEqual(options["env"]["HOME"], str(Path.home()))
            self.assertNotIn("XAI_API_KEY", options["env"])
            self.assertFalse(Path(options["cwd"]).exists())

    def test_command_reports_outcomes_without_opening_job_ledgers(self):
        with tempfile.TemporaryDirectory() as tmp:
            for listed, expected in ((0, 0), (2, 1), (None, 1)):
                with self.subTest(listed=listed), \
                     patch.object(ai_team, "executable", return_value="/synthetic/grok"), \
                     patch.object(ai_team, "build_adapters", side_effect=AssertionError("opened job ledgers")), \
                     patch.object(ai_team.GrokLoginAdapter, "list_models", return_value=listed):
                    code, _, err = self.run_main("models", "grok", "--data-dir", tmp)
                self.assertEqual(code, expected)
                self.assertEqual("not verified" in err, expected == 1)
            with patch.object(ai_team, "executable", return_value=None):
                code, _, err = self.run_main("models", "grok", "--data-dir", tmp)
            self.assertEqual(code, 1)
            self.assertIn("not found", err)
            with patch.object(ai_team, "executable", return_value="/synthetic/grok"), \
                 patch.object(ai_team.GrokLoginAdapter, "list_models", side_effect=OSError("C:/private/path")):
                code, out, err = self.run_main("models", "grok", "--data-dir", tmp)
            self.assertEqual(code, 1)
            self.assertNotIn("private", out + err)

    def test_only_grok_is_offered(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ai_team.parser().parse_args(["models", "claude"])


if __name__ == "__main__":
    unittest.main()
