"""Regression checks for the offline CI runner."""
import subprocess
import sys
import tempfile
import unittest

from run_offline_tests import DISCOVERY


class OfflineRunnerTests(unittest.TestCase):
    def test_empty_suite_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-B", "-c", DISCOVERY],
                                    cwd=directory, capture_output=True, text=True,
                                    check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No tests discovered", result.stderr)
