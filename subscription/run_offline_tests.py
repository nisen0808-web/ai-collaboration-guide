"""Run synthetic unit and integration checks without contacting providers."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main() -> int:
    failed = False
    for name, directory in (("core", ROOT / "core"), ("claude", ROOT / "claude"),
                            ("grok", ROOT / "grok"), ("integration", ROOT)):
        print(f"\nOffline suite: {name}", flush=True)
        completed = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-v"],
                                   cwd=directory, check=False)
        failed = failed or completed.returncode != 0
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
