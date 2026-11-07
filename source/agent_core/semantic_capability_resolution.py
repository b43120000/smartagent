"""SCR V1: model-selected read capabilities, not model-issued authorization.

The Planner translates user semantics into this finite Runtime-owned registry.
Scope, ACL, write ceilings and execution evidence are checked separately.
"""
from __future__ import annotations
from collections.abc import Mapping
from types import MappingProxyType
import hashlib
import re

SCHEMA = "SMARTAGENT_SEMANTIC_CAPABILITY_RESOLUTION_V1"
CAPABILITY_TO_TOOL = MappingProxyType({
    "directory.list": "list_directory",
    "directory.inspect_metadata": "inspect_directory",
    "directory.find": "find_file",
    "directory.create": "create_directory",
    "project.inspect_scope": "inspect_project_scope",
    "project.inspect_working_set": "inspect_project_working_set",
})
TOOL_TO_CAPABILITY = MappingProxyType({
    tool: cap for cap, tool in CAPABILITY_TO_TOOL.items()
})


# Extension of the existing finite Semantic Capability Resolution (SCR).
# Fixed language aliases live ONLY here; Goal/Rule/ActionLoop must consume
# translated capabilities instead of maintaining independent verb tables.
TRANSLATION_SCHEMA = "SMARTAGENT_ACTION_SEMANTIC_TRANSLATION_V1"
ACTION_VOCABULARY = MappingProxyType({
    "directory.create": (
        r"創立|建立|創建|新增|新建|建一個|mkdir\b|\bcreate\b|\bmake\b",
        r"資料夾|目錄|\bfolder\b|\bdirectory\b",
    ),
    "directory.inspect_metadata": (
        r"檢查|確認|查看|觀測|\binspect\b|\bcheck\b",
        r"資料夾|目錄|\bfolder\b|\bdirectory\b",
    ),
    "directory.list": (
        r"列出|列舉|瀏覽|\blist\b",
        r"資料夾|目錄|檔案|\bfolder\b|\bdirectory\b",
    ),
    "directory.find": (
        r"搜尋|尋找|找出|\bfind\b|\bsearch\b",
        r"檔案|資料夾|\bfile\b|\bfolder\b",
    ),
    "project.inspect_scope": (
        r"檢查|確認|查看|\binspect\b|\bcheck\b",
        r"專案範圍|專案邊界|\bproject scope\b",
    ),
    "project.inspect_working_set": (
        r"檢查|確認|查看|\binspect\b|\bcheck\b",
        r"工作集合|\bworking set\b",
    ),
})


def validate_action_vocabulary() -> None:
    """CI invariant: every fixed-language entry uses canonical SCR/Tool schema."""
    from .smartagent_protocol import TOOL_ENVELOPE_SCHEMAS
    if set(ACTION_VOCABULARY) != set(CAPABILITY_TO_TOOL):
        raise SemanticCapabilityError("fixed_action_vocabulary_registry_drift")
    for capability, tool in CAPABILITY_TO_TOOL.items():
        if tool not in TOOL_ENVELOPE_SCHEMAS:
            raise SemanticCapabilityError("fixed_action_tool_schema_missing:" + tool)
        phrases = ACTION_VOCABULARY[capability]
        if len(phrases) != 2 or not all(isinstance(p, str) and p for p in phrases):
            raise SemanticCapabilityError("fixed_action_vocabulary_invalid:" + capability)


