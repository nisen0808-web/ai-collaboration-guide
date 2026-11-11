"""Run synthetic unit and integration checks without contacting providers."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent

# unittest discover can exit successfully when no tests are found on some
# supported Python versions. Reject an empty suite before reporting success.
DISCOVERY = """\
import sys
import unittest
suite = unittest.defaultTestLoader.discover('.')
if suite.countTestCases() == 0:
    print('No tests discovered', file=sys.stderr)
    sys.exit(1)
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
"""


def main() -> int:
    failed = False
    for name, directory in (("core", ROOT / "core"), ("claude", ROOT / "claude"),
                            ("grok", ROOT / "grok"), ("integration", ROOT)):
        print(f"\nOffline suite: {name}", flush=True)
        completed = subprocess.run([sys.executable, "-B", "-c", DISCOVERY],
                                   cwd=directory, check=False)
        failed = failed or completed.returncode != 0
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
