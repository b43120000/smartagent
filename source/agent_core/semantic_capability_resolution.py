"""SCR V1: model-selected read capabilities, not model-issued authorization.

The Planner translates user semantics into this finite Runtime-owned registry.
Scope, ACL, write ceilings and execution evidence are checked separately.
"""
from __future__ import annotations
from collections.abc import Mapping
from types import MappingProxyType

SCHEMA = "SMARTAGENT_SEMANTIC_CAPABILITY_RESOLUTION_V1"
CAPABILITY_TO_TOOL = MappingProxyType({
    "directory.list": "list_directory",
    "directory.inspect_metadata": "inspect_directory",
    "directory.find": "find_file",
    "project.inspect_scope": "inspect_project_scope",
    "project.inspect_working_set": "inspect_project_working_set",
})
TOOL_TO_CAPABILITY = MappingProxyType({
    tool: cap for cap, tool in CAPABILITY_TO_TOOL.items()
})


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


def verify_resolution_for_actions(proposal, previous, actions):
    """Validate semantic plan before admission; do not alter existing guards."""
    old = normalize_resolution(previous) if previous is not None else None
    new = normalize_resolution(proposal) if proposal is not None else old
    if new is None:
        return None  # Compatibility: existing canonical V9 tool admission.
    selected = set(new["capabilities"])
    if old is not None and not set(old["capabilities"]).issubset(selected):
        raise SemanticCapabilityError("semantic_capability_removal_forbidden")
    for action in actions:
        tool = str(action.get("tool", "") or "")
        cap = TOOL_TO_CAPABILITY.get(tool)
        if cap is not None and cap not in selected:
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
        "SCR V1: For read-only directory/project inspection requests, "
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


# Shared fixed-vocabulary action translator. This extends the existing SCR
# module; it does not create a competing Goal Intent Resolver. The legacy
# report_progress.capability_resolution registry remains read-only and stable.
ACTION_SEMANTIC_SCHEMA = "SMARTAGENT_ACTION_SEMANTIC_TRANSLATION_V1"
_ACTION_ALIASES = MappingProxyType({
    "create_directory": {
        "capability": "directory.create",
        "verbs": ("創立", "創建", "建立", "新增", "開設", "mkdir", "make directory",
                  "create directory", "create folder", "make folder"),
        "objects": ("資料夾", "目錄", "directory", "folder"),
        "intent": "CREATE_DIRECTORY",
        "requires": ("literal_goal_target", "directory.exists=false",
                     "runtime_write_scope_admission"),
        "side_effect": "CREATE_DIRECTORY_ONLY",
    },
    "inspect_directory": {
        "capability": "directory.inspect_metadata",
        "verbs": ("檢查", "確認", "查看", "探查", "inspect"),
        "objects": ("資料夾", "目錄", "directory", "folder"),
        "intent": "INSPECT_DIRECTORY",
        "requires": ("literal_goal_target", "runtime_read_scope_admission"),
        "side_effect": "READ_ONLY",
    },
    "list_directory": {
        "capability": "directory.list",
        "verbs": ("列出", "列舉", "列印", "list", "ls"),
        "objects": ("資料夾", "目錄", "directory", "folder", "內容", "files"),
        "intent": "LIST_DIRECTORY",
        "requires": ("literal_goal_target", "runtime_read_scope_admission"),
        "side_effect": "READ_ONLY",
    },
    "find_file": {
        "capability": "directory.find",
        "verbs": ("尋找", "搜尋", "找出", "find"),
        "objects": ("檔案", "文件", "file", "folder"),
        "intent": "FIND_FILE",
        "requires": ("runtime_read_scope_admission",),
        "side_effect": "READ_ONLY",
    },
    "read_file": {
        "capability": "file.read",
        "verbs": ("讀取", "開啟", "閱讀", "read"),
        "objects": ("檔案", "文件", "file"),
        "intent": "READ_FILE",
        "requires": ("runtime_read_scope_admission",),
        "side_effect": "READ_ONLY",
    },
    "write_file": {
        "capability": "file.write",
        "verbs": ("寫入", "儲存", "存入", "write", "save"),
        "objects": ("檔案", "文件", "file"),
        "intent": "WRITE_FILE",
        "requires": ("runtime_write_scope_admission",),
        "side_effect": "WRITE_FILE",
    },
    "delete_path": {
        "capability": "path.delete",
        "verbs": ("刪除", "移除", "delete", "remove"),
        "objects": ("資料夾", "目錄", "檔案", "folder", "directory", "file"),
        "intent": "DELETE_PATH",
        "requires": ("runtime_write_scope_admission",),
        "side_effect": "DELETE_PATH",
    },
})

