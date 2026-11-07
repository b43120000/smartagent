"""Canonical contract resolution for model-authored Progress extensions.

Only structural, provably equivalent transformations are permitted. A model's
semantic path/capability selection is never invented, changed or authorized.
The ordinary Progress, Action, Scope, ACL and approval gates remain mandatory.
"""
from __future__ import annotations

from collections.abc import Mapping
import json

from .semantic_path_resolution import SCHEMA as PATH_SCHEMA
from .semantic_capability_resolution import (
    SCHEMA as CAP_SCHEMA, CAPABILITY_TO_TOOL,
)

SCHEMA = "SMARTAGENT_CANONICAL_CONTRACT_RESOLUTION_V1"
_PATH_KEYS = frozenset({"schema", "selected_paths"})
_CAP_KEYS = frozenset({"schema", "capabilities"})
_PATH_ALIASES = ("paths", "path")
_CAP_ALIASES = ("selected_capabilities",)


def _keys(value: object) -> list[str]:
    """Safe field names only; no untrusted values/paths in diagnostics."""
    if not isinstance(value, Mapping):
        return []
    return sorted(str(k)[:64] for k in value
                  if isinstance(k, str) and k.isascii()
                  and k.replace("_", "").isalnum())[:24]


def _record(contract: str, decision: str, reason: str,
            *, expected_schema: str, expected_keys: set[str] | frozenset[str],
            original: object, rules: list[str] | None = None,
            changes: list[str] | None = None, count: int = 0) -> dict:
    keys = _keys(original)
    expected = sorted(expected_keys)
    return {
        "schema": SCHEMA,
        "contract": contract,
        "decision": decision,
        "reason_code": reason,
        "canonical_schema": expected_schema,
        "expected_keys": expected,
        "actual_keys": keys,
        "missing_keys": sorted(set(expected) - set(keys)),
        "unexpected_keys": sorted(set(keys) - set(expected)),
        "rewrite_rule_ids": list(rules or [])[:12],
        "changed_fields": list(changes or [])[:12],
        "selected_count": count,
        "rewrite_applied": decision == "REWRITE_SAFE_EQUIVALENT",
        "full_admission_required": True,
        "authorization_granted": False,
        "executed": False,
        "evidence_created": False,
        "next_step": (
            "REQUEST_USER_INPUT" if reason == "semantic_path_resolution_ambiguous"
            else "REPLAN_WITHIN_ORIGINAL_SCOPE" if decision == "REJECT_REPLAN"
            else "PLANNER_CORRECT_CANONICAL_CONTRACT"
        ),
    }


def _path(proposal: object, document: object) -> tuple[object, dict]:
    contract = "report_progress.path_resolution"
    def row(decision, reason, *, rules=None, changes=None, count=0):
        return _record(contract, decision, reason, expected_schema=PATH_SCHEMA,
                       expected_keys=_PATH_KEYS, original=proposal,
                       rules=rules, changes=changes, count=count)

    if not isinstance(proposal, Mapping):
        return proposal, row("NEEDS_INFORMATION", "semantic_path_planner_schema_invalid")
    if not isinstance(document, Mapping):
        return proposal, row("NEEDS_INFORMATION", "semantic_path_planner_preflight_unavailable")
    if document.get("status") == "AMBIGUOUS":
        return proposal, row("NEEDS_INFORMATION", "semantic_path_resolution_ambiguous")
    verified = document.get("paths")
    if not isinstance(verified, list) or not verified or any(type(x) is not str for x in verified):
        return proposal, row("NEEDS_INFORMATION", "semantic_path_planner_preflight_unavailable")
    data = dict(proposal)
    rules: list[str] = []
    changed: list[str] = []

    # Only known, exact, read-only preflight echoes may be omitted. Unknown
    # extra keys might represent an attempted scope/authority change.
    echoes = {"status": document.get("status"),
              "source": "VERIFIED_ORIGINAL_REQUEST_SPANS"}
    for key, expected in echoes.items():
        if key in data:
            if data[key] != expected:
                return proposal, row("NEEDS_INFORMATION", "semantic_path_planner_schema_invalid")
            del data[key]
            rules.append("REMOVE_EXACT_PREFLIGHT_ECHO")
            changed.append(key)

    unknown = set(data) - _PATH_KEYS - set(_PATH_ALIASES)
    if unknown:
        return proposal, row("NEEDS_INFORMATION", "semantic_path_planner_schema_invalid")
    for alias in _PATH_ALIASES:
        if alias not in data:
            continue
        value = data.pop(alias)
        if "selected_paths" in data:
            existing = data["selected_paths"]
            if existing != value and not (isinstance(existing, list) and len(existing) == 1 and existing[0] == value):
                return proposal, row("REJECT_REPLAN", "semantic_path_planner_selection_conflict")
            rules.append("REMOVE_IDENTICAL_PATH_ALIAS")
        else:
            data["selected_paths"] = value
            rules.append("CANONICAL_PATH_KEY_ALIAS")
        changed.append("selected_paths")

    selected = data.get("selected_paths")
    if type(selected) is str and len(verified) == 1:
        data["selected_paths"] = [selected]
        rules.append("SINGLE_VERIFIED_PATH_TO_LIST")
        changed.append("selected_paths")
    selected = data.get("selected_paths")
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 24
            or any(type(x) is not str or not x for x in selected)
            or len(selected) != len(set(selected))):
        return proposal, row("NEEDS_INFORMATION", "semantic_path_planner_selection_invalid")
    if selected != verified:
        # Strictly lexical Windows separator equivalence, not case folding,
        # dot-segment elimination, symlink resolution or a new path choice.
        # The target is copied only from the Runtime-verified original span.
        separators_equivalent = (
            len(selected) == len(verified)
            and all(type(candidate) is str and
                    candidate.replace("/", "\\") == original.replace("/", "\\")
                    for candidate, original in zip(selected, verified))
        )
        if not separators_equivalent:
            return proposal, row("REJECT_REPLAN",
                                 "semantic_path_planner_selection_mismatch", count=len(selected))
        data["selected_paths"] = list(verified)
        rules.append("WINDOWS_SEPARATOR_EQUIVALENCE")
        changed.append("selected_paths")
    if "schema" not in data:
        data["schema"] = PATH_SCHEMA
        rules.append("ADD_FIXED_PATH_SCHEMA")
        changed.append("schema")
    elif data["schema"] != PATH_SCHEMA:
        return proposal, row("NEEDS_INFORMATION", "semantic_path_planner_schema_invalid", count=len(selected))
    if set(data) != _PATH_KEYS:
        return proposal, row("NEEDS_INFORMATION", "semantic_path_planner_schema_invalid", count=len(selected))
    if rules:
        return data, row("REWRITE_SAFE_EQUIVALENT", "CANONICAL_PATH_FORMAT", rules=rules,
                         changes=sorted(set(changed)), count=len(selected))
    return data, row("PASS_UNCHANGED", "CANONICAL_PATH_VALID", count=len(selected))


