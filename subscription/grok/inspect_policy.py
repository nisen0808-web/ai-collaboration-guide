"""Conservative checks for a Grok inspection report.

The disabled-skill shapes handled here are synthetic and have not been
verified against a real official client's inspect output. Unknown shapes
remain blocked; this module never reads profiles or credentials.
"""
from __future__ import annotations


_OTHER_KEYS = ("hooks", "plugins", "mcpServers", "lspServers", "projectInstructions")
# These optional fields can describe discoverable extension sources. An
# absent key supports older inspect versions; a present key must be an empty
# list. Built-in agents have a separate caller-confirmed restriction below.
_EXTRA_EXTENSION_KEYS = ("marketplaces", "experiments")
# Known top-level report metadata. Its contents are not evidence that any
# extension is disabled; callers must retain the unverified flag below.
_METADATA_DICT_KEYS = frozenset({"configSources", "externalCompat",
                                 "loginPolicy", "permissions"})
_METADATA_SCALAR_TYPES = {"channel": (str,), "cwd": (str,), "grokVersion": (str,),
                          "projectRoot": (str, type(None)), "projectTrusted": (bool,)}


def _explicitly_disabled(skill: object) -> bool:
    """Accept only explicit, non-conflicting disabled signals.

    A name, a bundled source, or a missing flag is not a disabled signal.
    Booleans are checked by identity so numeric 0/1 and strings cannot pass.
    """
    if not isinstance(skill, dict):
        return False
    markers = []
    if "enabled" in skill:
        if skill["enabled"] is not False:
            return False
        markers.append(True)
    if "disabled" in skill:
        if skill["disabled"] is not True:
            return False
        markers.append(True)
    if "active" in skill:
        if skill["active"] is not False:
            return False
        # Inactive does not establish that a skill is disabled; it may still
        # be eligible for automatic activation later in the same session.
    for key in ("status", "state"):
        if key in skill:
            value = skill[key]
            if not isinstance(value, str) or value.lower() != "disabled":
                return False
            markers.append(True)
    return bool(markers)


def inspect_report_diagnostics(report: object, *, subagents_disabled: bool = False) -> dict:
    """Check understood extension fields and expose unverified metadata.

    The caller may pass True only after enforcing its own subagent-disabling
    command and environment. Even then, discovery is not proof of use, and
    this check cannot prove the model never read or used any agent.
    Do not forward report contents, which may contain personal paths.
    """
    def verdict(allowed: bool, reason: str | None, unverified: bool = False) -> dict:
        return {"allowed": allowed, "reason": reason, "unverified_metadata": unverified}

    if not isinstance(report, dict):
        return verdict(False, "inspection_format_unknown")
    if any(not isinstance(report.get(key), list) or report[key] for key in _OTHER_KEYS):
        return verdict(False, "extensions_present_or_unknown")
    skills = report.get("skills")
    if not isinstance(skills, list) or not all(_explicitly_disabled(skill) for skill in skills):
        return verdict(False, "skills_present_or_unknown")
    for key in _EXTRA_EXTENSION_KEYS:
        if key in report and (not isinstance(report[key], list) or report[key]):
            return verdict(False, "extensions_present_or_unknown")

    agents = report.get("agents", [])
    if not isinstance(agents, list):
        return verdict(False, "agents_present_or_unknown")
    if agents:
        if subagents_disabled is not True:
            return verdict(False, "subagents_disable_unverified", True)
        for agent in agents:
            source = agent.get("source") if isinstance(agent, dict) else None
            if not isinstance(source, dict) or source.get("type") != "builtin":
                return verdict(False, "agents_present_or_unknown", True)
            # Explicit conflicts or unknown state must not be reinterpreted as
            # disabled. Merely being listed without these fields is permitted
            # only with the caller's separate enforcement assertion.
            if ("enabled" in agent and agent["enabled"] is not False or
                "disabled" in agent and agent["disabled"] is not True or
                "active" in agent and agent["active"] is not False or
                any(key in agent and (not isinstance(agent[key], str) or
                                      agent[key].lower() != "disabled")
                    for key in ("status", "state"))):
                return verdict(False, "agent_state_conflict_or_unknown", True)

    known = set(_OTHER_KEYS) | set(_EXTRA_EXTENSION_KEYS) | {"skills", "agents"}
    # An agent being discovered does not establish that it ran (or did not).
    unverified = bool(agents)
    for key, value in report.items():
        if key in known:
            continue
        if key in _METADATA_DICT_KEYS:
            if not isinstance(value, dict):
                return verdict(False, "metadata_format_unknown", True)
            # Recognized as metadata, not interpreted as disabled or safe.
            unverified = unverified or bool(value)
            continue
        if key in _METADATA_SCALAR_TYPES:
            if type(value) not in _METADATA_SCALAR_TYPES[key]:
                return verdict(False, "metadata_format_unknown", True)
            continue
        if isinstance(value, (list, dict)) and value:
            return verdict(False, "unknown_nonempty_container", True)
        # Even empty unknown fields or unknown scalars do not prove safety.
        unverified = True
    return verdict(True, None, unverified)


def inspect_report_is_isolated(report: object, *, subagents_disabled: bool = False) -> bool:
    """Compatibility wrapper: only the limited extension policy is checked."""
    return inspect_report_diagnostics(report, subagents_disabled=subagents_disabled)["allowed"]
