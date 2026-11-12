"""Compare install preflight with the real adapter constructors, offline."""
from pathlib import Path
import tempfile
import unittest

import ai_team
from claude.claude_adapter import ClaudeAdapter
from grok.grok_adapter import GrokAdapter


class InstallationRulesTests(unittest.TestCase):
    def test_preflight_and_constructors_accept_the_same_model_and_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            pairs = (("claude", "claude-synthetic", "medium", True),
                     ("claude", "claude-synthetic", "xhigh", True),
                     ("claude", "bad model", "medium", False),
                     ("claude", "claude-synthetic", "extreme", False),
                     ("grok", "grok-synthetic", "xhigh", True),
                     ("grok", "bad model", "medium", False),
                     ("grok", "grok-synthetic", "max", False))
            for provider, model, effort, accepted in pairs:
                with self.subTest(provider=provider, model=model, effort=effort):
                    args = ai_team.parser().parse_args(["install", "--grok-model", "grok-synthetic", f"--{provider}-model", model,
                                                        f"--{provider}-effort", effort])
                    rejected = ai_team.rejected_install_options(args)
                    self.assertEqual(not bool(rejected), accepted)
                    def make_adapter():
                        if provider == "claude":
                            return ClaudeAdapter(str(root / "cli"), str(root / "profile"),
                                                 str(root / "work"), model, effort)
                        return GrokAdapter(str(root / "cli"), model, effort, str(root / "profile"))
                    if accepted:
                        make_adapter()
                    else:
                        with self.assertRaises(ValueError):
                            make_adapter()


if __name__ == "__main__":
    unittest.main()