def _capability(proposal: object) -> tuple[object, dict]:
    contract = "report_progress.capability_resolution"
    def row(decision, reason, *, rules=None, changes=None, count=0):
        return _record(contract, decision, reason, expected_schema=CAP_SCHEMA,
                       expected_keys=_CAP_KEYS, original=proposal,
                       rules=rules, changes=changes, count=count)

    if not isinstance(proposal, Mapping):
        return proposal, row("NEEDS_INFORMATION", "semantic_capability_schema_invalid")
    data = dict(proposal)
    rules: list[str] = []
    changed: list[str] = []
    for alias in _CAP_ALIASES:
        if alias in data:
            value = data.pop(alias)
            if "capabilities" in data:
                if data["capabilities"] != value:
                    return proposal, row("REJECT_REPLAN", "semantic_capability_selection_conflict")
                rules.append("REMOVE_IDENTICAL_CAPABILITY_ALIAS")
            else:
                data["capabilities"] = value
                rules.append("CANONICAL_CAPABILITY_KEY_ALIAS")
            changed.append("capabilities")
    if set(data) - _CAP_KEYS:
        return proposal, row("NEEDS_INFORMATION", "semantic_capability_schema_invalid")
    caps = data.get("capabilities")
    if type(caps) is str and caps in CAPABILITY_TO_TOOL:
        data["capabilities"] = [caps]
        rules.append("SINGLE_CAPABILITY_TO_LIST")
        changed.append("capabilities")
    caps = data.get("capabilities")
    if (not isinstance(caps, list) or not 1 <= len(caps) <= 12
            or any(type(x) is not str or x not in CAPABILITY_TO_TOOL for x in caps)
            or len(caps) != len(set(caps))):
        return proposal, row("NEEDS_INFORMATION", "semantic_capability_list_invalid")
    if "schema" not in data:
        data["schema"] = CAP_SCHEMA
        rules.append("ADD_FIXED_CAPABILITY_SCHEMA")
        changed.append("schema")
    elif data["schema"] != CAP_SCHEMA:
        return proposal, row("NEEDS_INFORMATION", "semantic_capability_schema_invalid", count=len(caps))
    if rules:
        return data, row("REWRITE_SAFE_EQUIVALENT", "CANONICAL_CAPABILITY_FORMAT",
                         rules=rules, changes=sorted(set(changed)), count=len(caps))
    return data, row("PASS_UNCHANGED", "CANONICAL_CAPABILITY_VALID", count=len(caps))


