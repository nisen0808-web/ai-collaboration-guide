"""Synthetic evidence tests; never open an account profile or run a model."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

import session_evidence as evidence


class SessionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.profile = str(base / "profile")
        self.work = str(base / "work")
        self.project = Path(self.profile) / "projects" / "synthetic-work"
        self.session = str(uuid.uuid4())
        self.model = "claude-synthetic"
        self.effort = "medium"

    def raw(self, rows):
        return ("\n".join(json.dumps(row) for row in rows) + "\n").encode()

    def row(self, **overrides):
        row = {"type": "assistant", "sessionId": self.session,
               "message": {"model": self.model, "content": "synthetic-secret"},
               "effort": self.effort}
        row.update(overrides)
        return row

    def test_disabled_or_exit_unknown_never_open_session(self):
        with patch.object(evidence, "_native", side_effect=AssertionError("opened")):
            disabled = evidence.collect_session_evidence(self.profile, self.work, self.session,
                self.model, self.effort, process_exit_confirmed=True)
            unknown = evidence.collect_session_evidence(self.profile, self.work, self.session,
                self.model, self.effort, process_exit_confirmed=False, enabled=True)
            self.assertEqual(disabled["client_evidence_status"], "disabled")
            self.assertEqual(unknown["session_cleanup"], "not_attempted")
            self.assertFalse(evidence.prepare_session_evidence(self.profile, self.work, self.session))

    def test_projection_rejects_bad_or_cross_session_records(self):
        for raw in (self.raw([self.row(sessionId=str(uuid.uuid4()))]),
                    b'{"type":"assistant","sessionId":"x","sessionId":"y"}',
                    self.raw([{"type": "user", "sessionId": self.session}]), b'{invalid'):
            with self.subTest(size=len(raw)), self.assertRaises((ValueError, UnicodeError)):
                evidence._projection(raw, self.session, self.model, self.effort)

    def test_projection_requires_every_assistant_to_match(self):
        self.assertEqual(evidence._projection(self.raw([self.row(), self.row()]), self.session,
                                             self.model, self.effort), (True, True))
        self.assertEqual(evidence._projection(self.raw([self.row(), self.row(effort="high")]), self.session,
                                             self.model, self.effort), (True, False))
        self.assertEqual(evidence._projection(self.raw([self.row(message={})]), self.session,
                                             self.model, self.effort), (False, True))

    def collect_fake(self, raw, *, acl_ok=True, close_ok=True, durable_saved=False):
        calls = []
        class UnsafeSessionFile(Exception):
            pass
        fake_acl = SimpleNamespace(protected_private_acl=lambda *args, **kwargs: acl_ok)
        def read(project, session, **kwargs):
            calls.append((project, session))
            if not kwargs["validate_file"](object()):
                raise UnsafeSessionFile("file_acl_unconfirmed")
            if not kwargs["validate_directory"](project, object()):
                raise UnsafeSessionFile("directory_acl_unconfirmed")
            if not kwargs["validate_before_delete"](raw):
                raise UnsafeSessionFile("session_identity_unconfirmed")
            return raw
        fake_handles = SimpleNamespace(read_then_mark_exact_session_for_deletion=read,
                                       UnsafeSessionFile=UnsafeSessionFile)
        with patch.object(evidence, "_native", return_value=(fake_handles, fake_acl)), \
             patch.object(evidence, "_context", return_value=self.project), \
             patch.object(evidence, "_close", return_value=close_ok):
            out = evidence.collect_session_evidence(self.profile, self.work, self.session,
                self.model, self.effort, process_exit_confirmed=True, enabled=True,
                durable_result_saved=durable_saved)
        return out, calls

    def test_only_expected_session_is_projected_and_no_content_escapes(self):
        out, calls = self.collect_fake(self.raw([self.row()]))
        self.assertEqual(calls, [(self.project, self.session)])
        self.assertEqual(out["client_evidence_status"], "matched")
        self.assertTrue(out["client_model_matches_requested"])
        self.assertTrue(out["client_effort_matches_requested"])
        self.assertFalse(out["server_effort_verified"])
        self.assertEqual(out["session_cleanup"], "retained")
        self.assertNotIn("synthetic-secret", repr(out))
        self.assertNotIn(self.session, repr(out))
        self.assertNotIn(self.profile, repr(out))

    def test_bad_metadata_retained_or_unknown_without_false_proof(self):
        out, _ = self.collect_fake(self.raw([self.row(effort="high")]))
        self.assertEqual(out["client_evidence_status"], "unavailable")
        self.assertIsNone(out["client_effort_matches_requested"])
        self.assertEqual(out["session_cleanup"], "retained")
        out, _ = self.collect_fake(self.raw([self.row(sessionId=str(uuid.uuid4()))]))
        self.assertEqual(out["session_cleanup"], "retained")
        self.assertIsNone(out["client_model_matches_requested"])

    def test_acl_or_handle_close_failure_does_not_claim_evidence(self):
        out, _ = self.collect_fake(self.raw([self.row()]), acl_ok=False)
        self.assertEqual(out["client_evidence_status"], "unavailable")
        self.assertEqual(out["session_cleanup"], "retained")
        out, _ = self.collect_fake(self.raw([self.row()]), close_ok=False)
        self.assertEqual(out["client_evidence_status"], "unavailable")
        self.assertEqual(out["session_cleanup"], "unconfirmed")

    def test_cleanup_requires_explicit_durable_result_commit(self):
        out, _ = self.collect_fake(self.raw([self.row()]), durable_saved=True)
        self.assertEqual(out["client_evidence_status"], "matched")
        self.assertEqual(out["session_cleanup"], "deletion_requested")
        out, _ = self.collect_fake(self.raw([self.row()]), durable_saved=1)
        self.assertEqual(out["session_cleanup"], "retained")

    def test_bad_inputs_do_not_access_files(self):
        with patch.object(evidence, "_native", side_effect=AssertionError("opened")):
            out = evidence.collect_session_evidence(self.profile, self.work, "../other",
                self.model, self.effort, process_exit_confirmed=True, enabled=True)
            self.assertEqual(out["client_evidence_status"], "unavailable")
            self.assertEqual(out["session_cleanup"], "not_attempted")


if __name__ == "__main__":
    unittest.main()
