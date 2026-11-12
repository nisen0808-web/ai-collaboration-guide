"""Windows-only tests in newly created synthetic directories; no client login."""
import csv
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import session_evidence as evidence
import durable_claude as durable


@unittest.skipUnless(os.name == "nt", "Windows ACL and handles required")
class WindowsSessionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session-evidence-test-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.profile = base / "profile"
        self.work = base / "work"
        self.profile.mkdir()
        self.work.mkdir()
        identity = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, text=True, shell=False)
        if identity.returncode:
            self.skipTest("cannot determine test account")
        rows = list(csv.reader(io.StringIO(identity.stdout)))
        if not rows or len(rows[0]) != 2 or not rows[0][1].startswith("S-1-"):
            self.skipTest("cannot determine test account")
        for path in (self.profile, self.work):
            result = subprocess.run(["icacls.exe", str(path), "/inheritance:r", "/grant:r",
                                     "*" + rows[0][1] + ":(OI)(CI)F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
            if result.returncode:
                self.skipTest("cannot make synthetic directory private")
        self.project = self.profile / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(self.work.resolve()))
        self.project.mkdir(parents=True)
        self.session = str(uuid.uuid4())
        self.target = self.project / (self.session + ".jsonl")
        self.other = self.project / (str(uuid.uuid4()) + ".jsonl")

    def test_preflight_bound_session_cleanup_does_not_touch_other_file(self):
        self.assertTrue(evidence.prepare_session_evidence(str(self.profile), str(self.work), self.session, enabled=True))
        self.target.write_text(json.dumps({"type": "assistant", "sessionId": self.session,
            "message": {"model": "claude-synthetic", "content": "synthetic-secret"},
            "effort": "medium"}) + "\n", encoding="utf-8")
        self.other.write_text("unrelated synthetic session", encoding="utf-8")
        self.assertFalse(evidence.prepare_session_evidence(str(self.profile), str(self.work), self.session, enabled=True))
        out = evidence.collect_session_evidence(str(self.profile), str(self.work), self.session,
            "claude-synthetic", "medium", process_exit_confirmed=True, enabled=True)
        self.assertEqual(out["client_evidence_status"], "matched")
        self.assertEqual(out["session_cleanup"], "retained")
        self.assertTrue(self.target.exists())
        out = evidence.collect_session_evidence(str(self.profile), str(self.work), self.session,
            "claude-synthetic", "medium", process_exit_confirmed=True, enabled=True, durable_result_saved=True)
        self.assertEqual(out["client_evidence_status"], "matched")
        self.assertEqual(out["session_cleanup"], "deletion_requested")
        self.assertFalse(self.target.exists())
        self.assertEqual(self.other.read_text(encoding="utf-8"), "unrelated synthetic session")
        self.assertNotIn("synthetic-secret", repr(out))

    def test_preflight_rejects_dangling_session_junction(self):
        # A junction is a Windows reparse point. Use a unique, synthetic target
        # within this test directory and remove only the empty target directory
        # to make the junction dangling; never follow or delete its destination.
        missing_target = self.project / "synthetic-empty-target"
        missing_target.mkdir()
        created = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(self.target), str(missing_target)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
        if created.returncode:
            self.skipTest("creating a synthetic junction is unavailable")
        try:
            missing_target.rmdir()
            self.assertFalse(evidence.prepare_session_evidence(
                str(self.profile), str(self.work), self.session, enabled=True))
        finally:
            # rmdir removes this exact junction itself; it does not traverse it.
            self.target.rmdir()

    def test_damaged_or_cross_session_record_is_retained(self):
        self.target.write_text(json.dumps({"type": "assistant", "sessionId": str(uuid.uuid4()),
            "message": {"model": "claude-synthetic"}, "effort": "medium"}), encoding="utf-8")
        out = evidence.collect_session_evidence(str(self.profile), str(self.work), self.session,
            "claude-synthetic", "medium", process_exit_confirmed=True, enabled=True)
        self.assertEqual(out["client_evidence_status"], "unavailable")
        self.assertEqual(out["session_cleanup"], "retained")
        self.assertTrue(self.target.exists())

    def test_broad_profile_acl_rejects_without_reading_or_deleting(self):
        self.target.write_text("synthetic", encoding="utf-8")
        changed = subprocess.run(["icacls.exe", str(self.profile), "/grant", "*S-1-1-0:(OI)(CI)F"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
        if changed.returncode:
            self.skipTest("cannot add synthetic broad allow")
        out = evidence.collect_session_evidence(str(self.profile), str(self.work), self.session,
            "claude-synthetic", "medium", process_exit_confirmed=True, enabled=True)
        self.assertEqual(out["client_evidence_status"], "unavailable")
        self.assertEqual(out["session_cleanup"], "not_attempted")
        self.assertTrue(self.target.exists())

    def test_real_durable_worker_with_synthetic_cli_persists_then_collects(self):
        """Real detached worker and Windows handles; fake CLI, no provider."""
        compiler = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
        if not compiler.is_file():
            self.skipTest("Windows C# compiler unavailable")
        state = Path(self.temp.name) / "state"
        state.mkdir()
        identity = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, shell=False)
        if identity.returncode:
            self.skipTest("cannot determine test account")
        rows = list(csv.reader(io.StringIO(identity.stdout)))
        if not rows or len(rows[0]) != 2 or not rows[0][1].startswith("S-1-"):
            self.skipTest("cannot determine test account")
        acl = subprocess.run(["icacls.exe", str(state), "/inheritance:r", "/grant:r", "*" + rows[0][1] + ":(OI)(CI)F"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
        if acl.returncode:
            self.skipTest("cannot make synthetic state directory private")
        # The executable handles only a fixed fake auth status and one fixed
        # synthetic prompt. It has no network or credential-reading code.
        source = Path(self.temp.name) / "synthetic_cli.cs"
        executable = Path(self.temp.name) / "synthetic_cli.exe"
        source.write_text(r'''
using System;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
class SyntheticCli {
    static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        if (args.Length == 2 && args[0] == "auth" && args[1] == "status") {
            Console.Write("{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"max\"}");
            return 0;
        }
        int i = Array.IndexOf(args, "--session-id");
        int m = Array.IndexOf(args, "--model");
        int e = Array.IndexOf(args, "--effort");
        if (i < 0 || m < 0 || e < 0 || i + 1 >= args.Length || m + 1 >= args.Length || e + 1 >= args.Length) return 10;
        Guid parsed;
        string id = args[i + 1];
        if (!Guid.TryParse(id, out parsed) || args[m + 1] != "claude-synthetic" || args[e + 1] != "medium") return 11;
        if (Console.In.ReadToEnd() != "synthetic task") return 12;
        string profile = Environment.GetEnvironmentVariable("CLAUDE_CONFIG_DIR");
        string work = Directory.GetCurrentDirectory();
        string project = Path.Combine(profile, "projects", Regex.Replace(work, "[^a-zA-Z0-9]", "-"));
        string row = "{\"type\":\"assistant\",\"sessionId\":\"" + id +
            "\",\"message\":{\"model\":\"claude-synthetic\",\"content\":\"synthetic private\"},\"effort\":\"medium\"}";
        File.WriteAllText(Path.Combine(project, id + ".jsonl"), row + "\n", new UTF8Encoding(false));
        Console.Write("{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"synthetic answer\",\"session_id\":\"" + id + "\"}");
        return 0;
    }
}
''', encoding="utf-8")
        compiled = subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(executable), str(source)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
        self.assertEqual(compiled.returncode, 0, "synthetic CLI compilation failed")
        self.other.write_text("unrelated synthetic session", encoding="utf-8")
        adapter = durable.DurableClaudeAdapter(str(executable), str(self.profile), str(self.work), str(state),
                                               "claude-synthetic", "medium", enabled=True,
                                               session_evidence=True, max_concurrency=1)
        workers = []
        real_popen = subprocess.Popen
        def record_worker(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            workers.append(process)
            return process
        result = None
        try:
            with patch("durable_claude.subprocess.Popen", side_effect=record_worker):
                receipt = adapter.submit("synthetic task", "synthetic-e2e")
            self.assertEqual(receipt["status"], "queued")
            self.assertEqual(len(workers), 1)
            deadline = time.monotonic() + 20
            saw_answer = False
            while time.monotonic() < deadline:
                result = adapter.poll("synthetic-e2e")
                if result["status"] == "succeeded":
                    saw_answer = True
                    self.assertEqual(result["output"], "synthetic answer")
                    output = state / "synthetic-e2e.result.json"
                    self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["output"], "synthetic answer")
                    if result["quality_verification"].get("client_evidence_status") == "matched":
                        break
                if result["status"] == "failed":
                    self.fail("synthetic worker failed before verification")
                time.sleep(.01)
            self.assertTrue(saw_answer, "synthetic result did not finish")
            self.assertEqual(result["quality_verification"]["client_evidence_status"], "matched")
            self.assertTrue(result["quality_verification"]["client_model_matches_requested"])
            self.assertTrue(result["quality_verification"]["client_effort_matches_requested"])
            self.assertEqual(result["quality_verification"]["session_cleanup"], "deletion_requested")
            self.assertFalse(result["quality_verification"]["effort_verified"])
            with durable._connect(adapter.db) as con:
                row = con.execute("SELECT session_id FROM session_evidence_intents WHERE job_id=?", ("synthetic-e2e",)).fetchone()
            self.assertIsNotNone(row)
            self.assertFalse((self.project / (row[0] + ".jsonl")).exists())
            self.assertEqual(self.other.read_text(encoding="utf-8"), "unrelated synthetic session")
            self.assertNotIn(row[0], repr(result))
            with patch("durable_claude.subprocess.Popen", side_effect=AssertionError("must not restart")):
                replay = adapter.submit("synthetic task", "synthetic-e2e")
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["existing_status"], "succeeded")
        finally:
            for worker in workers:
                # The synthetic child must exit by itself; never terminate it.
                worker.wait(timeout=30)


if __name__ == "__main__":
    unittest.main()