def _feedback(receipt: dict) -> dict:
    contract = receipt["contract"]
    path = contract.endswith("path_resolution")
    selection = "selected_paths" if path else "capabilities"
    kind = receipt["decision"]
    path_hint = (
        "For path_resolution use only the original-request verified path(s), "
        "never change or add paths or authority; omit this optional field if "
        "you do not intend to confirm the selection. "
    ) if path else (
        "For capability_resolution select only finite known capabilities, "
        "preserve cumulative previous selections, and include those required "
        "by your proposed read operations. "
    )
    suggestion = (
        "Return exactly the two keys schema and " + selection + ". "
        "Set schema to " + receipt["canonical_schema"] + ". "
        + path_hint
        + ("This is a semantic/scope conflict: choose a new admitted strategy "
           "or request genuine new user authority; Runtime cannot rewrite it."
           if kind == "REJECT_REPLAN" else
           "Provide the missing or corrected fields; never assume actions executed.")
    )
    if receipt["reason_code"] == "semantic_path_resolution_ambiguous":
        suggestion += (" Original Request paths are ambiguous: ask the user for "
                       "a quoted exact path. Do not execute any path operation.")
    return {
        "marker": "[SMARTAGENT_CANONICAL_CONTRACT_REJECTED]",
        "reason": receipt["reason_code"],
        "repair_scope": "SEMANTIC_CONFLICT" if kind == "REJECT_REPLAN" else "CANONICAL_SCHEMA",
        "canonical_contract": receipt,
        # Existing Planner diagnostic formatter emits detail/suggestion only;
        # serialize the bounded structured receipt so the *immediate* retry
        # receives exact missing/extra keys, not an incomplete generic hint.
        "detail": json.dumps(receipt, ensure_ascii=False, separators=(",", ":")),
        "suggestion": suggestion,
    }


# The core Progress schema uses the same safe-equivalence policy as SCR/SPR.
# These are spelling aliases only. Never infer a condition, Evidence reference,
# Goal, Outcome, Decision, prerequisite or permission from absent fields.
_CORE_ALIASES = {
    "current step": "current_step",
    "total steps": "total_steps",
    "current focus": "current_focus",
    "next action": "next_action",
    "base evaluation": "base_evaluation",
    "currentStep": "current_step",
    "totalSteps": "total_steps",
    "currentFocus": "current_focus",
    "nextAction": "next_action",
    "baseEvaluation": "base_evaluation",
    "progressUpdate": "progress_update",
    "completionContract": "completion_contract",
    "matchedCondition": "matched_condition",
    "matchedConditionId": "matched_condition_id",
    "evidenceRefs": "evidence_refs",
    "decisionReason": "decision_reason",
    "contextRequest": "context_request",
}


def resolve_core_progress_fields(payload: Mapping) -> tuple[dict, dict]:
    """Normalize unambiguous Core Progress aliases without semantic invention.

    Called from the established Progress materialization entrypoint, so
    existing evidence and completion validation remain the authority.
    """
    result = dict(payload)
    source = dict(payload)
    rules: list[str] = []
    changes: list[str] = []
    for alias, canonical in _CORE_ALIASES.items():
        if alias not in result:
            continue
        if canonical in result and result[canonical] != result[alias]:
            raise ValueError("conflicting_progress_alias:" + canonical)
        value = result.pop(alias)
        if canonical not in result:
            result[canonical] = value
        rules.append("CORE_PROGRESS_EXACT_ALIAS")
        changes.append(canonical)
    return result, _record(
        "report_progress", "REWRITE_SAFE_EQUIVALENT" if changes else "PASS_UNCHANGED",
        "CANONICAL_CORE_PROGRESS_FIELDS" if changes else "CORE_PROGRESS_VALID",
        expected_schema="SMARTAGENT_PROGRESS_REPLY_CONTRACT_V1",
        expected_keys=set(source) | set(_CORE_ALIASES.values()),
        original=source, rules=sorted(set(rules)), changes=sorted(set(changes)),
    )


def resolve_planner_contracts(progress: Mapping, *,
                              path_document: Mapping | None) -> tuple[dict, list[dict], list[dict]]:
    """Resolve optional Planner contracts without changing semantic decisions.

    Returns (candidate_progress, sanitized_receipts, diagnostics). Rejected
    candidates are never submitted to downstream Tool Admission or execution.
    """
    candidate = dict(progress)
    receipts: list[dict] = []
    errors: list[dict] = []
    for key, resolver in (
        ("capability_resolution", lambda v: _capability(v)),
        ("path_resolution", lambda v: _path(v, path_document)),
    ):
        # None has the pre-existing meaning of optional/omitted. Do not
        # invent a Planner selection or alter inheritance semantics.
        if candidate.get(key) is None:
            continue
        original = candidate[key]
        canonical, receipt = resolver(original)
        receipts.append(receipt)
        if receipt["decision"] in {"NEEDS_INFORMATION", "REJECT_REPLAN"}:
            errors.append(_feedback(receipt))
        else:
            candidate[key] = canonical
    return candidate, receipts, errors


__all__ = ["SCHEMA", "resolve_planner_contracts", "resolve_core_progress_fields"]
