"""Windows-only file identity tests with newly created synthetic paths."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

if os.name == "nt":
    import win_session_handle as handles


@unittest.skipUnless(os.name == "nt", "Windows file handles required")
class WindowsHandleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session-handle-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "sessions"
        self.directory.mkdir()
        self.session = str(uuid.uuid4())
        self.file = self.directory / (self.session + ".jsonl")
        self.other = self.directory / "synthetic-other"

    def test_only_expected_file_is_deleted(self):
        self.file.write_bytes(b"synthetic")
        self.other.write_bytes(b"other")
        self.assertEqual(handles.read_then_mark_exact_session_for_deletion(self.directory, self.session), b"synthetic")
        self.assertFalse(self.file.exists())
        self.assertEqual(self.other.read_bytes(), b"other")

    def test_invalid_id_oversize_and_failed_validation_keep_file(self):
        self.file.write_bytes(b"synthetic")
        for kwargs in ({"session": "../other"}, {"session": self.session, "max_bytes": 2},
                       {"session": self.session, "validate_before_delete": lambda raw: False},
                       {"session": self.session, "validate_file": lambda handle: False},
                       {"session": self.session, "validate_directory": lambda path, handle: False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(handles.UnsafeSessionFile):
                handles.read_then_mark_exact_session_for_deletion(self.directory, **kwargs)
            self.assertTrue(self.file.exists())

    def test_hardlink_is_rejected_and_both_names_remain(self):
        self.file.write_bytes(b"synthetic")
        try:
            os.link(self.file, self.other)
        except OSError:
            self.skipTest("creating a synthetic hardlink is unavailable")
        with self.assertRaises(handles.UnsafeSessionFile):
            handles.read_then_mark_exact_session_for_deletion(self.directory, self.session)
        self.assertTrue(self.file.exists())
        self.assertTrue(self.other.exists())

    def test_target_symlink_is_not_followed(self):
        self.other.write_bytes(b"other")
        try:
            os.symlink(self.other, self.file)
        except OSError:
            self.skipTest("creating a synthetic symlink is unavailable")
        with self.assertRaises(handles.UnsafeSessionFile):
            handles.read_then_mark_exact_session_for_deletion(self.directory, self.session)
        self.assertEqual(self.other.read_bytes(), b"other")

    def test_directory_junction_is_not_followed(self):
        self.file.write_bytes(b"synthetic")
        junction = self.root / "junction"
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(self.directory)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
        if result.returncode:
            self.skipTest("creating a synthetic junction is unavailable")
        try:
            with self.assertRaises(handles.UnsafeSessionFile):
                handles.read_then_mark_exact_session_for_deletion(junction, self.session)
            self.assertTrue(self.file.exists())
        finally:
            os.rmdir(junction)

    def test_close_failure_cannot_report_success(self):
        self.file.write_bytes(b"synthetic")
        close = handles._kernel.CloseHandle
        def close_but_fail(handle):
            close(handle)
            return 0
        with patch.object(handles._kernel, "CloseHandle", side_effect=close_but_fail), \
                self.assertRaisesRegex(handles.UnsafeSessionFile, "handle_close_unconfirmed"):
            handles.read_then_mark_exact_session_for_deletion(self.directory, self.session)


if __name__ == "__main__":
    unittest.main()