def translate_request_action(request: object) -> dict:
    """Translate bounded original user semantics into SCR capabilities.

    A translation is a candidate, never a Planner selection, write grant,
    scope expansion, tool invocation or filesystem observation. For a request
    containing inspect+create, explicit create is the Goal action and inspect
    remains a prerequisite. Ambiguous/non-directory text has no match.
    """
    text = str(request or "")[:32768]
    matches = [
        capability for capability, (verb, noun) in ACTION_VOCABULARY.items()
        if re.search(verb, text, re.I) and re.search(noun, text, re.I)
    ]
    # Existing precondition rule performs observation before mutation.
    selected = (
        "directory.create" if "directory.create" in matches else
        matches[0] if len(matches) == 1 else ""
    )
    return {
        "schema": TRANSLATION_SCHEMA,
        "registry": SCHEMA,
        "request_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "status": "MATCHED" if selected else "AMBIGUOUS" if matches else "NO_MATCH",
        "selected_capability": selected,
        "canonical_tool": CAPABILITY_TO_TOOL.get(selected, ""),
        "matched_capabilities": matches[:12],
        "translation_is_authorization": False,
        "translation_is_planner_selection": False,
        "translation_is_execution_evidence": False,
    }



class SemanticCapabilityError(ValueError):
    pass


def normalize_resolution(proposal: object) -> dict:
    """Reject unknown entries, duplicates, oversized and authority-like keys."""
    if not isinstance(proposal, Mapping) or set(proposal) != {"schema", "capabilities"}:
        raise SemanticCapabilityError("semantic_capability_schema_invalid")
    if proposal.get("schema") != SCHEMA:
        raise SemanticCapabilityError("semantic_capability_schema_invalid")
    caps = proposal.get("capabilities")
    if (not isinstance(caps, list) or not 1 <= len(caps) <= 12
            or any(type(x) is not str or x not in CAPABILITY_TO_TOOL for x in caps)
            or len(set(caps)) != len(caps)):
        raise SemanticCapabilityError("semantic_capability_list_invalid")
    return {"schema": SCHEMA, "capabilities": list(caps)}


def verify_resolution_for_actions(
    proposal, previous, actions, *, attested_goal_capabilities=(),
):
    """Validate semantic plan before admission; do not alter existing guards."""
    old = normalize_resolution(previous) if previous is not None else None
    new = normalize_resolution(proposal) if proposal is not None else old
    if new is None:
        return None  # Compatibility: existing canonical V9 tool admission.
    selected = set(new["capabilities"])
    if old is not None and not set(old["capabilities"]).issubset(selected):
        raise SemanticCapabilityError("semantic_capability_removal_forbidden")
    trusted = set(attested_goal_capabilities or ())
    if not trusted.issubset(CAPABILITY_TO_TOOL):
        raise SemanticCapabilityError("goal_translation_capability_unknown")
    for action in actions:
        tool = str(action.get("tool", "") or "")
        cap = TOOL_TO_CAPABILITY.get(tool)
        # A concrete Planner Action also expresses semantic selection.
        # A runtime-attested exact original Goal translation can satisfy this
        # *selection-only* guard; it does NOT bind SCR, grant ACL or write scope.
        if cap is not None and cap not in selected and cap not in trusted:
            raise SemanticCapabilityError("semantic_capability_not_selected:" + tool)
    return new



