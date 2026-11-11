"""Synthetic report checks; no real profiles, credentials or CLI calls."""
from __future__ import annotations

import unittest

from inspect_policy import inspect_report_diagnostics, inspect_report_is_isolated


def report(**changes):
    value = {key: [] for key in ("hooks", "plugins", "mcpServers", "skills",
                                 "lspServers", "projectInstructions")}
    value.update(changes)
    return value


class InspectPolicyTests(unittest.TestCase):
    def test_empty_report_is_accepted(self):
        self.assertTrue(inspect_report_is_isolated(report()))

    def test_explicit_nonconflicting_disabled_skills_are_accepted(self):
        skills = [{"name": "one", "enabled": False}, {"disabled": True},
                  {"status": "disabled"}, {"state": "DISABLED"},
                  {"enabled": False, "disabled": True, "status": "disabled"}]
        self.assertTrue(inspect_report_is_isolated(report(skills=skills)))

    def test_unknown_or_bundled_skill_is_not_assumed_disabled(self):
        for skill in ({}, {"name": "example", "source": {"type": "bundled"}},
                      {"enabled": 0}, {"disabled": 1}, {"enabled": "false"},
                      {"status": "unknown"}, {"active": False}, None, "skill"):
            with self.subTest(skill=skill):
                self.assertFalse(inspect_report_is_isolated(report(skills=[skill])))

    def test_conflicting_flags_are_rejected(self):
        for skill in ({"enabled": False, "disabled": False},
                      {"enabled": False, "status": "enabled"},
                      {"disabled": True, "active": True},
                      {"disabled": True, "state": "unknown"}):
            with self.subTest(skill=skill):
                self.assertFalse(inspect_report_is_isolated(report(skills=[skill])))

    def test_non_skill_extensions_remain_blocked(self):
        for key in ("hooks", "plugins", "mcpServers", "lspServers", "projectInstructions"):
            with self.subTest(key=key):
                self.assertFalse(inspect_report_is_isolated(report(**{key: [{"enabled": False}]})))

    def test_known_extra_extension_lists_are_fail_closed(self):
        for key in ("agents", "marketplaces", "experiments"):
            with self.subTest(key=key):
                self.assertTrue(inspect_report_is_isolated(report(**{key: []})))
                self.assertFalse(inspect_report_is_isolated(report(**{key: [{"enabled": False}]})))
                self.assertFalse(inspect_report_is_isolated(report(**{key: {}})))

    def test_default_builtin_agents_require_caller_enforcement(self):
        # Synthetic shape derived from the reported top-level types of a
        # no-login empty-profile inspect. Agent contents are synthetic.
        data = report(agents=[{"source": {"type": "builtin"}},
                              {"source": {"type": "builtin"}},
                              {"source": {"type": "builtin"}}],
                      channel="stable", cwd="synthetic", grokVersion="1.0.52",
                      projectRoot=None, projectTrusted=False,
                      configSources={}, externalCompat={}, loginPolicy={}, permissions={},
                      marketplaces=[], experiments=[])
        self.assertFalse(inspect_report_is_isolated(data))
        self.assertFalse(inspect_report_is_isolated(data, subagents_disabled=1))
        result = inspect_report_diagnostics(data, subagents_disabled=True)
        self.assertTrue(result["allowed"])
        self.assertTrue(result["unverified_metadata"])
        self.assertNotIn("synthetic", repr(result))

    def test_other_or_unknown_agents_remain_blocked_even_with_enforcement(self):
        for agent in ({}, {"source": "builtin"}, {"source": {"type": "bundled"}},
                      {"source": {"type": "external"}}, {"source": {}}, None):
            with self.subTest(agent=agent):
                self.assertFalse(inspect_report_is_isolated(report(agents=[agent]),
                                                            subagents_disabled=True))

    def test_explicit_agent_conflicts_remain_blocked(self):
        for fields in ({"enabled": True}, {"disabled": False}, {"active": True},
                       {"status": "running"}, {"state": "unknown"}, {"enabled": 0}):
            with self.subTest(fields=fields):
                agent = {"source": {"type": "builtin"}, **fields}
                self.assertFalse(inspect_report_is_isolated(report(agents=[agent]),
                                                            subagents_disabled=True))

    def test_empty_agents_and_other_extension_lists_stay_conservative(self):
        self.assertTrue(inspect_report_is_isolated(report(agents=[])))
        self.assertFalse(inspect_report_is_isolated(report(agents={})))
        self.assertFalse(inspect_report_is_isolated(report(agents=[], marketplaces=["example"]),
                                                    subagents_disabled=True))

    def test_known_metadata_dicts_remain_usable_but_unverified(self):
        for key in ("configSources", "externalCompat", "loginPolicy", "permissions"):
            with self.subTest(key=key):
                result = inspect_report_diagnostics(report(**{key: {"opaque": "value"}}))
                self.assertTrue(result["allowed"])
                self.assertIsNone(result["reason"])
                self.assertTrue(result["unverified_metadata"])
                self.assertNotIn("value", repr(result))

    def test_unknown_nonempty_containers_are_blocked_without_echo(self):
        for item in (["opaque"], {"opaque": "sensitive"}):
            with self.subTest(item=item):
                result = inspect_report_diagnostics(report(futureExtension=item))
                self.assertFalse(result["allowed"])
                self.assertEqual(result["reason"], "unknown_nonempty_container")
                self.assertTrue(result["unverified_metadata"])
                self.assertNotIn("opaque", repr(result))

    def test_unknown_empty_and_scalar_fields_are_reported_unverified(self):
        for item in ([], {}, "unknown"):
            with self.subTest(item=item):
                result = inspect_report_diagnostics(report(futureField=item))
                self.assertTrue(result["allowed"])
                self.assertTrue(result["unverified_metadata"])

    def test_known_scalar_metadata_does_not_expose_personal_paths(self):
        result = inspect_report_diagnostics(report(cwd="sensitive-path", projectRoot="other-path",
                                                   grokVersion="test", projectTrusted=False,
                                                   channel="test"))
        self.assertTrue(result["allowed"])
        self.assertNotIn("sensitive-path", repr(result))

    def test_non_dictionary_known_metadata_is_rejected(self):
        result = inspect_report_diagnostics(report(configSources=["unknown"]))
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "metadata_format_unknown")
        self.assertTrue(result["unverified_metadata"])

    def test_malformed_known_scalar_metadata_is_rejected(self):
        for extra in ({"cwd": []}, {"projectTrusted": 1}, {"channel": {}},
                      {"grokVersion": None}, {"projectRoot": []}):
            with self.subTest(extra=extra):
                result = inspect_report_diagnostics(report(**extra))
                self.assertFalse(result["allowed"])
                self.assertEqual(result["reason"], "metadata_format_unknown")

    def test_missing_or_malformed_fields_are_rejected(self):
        self.assertFalse(inspect_report_is_isolated(None))
        self.assertFalse(inspect_report_is_isolated({}))
        self.assertFalse(inspect_report_is_isolated(report(skills={})))
        malformed = report()
        del malformed["hooks"]
        self.assertFalse(inspect_report_is_isolated(malformed))


if __name__ == "__main__":
    unittest.main()
