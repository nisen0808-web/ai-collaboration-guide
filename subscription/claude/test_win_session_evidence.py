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
        # Windows runners can expose TEMP using a DOS 8.3 alias. Use the
        # canonical paths required by the session-evidence preflight.
        base = Path(self.temp.name).resolve()
        self.profile = base / "profile"
        self.work = base / "work"
        self.profile.mkdir()
        self.work.mkdir()
        identity = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, text=True, shell=False)
        if identity.returncode:
            self.fail("cannot determine synthetic test account")
        rows = list(csv.reader(io.StringIO(identity.stdout)))
        if not rows or len(rows[0]) != 2 or not rows[0][1].startswith("S-1-"):
            self.fail("cannot determine synthetic test account")
        self._owner_sid = rows[0][1]
        for path in (self.profile, self.work):
            self._set_fixture_owner(path)
            result = subprocess.run(["icacls.exe", str(path), "/inheritance:r", "/grant:r",
                                     "*" + self._owner_sid + ":(OI)(CI)F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
            if result.returncode:
                self.fail("cannot make synthetic directory private")
        self.project = self.profile / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(self.work.resolve()))
        self.project.mkdir(parents=True)
        self._set_fixture_owner(self.profile / "projects")
        self._set_fixture_owner(self.project)
        self.session = str(uuid.uuid4())
        self.target = self.project / (self.session + ".jsonl")
        self.other = self.project / (str(uuid.uuid4()) + ".jsonl")

    def _set_fixture_owner(self, path):
        """Set ownership only on this test's synthetic files or directories."""
        if not path.is_relative_to(Path(self.temp.name).resolve()):
            self.fail("synthetic ownership target outside test directory")
        result = subprocess.run(["icacls.exe", str(path), "/setowner", "*" + self._owner_sid],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
        if result.returncode:
            self.fail("cannot set synthetic fixture owner")

    def _fixture_diagnostics(self):
        """Only fixed booleans/categories; never emit paths, ACLs or SIDs."""
        import ctypes
        from ctypes import wintypes
        import win_session_handle as handles
        import win_private_acl as acl
        checks = {}

        def owner_is_current_user(handle):
            token = wintypes.HANDLE()
            descriptor = ctypes.c_void_p()
            try:
                if not acl._adv.OpenProcessToken(acl._kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
                    return None
                needed = wintypes.DWORD()
                acl._adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
                if not 0 < needed.value <= 1_000_000:
                    return None
                token_data = ctypes.create_string_buffer(needed.value)
                if not acl._adv.GetTokenInformation(token, 1, token_data, needed.value, ctypes.byref(needed)):
                    return None
                user_sid = ctypes.cast(token_data, ctypes.POINTER(ctypes.c_void_p)).contents.value
                owner = ctypes.c_void_p()
                if acl._adv.GetSecurityInfo(handle, 1, 1, ctypes.byref(owner), None, None, None,
                                            ctypes.byref(descriptor)) != 0:
                    return None
                if not owner.value or not user_sid:
                    return None
                return bool(acl._adv.EqualSid(owner, ctypes.c_void_p(user_sid)))
            except Exception:
                return None
            finally:
                if descriptor.value:
                    acl._kernel.LocalFree(descriptor)
                if token.value:
                    acl._kernel.CloseHandle(token)
        try:
            checks["canonical_work"] = os.path.normcase(str(self.work.resolve(strict=True))) == os.path.normcase(str(self.work))
        except OSError:
            checks["canonical_work"] = False
        paths = (("profile", self.profile, True), ("work", self.work, True),
                 ("projects", self.profile / "projects", False), ("project", self.project, False))
        for label, path, protected in paths:
            opened = []
            checks[label + "_chain"] = False
            checks[label + "_acl"] = False
            checks[label + "_owner_current_user"] = None
            try:
                handle = evidence._hold_chain(path, handles, opened)
                checks[label + "_chain"] = True
                checks[label + "_owner_current_user"] = owner_is_current_user(handle)
                checks[label + "_acl"] = acl.protected_private_acl(handle,
                    require_protected=protected, allow_inherited=not protected)
            except Exception:
                pass
            finally:
                checks[label + "_closed"] = evidence._close(handles, opened)
        return checks

    def test_preflight_bound_session_cleanup_does_not_touch_other_file(self):
        if not evidence.prepare_session_evidence(str(self.profile), str(self.work), self.session, enabled=True):
            self.fail(json.dumps(self._fixture_diagnostics(), sort_keys=True))
        self.target.write_text(json.dumps({"type": "assistant", "sessionId": self.session,
            "message": {"model": "claude-synthetic", "content": "synthetic-secret"},
            "effort": "medium"}) + "\n", encoding="utf-8")
        self.other.write_text("unrelated synthetic session", encoding="utf-8")
        self._set_fixture_owner(self.target)
        self._set_fixture_owner(self.other)
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
        self._set_fixture_owner(self.target)
        out = evidence.collect_session_evidence(str(self.profile), str(self.work), self.session,
            "claude-synthetic", "medium", process_exit_confirmed=True, enabled=True)
        self.assertEqual(out["client_evidence_status"], "unavailable")
        self.assertEqual(out["session_cleanup"], "retained")
        self.assertTrue(self.target.exists())

    def test_broad_profile_acl_rejects_without_reading_or_deleting(self):
        self.target.write_text("synthetic", encoding="utf-8")
        self._set_fixture_owner(self.target)
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
        state = state.resolve()
        self._set_fixture_owner(state)
        acl = subprocess.run(["icacls.exe", str(state), "/inheritance:r", "/grant:r", "*" + self._owner_sid + ":(OI)(CI)F"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
        if acl.returncode:
            self.fail("cannot make synthetic state directory private")
        # The executable handles only a fixed fake auth status and one fixed
        # synthetic prompt. It has no network or credential-reading code.
        source = Path(self.temp.name) / "synthetic_cli.cs"
        executable = Path(self.temp.name) / "synthetic_cli.exe"
        source.write_text(r'''
using System;
using System.IO;
using System.Security.Principal;
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
        string sessionFile = Path.Combine(project, id + ".jsonl");
        File.WriteAllText(sessionFile, row + "\n", new UTF8Encoding(false));
        // Administrator-token runners may default the owner to Administrators.
        // Give only this synthetic session file back to the current test user.
        var info = new FileInfo(sessionFile);
        var permissions = info.GetAccessControl();
        permissions.SetOwner(WindowsIdentity.GetCurrent().User);
        info.SetAccessControl(permissions);
        Console.Write("{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"synthetic answer\",\"session_id\":\"" + id + "\"}");
        return 0;
    }
}
''', encoding="utf-8")
        compiled = subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(executable), str(source)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
        self.assertEqual(compiled.returncode, 0, "synthetic CLI compilation failed")
        self.other.write_text("unrelated synthetic session", encoding="utf-8")
        self._set_fixture_owner(self.other)
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