def action_semantic_registry() -> dict:
    """Describe every canonical Tool, with aliases only where declared.

    The canonical Tool Envelope is the *single* inventory: adding a new Tool
    cannot silently create a second vocabulary. Tools without specialized
    synonyms remain selectable by canonical name/Planner meaning, and are
    never inferred from arbitrary natural-language keywords.
    """
    from .smartagent_protocol import TOOL_ENVELOPE_SCHEMAS
    return {
        tool: {
            "tool": tool,
            "capability": str(_ACTION_ALIASES.get(tool, {}).get(
                "capability") or ("tool." + tool)),
            "fixed_aliases": list(_ACTION_ALIASES.get(tool, {}).get("verbs") or ()),
            "object_hints": list(_ACTION_ALIASES.get(tool, {}).get("objects") or ()),
            "required_fields": sorted(
                name for name in schema.get("required", {})
                if name != "action_id"
            ),
            "prerequisite_kinds": list(
                _ACTION_ALIASES.get(tool, {}).get("requires") or ()
            ),
            "side_effect": str(_ACTION_ALIASES.get(tool, {}).get(
                "side_effect") or "CANONICAL_TOOL_POLICY"),
            "aliases_are_non_authorizing": True,
            "tool_admission_required": True,
        }
        for tool, schema in sorted(TOOL_ENVELOPE_SCHEMAS.items())
    }


def translate_goal_action(goal: object) -> dict:
    """Bounded semantic *hint* for original Goal text, never write permission.

    A recognized action requires both a registered verb and its object class.
    Negation of the verb is not a positive mutation request. Ambiguous or
    unrelated prose stays unmapped for the Planner, not guessed by Runtime.
    """
    import re
    text = str(goal or "")[:32768]
    matches = []
    for tool, entry in _ACTION_ALIASES.items():
        objects = entry["objects"]
        for verb in entry["verbs"]:
            pattern = re.compile(re.escape(verb), re.I)
            for found in pattern.finditer(text):
                start, end = found.span()
                prefix = text[max(0, start - 10):start]
                if re.search(
                    r"(?:不|別|勿|禁止|不得|不要|無須|無需|not|never|don't|do not)\s*$",
                    prefix, re.I,
                ):
                    continue
                # Constrain to the same bounded instruction clause. Paths and
                # subsequent prose cannot create an imaginary target class.
                clause_start = max(
                    text.rfind(mark, 0, start) for mark in ("\n", "。", "；", ";")
                ) + 1
                ends = [text.find(mark, end) for mark in ("\n", "。", "；", ";")]
                clause_end = min((v for v in ends if v >= 0), default=len(text))
                chunk = text[clause_start:clause_end]
                if not any(re.search(re.escape(obj), chunk, re.I) for obj in objects):
                    continue
                matches.append((start, tool, entry, verb))
                break
    # Only one distinct canonical Tool may be semantically bound. In mixed
    # tasks leave ordering to the Planner, not a regex priority guess.
    unique = {tool for _, tool, _, _ in matches}
    status = ("MATCHED" if len(unique) == 1 else
              "AMBIGUOUS" if len(unique) > 1 else "UNMAPPED")
    selected = min(matches, key=lambda row: row[0]) if status == "MATCHED" else None
    return {
        "schema": ACTION_SEMANTIC_SCHEMA,
        "status": status,
        "canonical_tool": selected[1] if selected else "",
        "capability": selected[2]["capability"] if selected else "",
        "semantic_intent": selected[2]["intent"] if selected else "",
        "matched_registered_verb": selected[3] if selected else "",
        "matching_tools": sorted(unique)[:12],
        "original_text_sha256": __import__("hashlib").sha256(
            text.encode("utf-8")
        ).hexdigest(),
        "semantic_translation_is_not_authorization": True,
        "target_path_binding_required": True,
        "tool_admission_required": True,
    }


def validate_action_semantic_registry() -> None:
    """Fail CI on registry drift, ambiguous canonical ownership or alias gaps."""
    from .smartagent_protocol import TOOL_ENVELOPE_SCHEMAS
    catalog = action_semantic_registry()
    if set(catalog) != set(TOOL_ENVELOPE_SCHEMAS):
        raise SemanticCapabilityError("action_semantic_tool_registry_drift")
    for tool, spec in catalog.items():
        if spec["tool"] != tool or not spec["capability"]:
            raise SemanticCapabilityError("action_semantic_canonical_mapping_invalid:" + tool)
        if not spec["required_fields"] and TOOL_ENVELOPE_SCHEMAS[tool].get("required"):
            required = set(TOOL_ENVELOPE_SCHEMAS[tool]["required"])
            if required != {"action_id"}:
                raise SemanticCapabilityError("action_semantic_required_fields_missing:" + tool)
    if "創立" not in catalog["create_directory"]["fixed_aliases"]:
        raise SemanticCapabilityError("action_semantic_directory_create_alias_missing")
