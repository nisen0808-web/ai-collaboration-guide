"""Boot evidence checks with synthetic event XML; no reboot or live ledger use."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import boot_evidence as boot

FIRST = "00000000-0000-4000-8000-000000000001"
SECOND = "00000000-0000-4000-8000-000000000002"
START = "2026-11-01T10:00:00.1234567Z"
LATER = "2026-11-02T10:00:00.1234567Z"
EARLIER = "2020-01-01T00:00:00.0000000Z"


def event_xml(number=100, start=START, created=None, computer="private-computer"):
    return (f'<Events><Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">'
            f'<System><Provider Name="Microsoft-Windows-Kernel-General"/>'
            f'<EventID>12</EventID><TimeCreated SystemTime="{created or start}"/>'
            f'<EventRecordID>{number}</EventRecordID><Channel>System</Channel>'
            f'<Computer>{computer}</Computer><Security UserID="private-user"/></System>'
            f'<EventData><Data Name="StartTime">{start}</Data>'
            '<Data Name="BootMode">0</Data></EventData></Event></Events>').encode("utf-16")


def completed(data, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=data)


class LinuxEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.platform = mock.patch.object(boot.sys, "platform", "linux").start()
        self.reader = mock.patch.object(boot.Path, "read_text", return_value=FIRST + "\n").start()
        self.addCleanup(mock.patch.stopall)

    def test_same_and_different_canonical_boot_ids(self):
        saved = boot.snapshot()
        self.assertEqual(saved, {"kind": "linux_boot_id", "boot_id": FIRST})
        self.assertFalse(boot.changed(saved))
        self.reader.return_value = SECOND
        with mock.patch("time.time", side_effect=AssertionError("must not read wall clock")):
            self.assertTrue(boot.changed(saved))
        self.reader.assert_called_with(encoding="ascii")

    def test_unavailable_or_noncanonical_id_never_falls_back(self):
        for value in ("-" * 36, FIRST.upper().replace("00000001", "ABCDEF01"),
                      FIRST.replace("-", ""), "", "not-an-id"):
            with self.subTest(value=value):
                self.reader.return_value = value
                with self.assertRaisesRegex(ValueError, "^boot_evidence_unavailable$"):
                    boot.snapshot()
        for failure in (OSError("private path"), UnicodeError("private data")):
            self.reader.side_effect = failure
            with self.assertRaisesRegex(ValueError, "^boot_evidence_unavailable$"):
                boot.changed({"kind": "linux_boot_id", "boot_id": FIRST})

    def test_invalid_saved_evidence_is_rejected_before_read(self):
        for value in (None, {}, {"kind": "unknown"}, {"kind": "linux_boot_id", "boot_id": True},
                      {"kind": "linux_boot_id", "boot_id": "-" * 36},
                      {"kind": "linux_boot_id", "boot_id": FIRST, "extra": "x"}):
            with self.subTest(value=value):
                self.reader.reset_mock()
                with self.assertRaisesRegex(ValueError, "^invalid_boot_evidence$"):
                    boot.changed(value)
                self.reader.assert_not_called()

    def test_unsupported_platform_has_fixed_reason(self):
        with mock.patch.object(boot.sys, "platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "^unsupported_recovery_platform$"):
                boot.snapshot()


class WindowsEvidenceTests(unittest.TestCase):
    def setUp(self):
        mock.patch.object(boot.sys, "platform", "win32").start()
        self.system_root = str(Path(tempfile.gettempdir()) / "test-windows")
        mock.patch.dict(os.environ, {"SystemRoot": self.system_root}).start()
        self.run = mock.patch.object(boot.subprocess, "run", return_value=completed(event_xml())).start()
        self.addCleanup(mock.patch.stopall)

    def test_snapshot_uses_local_readonly_bounded_command_and_stores_no_private_fields(self):
        saved = boot.snapshot()
        self.assertEqual(set(saved), {"kind", "record_id", "event_hash"})
        self.assertEqual(saved["kind"], "windows_event12")
        self.assertEqual(saved["record_id"], 100)
        self.assertRegex(saved["event_hash"], r"^[a-f0-9]{64}$")
        self.assertNotIn("private", json.dumps(saved))
        command = self.run.call_args.args[0]
        self.assertEqual(command[0], str(Path(self.system_root) / "System32" / "wevtutil.exe"))
        self.assertEqual(command[1:3], ["qe", "System"])
        self.assertIn("Provider[@Name='Microsoft-Windows-Kernel-General'] and EventID=12", command[3])
        for argument in ("/rd:true", "/c:1", "/f:xml", "/e:Events", "/uni:true"):
            self.assertIn(argument, command)
        self.assertFalse(any(argument.startswith(("/r:", "/u:", "/p:")) for argument in command))
        options = self.run.call_args.kwargs
        self.assertIs(options["shell"], False)
        self.assertEqual(options["timeout"], 10)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.run.return_value = completed(event_xml(computer="another-computer"))
        self.assertEqual(saved, boot.snapshot())

    def test_same_startup_is_false_and_history_is_queried_by_id(self):
        saved = boot.snapshot()
        self.run.reset_mock()
        self.assertFalse(boot.changed(saved))
        calls = self.run.call_args_list
        self.assertIn("EventRecordID=100", calls[0].args[0][3])
        self.assertNotIn("EventRecordID=", calls[1].args[0][3])

    def test_later_record_with_different_start_accepts_both_clock_directions(self):
        saved = boot.snapshot()
        for start in (LATER, EARLIER):
            with self.subTest(start=start):
                self.run.side_effect = [completed(event_xml()), completed(event_xml(200, start))]
                with mock.patch("time.time", side_effect=AssertionError("must not read wall clock")):
                    self.assertTrue(boot.changed(saved))

    def test_same_start_time_repeated_in_later_record_is_not_another_start(self):
        saved = boot.snapshot()
        self.run.side_effect = [completed(event_xml()), completed(event_xml(200, START, LATER))]
        self.assertFalse(boot.changed(saved))

    def test_missing_or_replaced_history_refuses(self):
        saved = boot.snapshot()
        for old in (event_xml(100, LATER), event_xml(99), b"<Events/>"):
            with self.subTest(old=old):
                self.run.side_effect = [completed(old)]
                with self.assertRaisesRegex(ValueError, "^boot_evidence_unavailable$"):
                    boot.changed(saved)

    def test_log_reset_or_record_replacement_during_query_refuses(self):
        saved = boot.snapshot()
        for current in (event_xml(99, LATER), event_xml(100, LATER)):
            with self.subTest(current=current):
                self.run.side_effect = [completed(event_xml()), completed(current)]
                with self.assertRaisesRegex(ValueError, "^boot_evidence_unavailable$"):
                    boot.changed(saved)

    def test_bad_xml_and_wrong_event_identity_refuse(self):
        original = event_xml().decode("utf-16")
        variants = [b"", b"not XML", b"<Events/>", b"x" * 65537,
                    original.replace('Name="Microsoft-Windows-Kernel-General"', 'Name="Other"').encode(),
                    original.replace("<EventID>12</EventID>", "<EventID>13</EventID>").encode(),
                    original.replace("<Channel>System</Channel>", "<Channel>Application</Channel>").encode(),
                    original.replace("<EventRecordID>100</EventRecordID>", "<EventRecordID>0</EventRecordID>").encode(),
                    original.replace("<EventRecordID>100</EventRecordID>",
                                     "<EventRecordID>18446744073709551616</EventRecordID>").encode(),
                    original.replace(f'<Data Name="StartTime">{START}</Data>', "").encode(),
                    original.replace("</EventData>", f'<Data Name="StartTime">{START}</Data></EventData>').encode(),
                    original.replace(START, "not-a-time").encode(),
                    original.replace("</Events>", original[8:]).encode()]
        for data in variants:
            with self.subTest(data=data[:80]):
                self.run.return_value = completed(data)
                with self.assertRaisesRegex(ValueError, "^boot_evidence_unavailable$"):
                    boot.snapshot()

    def test_command_failure_and_timeout_have_fixed_reason(self):
        self.run.return_value = completed(b"private details", 5)
        with self.assertRaisesRegex(ValueError, "^boot_evidence_unavailable$"):
            boot.snapshot()
        for failure in (OSError("private command path"),
                        subprocess.TimeoutExpired("private command", 10, output=b"private data")):
            self.run.side_effect = failure
            with self.assertRaisesRegex(ValueError, "^boot_evidence_unavailable$"):
                boot.snapshot()

    def test_invalid_ticket_does_not_query_log(self):
        saved = boot.snapshot()
        variants = [None, {}, dict(saved, kind="linux_boot_id"), dict(saved, record_id=True),
                    dict(saved, record_id=0), dict(saved, record_id=2 ** 64),
                    dict(saved, record_id="100 or EventID=1"), dict(saved, event_hash="private"),
                    dict(saved, extra="private")]
        for value in variants:
            with self.subTest(value=value):
                self.run.reset_mock()
                with self.assertRaisesRegex(ValueError, "^invalid_boot_evidence$"):
                    boot.changed(value)
                self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
