"""Synthetic filesystem tests; never access real sessions or providers."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote
import uuid

import posix_session_reader as subject


class PlatformTest(unittest.TestCase):
    def test_unsupported_platform_fails_closed(self):
        with mock.patch.object(subject, '_supported', return_value=False):
            self.assertFalse(subject.preflight('/synthetic/profile', '/synthetic/work', str(uuid.uuid4())))
            self.assertIsNone(subject.read_records('/synthetic/profile', '/synthetic/work', str(uuid.uuid4())))


@unittest.skipUnless(subject._supported(), 'requires safe Linux dir_fd and O_NOFOLLOW support')
class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profile = self.root / 'profile'
        self.work = self.root / 'work'
        self.profile.mkdir(mode=0o700)
        self.work.mkdir(mode=0o700)
        self.session = str(uuid.uuid4())
        self.bucket = self.profile / 'sessions' / quote(str(self.work), safe='')
        self.target = self.bucket / self.session

    def tearDown(self):
        self.tmp.cleanup()

    def _new_records(self, summary=b'{"synthetic":true}', usage=b'{"synthetic":true}'):
        self.target.mkdir(parents=True, mode=0o700)
        # Intermediates default to 0777 masked by umask; force true privacy.
        (self.profile / 'sessions').chmod(0o700)
        self.bucket.chmod(0o700)
        self.target.chmod(0o700)
        for name, data in (('summary.json', summary), ('usage.json', usage)):
            path = self.target / name
            path.write_bytes(data)
            path.chmod(0o600)

    def test_absent_then_read_only_exact_session(self):
        self.assertTrue(subject.preflight(str(self.profile), str(self.work), self.session))
        self._new_records(b'{"summary":1}', b'{"usage":2}')
        self.assertFalse(subject.preflight(str(self.profile), str(self.work), self.session))
        self.assertEqual(subject.read_records(str(self.profile), str(self.work), self.session),
                         (b'{"summary":1}', b'{"usage":2}'))
        self.assertEqual((self.target / 'summary.json').read_bytes(), b'{"summary":1}')

    def test_file_symlink_rejected(self):
        self._new_records()
        (self.target / 'usage.json').unlink()
        (self.target / 'usage.json').symlink_to(self.target / 'summary.json')
        self.assertIsNone(subject.read_records(str(self.profile), str(self.work), self.session))

    def test_directory_symlink_rejected_before_launch(self):
        original = self.profile / 'sessions'
        outside = self.root / 'outside'
        outside.mkdir(mode=0o700)
        original.symlink_to(outside, target_is_directory=True)
        self.assertFalse(subject.preflight(str(self.profile), str(self.work), self.session))

    def test_hard_linked_metadata_rejected(self):
        self._new_records()
        os.link(self.target / 'summary.json', self.root / 'other-link')
        self.assertIsNone(subject.read_records(str(self.profile), str(self.work), self.session))

    def test_unprotected_profile_work_or_metadata_rejected(self):
        self.profile.chmod(0o755)
        self.assertFalse(subject.preflight(str(self.profile), str(self.work), self.session))
        self.profile.chmod(0o700)
        self.work.chmod(0o755)
        self.assertFalse(subject.preflight(str(self.profile), str(self.work), self.session))
        self.work.chmod(0o700)
        self._new_records()
        (self.target / 'usage.json').chmod(0o664)
        self.assertIsNone(subject.read_records(str(self.profile), str(self.work), self.session))

    def test_default_umask_descendants_are_protected_by_private_profile(self):
        self.assertTrue(subject.preflight(str(self.profile), str(self.work), self.session))
        self._new_records()
        for directory in (self.profile / 'sessions', self.bucket, self.target):
            directory.chmod(0o755)
        for name in ('summary.json', 'usage.json'):
            (self.target / name).chmod(0o644)
        self.assertEqual(subject.read_records(str(self.profile), str(self.work), self.session),
                         (b'{"synthetic":true}', b'{"synthetic":true}'))

    def test_group_writable_session_dir_or_metadata_rejected(self):
        self._new_records()
        self.target.chmod(0o775)
        self.assertIsNone(subject.read_records(str(self.profile), str(self.work), self.session))
        self.target.chmod(0o755)
        (self.target / 'usage.json').chmod(0o664)
        self.assertIsNone(subject.read_records(str(self.profile), str(self.work), self.session))

    def test_extended_acl_or_unreadable_acl_rejected(self):
        with mock.patch.object(subject.os, 'getxattr', return_value=b'private-looking-acl'):
            self.assertFalse(subject.preflight(str(self.profile), str(self.work), self.session))
        with mock.patch.object(subject.os, 'getxattr', side_effect=PermissionError()):
            self.assertFalse(subject.preflight(str(self.profile), str(self.work), self.session))

    def test_missing_or_oversized_records_are_unknown(self):
        self._new_records(summary=b'x' * 33)
        self.assertIsNone(subject.read_records(str(self.profile), str(self.work), self.session, max_bytes=32))
        (self.target / 'usage.json').unlink()
        self.assertIsNone(subject.read_records(str(self.profile), str(self.work), self.session))

    def test_malformed_uuid_and_long_encoded_work_rejected(self):
        self.assertFalse(subject.preflight(str(self.profile), str(self.work), 'not-a-uuid'))
        self.assertFalse(subject.preflight(str(self.profile), '/' + 'x' * 260, self.session))
        self.assertFalse(subject.preflight(str(self.profile), str(self.work) + '/../work', self.session))

    def test_only_selected_session_is_read(self):
        self._new_records()
        unrelated = self.bucket / str(uuid.uuid4())
        unrelated.mkdir(mode=0o700)
        (unrelated / 'summary.json').write_bytes(b'not-this-task')
        self.assertEqual(subject.read_records(str(self.profile), str(self.work), self.session),
                         (b'{"synthetic":true}', b'{"synthetic":true}'))
        self.assertEqual((unrelated / 'summary.json').read_bytes(), b'not-this-task')


if __name__ == '__main__':
    unittest.main()
