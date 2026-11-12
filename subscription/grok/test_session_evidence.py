"""Synthetic tests only: no account data or provider requests."""
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch
from urllib.parse import quote

import session_evidence as evidence


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.session = str(uuid.uuid4())
        self.work = Path(os.path.abspath("synthetic-work"))
        self.model = "grok-4.7"
        self.effort = "xhigh"
        self.summary = {"info": {"id": self.session, "cwd": str(self.work)},
                        "current_model_id": self.model, "reasoning_effort": self.effort,
                        "session_summary": "SYNTHETIC PRIVATE CONTENT"}
        self.usage = {"sessionId": self.session, "session": {"primaryModelId": "grok-4.7",
                      "modelUsage": {"grok-4.7-build": {"inputTokens": 2}}}}

    def project(self, summary=None, usage=None):
        return evidence._projection(json.dumps(self.summary if summary is None else summary).encode(),
                                    json.dumps(self.usage if usage is None else usage).encode(),
                                    self.session, self.work, self.model, self.effort)

    def test_exact_binding_and_known_alias(self):
        out = self.project()
        self.assertEqual(out["client_evidence_status"], "matched")
        self.assertTrue(out["client_model_matches_requested"])
        self.assertTrue(out["client_effort_matches_requested"])
        self.assertFalse(out["server_effort_verified"])
        self.assertNotIn("SYNTHETIC PRIVATE CONTENT", repr(out))
        self.assertNotIn(self.session, repr(out))

    def test_summary_and_usage_conflicts_report_mismatch(self):
        for usage in ({"modelUsage": {"grok-other": {"inputTokens": 2}}},
                      {"modelId": self.model, "modelUsage": {self.model: {"inputTokens": 2}},
                       "signals": {"modelsUsed": ["grok-other"]}}):
            out = self.project(usage=usage)
            self.assertEqual(out["client_evidence_status"], "mismatch")
            self.assertFalse(out["client_model_matches_requested"])
            self.assertNotIn("grok-other", repr(out))

    def test_effort_mismatch_reported_without_service_claim(self):
        summary = dict(self.summary, reasoning_effort="low")
        out = self.project(summary=summary)
        self.assertEqual(out["client_evidence_status"], "mismatch")
        self.assertFalse(out["client_effort_matches_requested"])
        self.assertFalse(out["server_effort_verified"])

    def test_unrecognized_or_missing_values_are_unavailable(self):
        for summary, usage in ((dict(self.summary, reasoning_effort="???"), self.usage),
                               (dict(self.summary, current_model_id=None), self.usage),
                               (self.summary, {}), (self.summary, {"modelUsage": {}}),
                               (self.summary, {"session": {"primaryModelId": self.model}}),
                               (self.summary, {"session": []}),
                               (self.summary, {"signals": {"modelsUsed": []}})):
            with self.assertRaises(ValueError):
                self.project(summary, usage)

    def test_conflicting_session_and_cwd_are_not_accepted(self):
        for summary, usage in ((dict(self.summary, info={"id": str(uuid.uuid4()), "cwd": str(self.work)}), self.usage),
                               (dict(self.summary, info={"id": self.session, "cwd": str(self.work) + "x"}), self.usage),
                               (self.summary, dict(self.usage, sessionId=str(uuid.uuid4()))),
                               (self.summary, {"session": {"sessionId": str(uuid.uuid4()), "modelId": self.model}})):
            with self.assertRaises(ValueError):
                self.project(summary, usage)

    def test_duplicate_keys_invalid_json_and_nonfinite_values_rejected(self):
        for raw in (b'{"a":1,"a":2}', b'{"x":NaN}', b'[]', b'{broken'):
            with self.assertRaises((ValueError, UnicodeError)):
                evidence._unique_json(raw)

    def test_alias_is_only_allowed_for_specific_requested_model(self):
        with self.assertRaises(ValueError):
            evidence._usage_models({"session": {"modelUsage": {"grok-4.7-build": None}}})
        out = evidence._projection(json.dumps(dict(self.summary, current_model_id="grok-4.8")).encode(),
                                   json.dumps(self.usage).encode(), self.session, self.work,
                                   "grok-4.8", self.effort)
        self.assertEqual(out["client_evidence_status"], "mismatch")

    def test_without_preflight_or_confirmed_exit_never_inspects(self):
        with patch.object(evidence, "_native", side_effect=AssertionError("must not inspect")):
            for preflight, exited in ((False, True), (True, False), (None, True)):
                out = evidence.collect(str(self.work), str(self.work), self.session, self.model, self.effort,
                                       preflight_ok=preflight, process_exit_confirmed=exited)
                self.assertEqual(out["client_evidence_status"], "unavailable")

    def test_invalid_uuid_or_cwd_slug_unavailable(self):
        with self.assertRaises(ValueError):
            evidence._inputs(str(self.work), str(self.work), "../other")
        with self.assertRaises(ValueError):
            evidence._inputs(str(self.work), str(self.work / ("z" * 256)), self.session)

    def test_corrupt_usage_model_field_fails_closed(self):
        for value in (None, 9, {}, ["model"]):
            with self.assertRaises(ValueError):
                self.project(usage={"modelId": value})

    def test_no_token_evidence_and_invalid_token_counts_fail_closed(self):
        for counters in ({}, {"inputTokens": 0}, {"inputTokens": True},
                         {"inputTokens": -1}, {"inputTokens": 1.0},
                         {"inputTokens": "2"}, {"inputTokens": 1, "outputTokens": False}):
            with self.subTest(counters=counters):
                with self.assertRaises(ValueError):
                    self.project(usage={"session": {"primaryModelId": self.model,
                                                    "modelUsage": {self.model: counters}}})
        with self.assertRaises(ValueError):
            self.project(usage={"signals": {"modelsUsed": [self.model]}})

    def test_posix_reader_branch_projects_only_bound_records(self):
        reader = SimpleNamespace(preflight=lambda *args: True,
                                 read_records=lambda *args, **kwargs:
                                 (json.dumps(self.summary).encode(), json.dumps(self.usage).encode()))
        with patch.object(evidence, "os", SimpleNamespace(name="posix", path=os.path)), \
             patch.object(evidence, "_posix_reader", return_value=reader):
            self.assertTrue(evidence.preflight(str(self.work), str(self.work), self.session))
            out = evidence.collect(str(self.work), str(self.work), self.session, self.model, self.effort,
                                   preflight_ok=True, process_exit_confirmed=True)
        self.assertEqual(out["client_evidence_status"], "matched")
        self.assertNotIn("SYNTHETIC PRIVATE CONTENT", repr(out))

    def test_posix_reader_absent_records_remain_unavailable(self):
        reader = SimpleNamespace(preflight=lambda *args: False, read_records=lambda *args, **kwargs: None)
        with patch.object(evidence, "os", SimpleNamespace(name="posix", path=os.path)), \
             patch.object(evidence, "_posix_reader", return_value=reader):
            self.assertFalse(evidence.preflight(str(self.work), str(self.work), self.session))
            out = evidence.collect(str(self.work), str(self.work), self.session, self.model, self.effort,
                                   preflight_ok=True, process_exit_confirmed=True)
        self.assertEqual(out["client_evidence_status"], "unavailable")


