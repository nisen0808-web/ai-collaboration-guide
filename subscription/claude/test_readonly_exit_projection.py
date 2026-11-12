"""Synthetic tests for read-only projection of persisted local exit evidence."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from readonly_ledger import ReadOnlyClaudeLedger


class ReadOnlyExitProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.db = self.state / "jobs.sqlite3"
        self.output = self.state / "synthetic.result.json"
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT, error TEXT, heartbeat REAL, output_file TEXT)")
            con.execute("INSERT INTO jobs VALUES (?,?,?,?,?)",
                        ("synthetic", "succeeded", None, None, self.output.name))
        self.adapter = ReadOnlyClaudeLedger(self.state)

    def _write(self, **fields):
        self.output.write_text(json.dumps({"output": "synthetic answer", **fields}), encoding="utf-8")

    def _set_row(self, *, status="succeeded", error=None, output_file=None):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE jobs SET status=?, error=?, output_file=? WHERE id=?",
                        (status, error, self.output.name if output_file is None else output_file, "synthetic"))

    def test_exact_persisted_boolean_is_projected_without_writes(self):
        for value in (True, False):
            with self.subTest(value=value):
                self._write(local_exit_confirmed=value)
                before_db, before_output = self.db.read_bytes(), self.output.read_bytes()
                result = self.adapter.poll("synthetic")
                self.assertEqual(result["status"], "succeeded")
                self.assertIs(result["local_exit_confirmed"], value)
                self.assertEqual(self.db.read_bytes(), before_db)
                self.assertEqual(self.output.read_bytes(), before_output)

    def test_old_or_nonboolean_evidence_is_not_inferred(self):
        for present, value in ((False, None), (True, None), (True, 1), (True, 0),
                               (True, "true"), (True, []), (True, {})):
            with self.subTest(present=present, value=value):
                self._write(**({"local_exit_confirmed": value} if present else {}))
                result = self.adapter.poll("synthetic")
                self.assertEqual(result["status"], "succeeded")
                self.assertNotIn("local_exit_confirmed", result)

    def test_conflicting_database_or_file_evidence_never_promotes_exit(self):
        for field, error in (("row", "process_exit_unconfirmed"), ("row", "operator_released"),
                             ("file", "process_still_running"), ("file", "worker_identity_unconfirmed")):
            with self.subTest(field=field, error=error):
                self._set_row(error=error if field == "row" else None)
                self._write(local_exit_confirmed=True, **({"error": error} if field == "file" else {}))
                result = self.adapter.poll("synthetic")
                self.assertEqual(result["status"], "succeeded")
                self.assertIs(result["local_exit_confirmed"], False)

    def test_operator_declaration_is_never_treated_as_confirmed_exit(self):
        self._write(local_exit_confirmed=True, release_basis="operator_confirmed")
        self.assertIs(self.adapter.poll("synthetic")["local_exit_confirmed"], False)
        self._set_row(status="operator_released")
        result = self.adapter.poll("synthetic")
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertIs(result["local_exit_confirmed"], False)
        self.assertEqual(result["release_basis"], "operator_confirmed")

    def test_missing_or_misdirected_result_file_does_not_claim_exit(self):
        self._set_row(output_file="other.result.json")
        self._write(local_exit_confirmed=True)
        result = self.adapter.poll("synthetic")
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertNotIn("local_exit_confirmed", result)
        self._set_row()
        self.output.unlink()
        result = self.adapter.poll("synthetic")
        self.assertEqual(result["status"], "submission_uncertain")
        self.assertNotIn("local_exit_confirmed", result)


if __name__ == "__main__":
    unittest.main()