def describe_resolution_repair(proposal: object, previous: object = None,
                               actions: object = None) -> dict:
    """Only trusted enum/shape diagnostics; never echo untrusted field values.

    Semantic selection is Planner-owned. A repair hint is NOT a selection,
    authorization, or permission to perform the proposed Tool Action.
    """
    expected_keys = {"schema", "capabilities"}
    mapping = isinstance(proposal, Mapping)
    keys = set(proposal) if mapping else set()
    caps = proposal.get("capabilities") if mapping else None
    cap_list = isinstance(caps, list)
    values = [x for x in caps if type(x) is str] if cap_list else []
    valid = [x for x in values if x in CAPABILITY_TO_TOOL]
    prior = []
    if isinstance(previous, Mapping):
        try:
            prior = normalize_resolution(previous)["capabilities"]
        except SemanticCapabilityError:
            prior = []
    required = sorted({
        TOOL_TO_CAPABILITY.get(str(a.get("tool", "") or ""))
        for a in (actions or []) if isinstance(a, Mapping)
    } - {None})
    # Candidate copies only valid capability names already proposed by the
    # Planner, previously bound, or required by its own submitted read tools.
    # Runtime does NOT apply the candidate or authorize any resulting action.
    candidate_caps = sorted(set(valid) | set(prior) | set(required))
    candidate = (
        {"schema": SCHEMA, "capabilities": candidate_caps}
        if 1 <= len(candidate_caps) <= 12 else None
    )
    return {
        "repair_target": "report_progress.capability_resolution",
        "canonical_candidate": candidate,
        "candidate_applied": False,
        "authorization_granted": False,
        "expected_keys": ["schema", "capabilities"],
        "canonical_schema": SCHEMA,
        "allowed_capabilities": sorted(CAPABILITY_TO_TOOL),
        "previously_selected": sorted(prior),
        "required_for_submitted_actions": required,
        "proposal_shape": {
            "present": proposal is not None,
            "object_type_valid": mapping,
            "key_set_exact": mapping and keys == expected_keys,
            "missing_expected_keys": sorted(expected_keys - keys),
            "extra_key_count": len(keys - expected_keys),
            "schema_present": mapping and "schema" in keys,
            "schema_matches": mapping and proposal.get("schema") == SCHEMA,
            "capabilities_is_list": cap_list,
            "capability_count": len(caps) if cap_list else 0,
            "known_capabilities": sorted(set(valid)),
            "unknown_or_invalid_count": (
                len(caps) - len(valid) if cap_list else 0
            ),
            "duplicate_count": (
                len(values) - len(set(values)) if cap_list else 0
            ),
        },
        "repair_rule": (
            "Replace only the proposal with exactly the two canonical keys. "
            "Use the literal canonical_schema as schema and select a non-empty "
            "unique subset of allowed_capabilities; keep previously_selected "
            "and include capabilities required by the chosen read actions. "
            "This proposal never grants scope, ACL or write permission."
        ),
    }


def resolution_decision_shape(proposal: object) -> dict:
    """Stable semantic shape for rejection deduplication.

    Ignores arbitrary invalid strings and wording, but distinguishes a real
    change in finite selected capabilities or canonical schema structure.
    """
    mapping = isinstance(proposal, Mapping)
    if not mapping:
        return {"kind": "OMITTED" if proposal is None else "NON_OBJECT"}
    keys = set(proposal)
    caps = proposal.get("capabilities")
    valid_list = isinstance(caps, list)
    values = [x for x in caps if type(x) is str] if valid_list else []
    known = sorted({x for x in values if x in CAPABILITY_TO_TOOL})
    return {
        "kind": "OBJECT",
        "keys_exact": keys == {"schema", "capabilities"},
        "schema_matches": proposal.get("schema") == SCHEMA,
        "schema_type": type(proposal.get("schema")).__name__,
        "capabilities_type": type(caps).__name__,
        "selected_known": known,
        "invalid_count": (len(caps) - len([x for x in values if x in CAPABILITY_TO_TOOL])
                          if valid_list else 0),
        "duplicate_count": (len(values) - len(set(values)) if valid_list else 0),
        "missing_count": len({"schema", "capabilities"} - keys),
        "extra_count": len(keys - {"schema", "capabilities"}),
    }

def render_planner_contract():
    registry = "; ".join(k + " => " + v for k, v in CAPABILITY_TO_TOOL.items())
    return (
        "SCR V1: For directory/project capability requests, "
        "translate the user's intent into report_progress.capability_resolution "
        "on the first Progress, with schema " + SCHEMA
        + " and capabilities selected from the finite registry. "
        "Use the relevant subset of these finite canonical capabilities: "
        + registry + ". Mixed goals need all required capabilities. "
        "Choose by meaning, never by matching literal user words. "
        "Preserve earlier selections in any subsequent cumulative expansion. "
        "This proposal is NOT authorization: never grant write, broaden paths, "
        "override ACL or claim success/evidence from this field."
    )
