"""Verify installation reservation across real processes without Codex or network."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CHILD = r'''
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock

import ai_team

root = Path(sys.argv[1])
index = sys.argv[2]

def synthetic_registry(_cli, _env):
    # Both independent installers have passed their initial marker check and
    # entered the registry query before either is allowed to reserve a slot.
    (root / ("ready-" + index)).write_text("ready", encoding="ascii")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if all((root / ("ready-" + number)).is_file() for number in ("1", "2")):
            return True
        time.sleep(0.02)
    raise TimeoutError("synthetic registry barrier timed out")

def synthetic_add(command, **_kwargs):
    # Per-process files make two attempted adds visible without relying on
    # concurrent writes to one file.
    (root / ("add-" + index)).write_text("attempted", encoding="ascii")
    return subprocess.CompletedProcess(command, 0)

with mock.patch.object(ai_team, "executable", return_value="synthetic-client"), \
     mock.patch.object(ai_team, "codex_registry_available", side_effect=synthetic_registry), \
     mock.patch.object(ai_team.subprocess, "run", side_effect=synthetic_add), \
     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    result = ai_team.main(["install", "--grok-model", "synthetic-model",
                           "--data-dir", str(root / ("data-" + index))])
print(json.dumps({"result": result}))
'''


class CrossProcessInstallTests(unittest.TestCase):
    def test_same_codex_home_only_one_process_reaches_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home = root / "codex-home"
            home.mkdir()
            # Keep only platform variables needed for local child startup.
            # No API keys, provider settings, or original Codex config are
            # passed to the synthetic installer processes.
            allow = ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE")
            env = {name: os.environ[name] for name in allow if name in os.environ}
            env.update({"CODEX_HOME": str(home), "HOME": str(root), "USERPROFILE": str(root)})
            workers = []
            try:
                for index in ("1", "2"):
                    workers.append(subprocess.Popen(
                        [sys.executable, "-B", "-c", CHILD, str(root), index],
                        cwd=str(Path(__file__).resolve().parent), env=env,
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, shell=False,
                    ))
                results = []
                for worker in workers:
                    stdout, _stderr = worker.communicate(timeout=30)
                    self.assertEqual(worker.returncode, 0)
                    results.append(json.loads(stdout.decode("utf-8"))["result"])
                self.assertEqual(sorted(results), [0, 1])
                self.assertTrue((root / "ready-1").is_file())
                self.assertTrue((root / "ready-2").is_file())
                self.assertEqual(sum((root / ("add-" + index)).is_file() for index in ("1", "2")), 1)
                self.assertTrue((home / ".ai-collaboration-guide-install-attempt").is_file())
                self.assertEqual(sum((root / ("data-" + index) / "codex-install-attempt.txt").is_file()
                                     for index in ("1", "2")), 1)
            finally:
                # Only children created by this synthetic test may be stopped.
                for worker in workers:
                    if worker.poll() is None:
                        worker.kill()
                        worker.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