@unittest.skipUnless(os.name == "nt", "Windows ACL and handle checks required")
class WindowsSyntheticFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="grok-ev-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.profile, self.work = self.root / "p", self.root / "w"
        self.profile.mkdir(); self.work.mkdir()
        identity = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True)
        self.assertEqual(identity.returncode, 0)
        rows = list(csv.reader(io.StringIO(identity.stdout)))
        self.assertTrue(rows and rows[0][1].startswith("S-1-"))
        self.sid = rows[0][1]
        for path in (self.profile, self.work):
            self.owner(path)
            p = subprocess.run(["icacls.exe", str(path), "/inheritance:r", "/grant:r", "*" + self.sid + ":(OI)(CI)F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertEqual(p.returncode, 0)
        self.session = str(uuid.uuid4())
        self.parent = self.profile / "sessions" / quote(str(self.work), safe="")
        self.session_dir = self.parent / self.session

    def owner(self, path):
        logical = Path(str(path)[4:]) if str(path).startswith("\\\\?\\") else path
        self.assertTrue(logical.is_relative_to(self.root))
        p = subprocess.run(["icacls.exe", str(path), "/setowner", "*" + self.sid],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0)

    def create_records(self):
        self.session_dir.mkdir(parents=True)
        for path in (self.profile / "sessions", self.parent, self.session_dir):
            self.owner(path)
        for name, value in (("summary.json", {"info": {"id": self.session, "cwd": str(self.work)},
                                               "current_model_id": "grok-4.7", "reasoning_effort": "xhigh",
                                               "session_summary": "SYNTHETIC PRIVATE CONTENT"}),
                            ("usage.json", {"session": {"primaryModelId": "grok-4.7",
                                                       "modelUsage": {"grok-4.7-build": {"inputTokens": 2}}}})):
            p = self.session_dir / name
            p.write_text(json.dumps(value), encoding="utf-8")
            self.owner(p)

    def test_preflight_and_exact_read_without_deleting(self):
        self.assertTrue(evidence.preflight(str(self.profile), str(self.work), self.session))
        self.create_records()
        self.assertFalse(evidence.preflight(str(self.profile), str(self.work), self.session))
        out = evidence.collect(str(self.profile), str(self.work), self.session, "grok-4.7", "xhigh",
                               preflight_ok=True, process_exit_confirmed=True)
        self.assertEqual(out["client_evidence_status"], "matched")
        self.assertTrue((self.session_dir / "summary.json").exists())
        self.assertTrue((self.session_dir / "usage.json").exists())
        self.assertNotIn("SYNTHETIC PRIVATE CONTENT", repr(out))

    def test_rejects_hardlink_and_retains_unrelated_file(self):
        self.assertTrue(evidence.preflight(str(self.profile), str(self.work), self.session))
        self.create_records()
        original = self.session_dir / "summary.json"
        other = self.root / "other.json"
        os.link(original, other)
        out = evidence.collect(str(self.profile), str(self.work), self.session, "grok-4.7", "xhigh",
                               preflight_ok=True, process_exit_confirmed=True)
        self.assertEqual(out["client_evidence_status"], "unavailable")
        self.assertTrue(original.exists()); self.assertTrue(other.exists())

    def test_missing_usage_returns_unavailable(self):
        self.assertTrue(evidence.preflight(str(self.profile), str(self.work), self.session))
        self.create_records()
        (self.session_dir / "usage.json").unlink()
        out = evidence.collect(str(self.profile), str(self.work), self.session, "grok-4.7", "xhigh",
                               preflight_ok=True, process_exit_confirmed=True)
        self.assertEqual(out["client_evidence_status"], "unavailable")

    def test_long_windows_path_uses_extended_handles(self):
        # Keep the work path under 255 encoded bytes while forcing the final
        # metadata path past the legacy MAX_PATH boundary.
        original_work = self.work
        self.work = self.root / ("w" * 100)
        self.work.mkdir(); self.owner(self.work)
        proc = subprocess.run(["icacls.exe", str(self.work), "/inheritance:r", "/grant:r",
                               "*" + self.sid + ":(OI)(CI)F"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0)
        self.parent = self.profile / "sessions" / quote(str(self.work), safe="")
        self.session_dir = self.parent / self.session
        self.assertGreater(len(str(self.session_dir / "summary.json")), 260)
        self.assertTrue(evidence.preflight(str(self.profile), str(self.work), self.session))
        extended_session = evidence._extended(self.session_dir)
        try:
            extended_session.mkdir(parents=True)
            for path in (self.profile / "sessions", self.parent, self.session_dir):
                self.owner(evidence._extended(path))
            for name, value in (("summary.json", {"info": {"id": self.session, "cwd": str(self.work)},
                                                  "current_model_id": "grok-4.7", "reasoning_effort": "xhigh"}),
                                ("usage.json", {"session": {"primaryModelId": "grok-4.7",
                                                           "modelUsage": {"grok-4.7-build": {"inputTokens": 2}}}})):
                p = evidence._extended(self.session_dir / name)
                p.write_text(json.dumps(value), encoding="utf-8")
                self.owner(p)
            out = evidence.collect(str(self.profile), str(self.work), self.session, "grok-4.7", "xhigh",
                                   preflight_ok=True, process_exit_confirmed=True)
            self.assertEqual(out["client_evidence_status"], "matched")
        finally:
            for name in ("summary.json", "usage.json"):
                evidence._extended(self.session_dir / name).unlink(missing_ok=True)
            extended_session.rmdir()
            evidence._extended(self.parent).rmdir()
            (self.profile / "sessions").rmdir()
            self.work = original_work


if __name__ == "__main__":
    unittest.main()
