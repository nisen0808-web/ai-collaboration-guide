"""Offline tests for manual-keyboard probe evidence validation; no console launch."""
import copy
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("keyboard_probe", Path(__file__).parent / "tools" / "windows_keyboard_interrupt.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def evidence(scenario):
    control = scenario == "startup_failure_no_signal"
    unfinished = {"status": "submission_uncertain", "retry": "not_attempted",
                  "reason": "worker_start_unconfirmed" if control else "interrupted_submission_status_unknown"}
    claude = copy.deepcopy(unfinished) if scenario == "startup_failure" else {
        "status": "complete_unreviewed", "output": probe.CONTROL if control else probe.EARLY}
    result = {"providers": {"claude": claude, "grok": unfinished}, "note": "No retry was attempted."}
    meta = {"sigint_count": 0 if control else 1, "network_attempts": 0, "source_sha256": "abc",
            "start_attempts": 2, "second_start_failed": scenario != "normal",
            "provider_calls": ["claude", "grok"] if scenario == "normal" else ["claude"]}
    return [scenario, 1 if control else 130, result, meta, b"", "abc"]


class KeyboardProbeTests(unittest.TestCase):
    def test_all_expected_evidence(self):
        for scenario in probe.SCENARIOS:
            with self.subTest(scenario=scenario):
                probe.validate(*evidence(scenario))

    def test_rejects_lost_completed_result(self):
        args = evidence("normal")
        args[2]["providers"]["claude"]["output"] = "lost"
        with self.assertRaisesRegex(ValueError, "retained"):
            probe.validate(*args)

    def test_rejects_missing_signal_network_retry_source_and_exit(self):
        mutations = [lambda a: a[3].update(sigint_count=0), lambda a: a[3].update(network_attempts=1),
                     lambda a: a[3]["provider_calls"].append("grok"),
                     lambda a: a[2]["providers"]["grok"].update(retry="attempted"),
                     lambda a: a[3].update(source_sha256="changed"), lambda a: a.__setitem__(1, 0)]
        for mutate in mutations:
            args = evidence("normal")
            mutate(args)
            with self.assertRaises(ValueError):
                probe.validate(*args)

    def test_second_start_and_no_signal_control_are_distinct(self):
        args = evidence("startup_failure")
        args[3]["second_start_failed"] = False
        with self.assertRaises(ValueError):
            probe.validate(*args)
        args = evidence("startup_failure_no_signal")
        args[3]["sigint_count"] = 1
        with self.assertRaises(ValueError):
            probe.validate(*args)

    def test_environment_drops_credentials_and_python_overrides(self):
        with patch.dict(os.environ, {"SystemRoot": "Windows", "COLLAB_XAI_KEY": "private",
                                     "ANTHROPIC_API_KEY": "private", "PYTHONPATH": "unsafe"}, clear=True):
            self.assertEqual({key.upper(): value for key, value in probe.minimal_environment().items()},
                             {"SYSTEMROOT": "Windows"})


if __name__ == "__main__":
    unittest.main()
