"""The durable worker must not modify a read-only source snapshot with bytecode."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import durable_claude as durable


class WorkerBytecodeTests(unittest.TestCase):
    def test_real_worker_imports_do_not_create_bytecode(self):
        # Everything is synthetic: no credentials, client process or provider.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            for name in ("durable_claude.py", "claude_adapter.py"):
                shutil.copyfile(Path(durable.__file__).parent / name, source / name)
            profile, work, state = (root / name for name in ("profile", "work", "state"))
            for directory in (profile, work, state):
                directory.mkdir()
            adapter = durable.DurableClaudeAdapter(str(root / "fake-cli"), str(profile), str(work),
                                                   str(state), "model-example", "medium", enabled=True)
            fake_process = Mock()
            with patch.object(durable, "_WORKER_SCRIPT", str(source / "durable_claude.py")), \
                 patch.object(durable.subprocess, "Popen", return_value=fake_process) as spawn:
                receipt = adapter.submit("synthetic task", "synthetic-job")
            self.assertEqual(receipt["status"], "queued")
            self.assertEqual(spawn.call_count, 1)
            command = spawn.call_args.args[0]
            self.assertEqual(command[:3], [sys.executable, "-B", str(source / "durable_claude.py")])

            # Re-run the exact captured worker command with empty input: it
            # imports real modules but cannot reach the synthetic provider.
            cache = root / "redirected-cache"
            env = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")
                   if name in os.environ}
            env["PYTHONPYCACHEPREFIX"] = str(cache)
            completed = subprocess.run(command, input=b"", stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, cwd=work, env=env,
                                       timeout=15, check=False)
            self.assertEqual(completed.returncode, 0)
            self.assertFalse(list(root.rglob("*.pyc")))
            self.assertFalse(list(source.rglob("__pycache__")))

            # Negative control: the same real Python/module imports without
            # -B do create bytecode, proving this check can detect regression.
            no_b = [command[0], *command[2:]]
            control = subprocess.run(no_b, input=b"", stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=work, env=env,
                                     timeout=15, check=False)
            self.assertEqual(control.returncode, 0)
            self.assertTrue(any(path.name.startswith("claude_adapter.") for path in cache.rglob("*.pyc")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
