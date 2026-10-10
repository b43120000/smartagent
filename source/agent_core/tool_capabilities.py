"""Interface capability policy derived from the canonical tool schemas.

Tool existence and field schemas remain owned by smartagent_protocol; tool
execution remains owned by tools.execute_tool.  This module is policy only.
"""
from __future__ import annotations

from types import MappingProxyType

from .smartagent_protocol import (
    PROJECT_EVIDENCE_ACTION_CONTRACT,
    TOOL_ENVELOPE_SCHEMAS,
)


DETERMINISTIC_LOCAL = "DETERMINISTIC_LOCAL"
FILE_TRANSFER = "FILE_TRANSFER"
PROJECT_SYNC = "PROJECT_SYNC"
VERIFICATION = "VERIFICATION"
AGENT_ORCHESTRATION = "AGENT_ORCHESTRATION"
REMOTE_CONTROL = "REMOTE_CONTROL"
SELF_REPAIR = "SELF_REPAIR"
SEMANTIC_DELEGATION = "SEMANTIC_DELEGATION"
WEB_LOOKUP = "WEB_LOOKUP"
PROTOCOL_CONTROL = "PROTOCOL_CONTROL"


_CATEGORY_MEMBERS = {
    DETERMINISTIC_LOCAL: {
        "run_command", "read_file", "write_file", "delete_path", "begin_file_write",
        "write_file_chunk", "commit_file_write", "abort_file_write",
        "list_directory", "inspect_directory", "find_file",
        "inspect_project_scope", "inspect_project_working_set", "compare_project_snapshot",
        "inspect_semantic_map", "update_semantic_map", "update_semantic_map_file",
        "extract_project_dependencies", "build_project_bundle",
        "build_project_delta", "query_project", "inspect_project_ledger",
        "query_project_history", "validate_edit_plan", "apply_edit_plan",
        "propose_task_plan", "repair_task_plan", "propose_task_plan_file", "execute_frozen_plan",
        "save_session_summary",
    },
    FILE_TRANSFER: {
        "upload_file", "upload_files", "download_artifact",
        "execute_artifact_bundle", "return_artifact", "google_drive_upload",
    },
    PROJECT_SYNC: {"project_sync"},
    VERIFICATION: {"aggregate_verification"},
    AGENT_ORCHESTRATION: {"ask_executor"},
    REMOTE_CONTROL: set(),
    SELF_REPAIR: set(),
    SEMANTIC_DELEGATION: {"web_edit_file"},
    WEB_LOOKUP: {"web_search"},
    PROTOCOL_CONTROL: {"report_progress", "final_response", "turn_commit"},
}

TOOL_CATEGORIES = MappingProxyType({
    tool: category
    for category, tools in _CATEGORY_MEMBERS.items()
    for tool in tools
})

CAPABILITY_GAPS = MappingProxyType({
    "remote_control": "No canonical remote-control envelope exists.",
    "self_repair": "No canonical self-repair orchestration envelope exists.",
})

_TOOL_PURPOSES = MappingProxyType({
    "list_directory": "List entries at one exact authorized directory without widening scope.",
    "find_file": "Find matching files below one authorized root.",
    "read_file": "Read one targeted authorized file and return bounded content or a result reference.",
    "inspect_directory": "Inspect bounded directory metadata when a plain listing is insufficient.",
    "inspect_project_scope": "Inspect a declared project scope; not a substitute for list_directory.",
    "inspect_project_working_set": "Bind only the task plan's exact paths to the current Git/filesystem snapshot without uploading source.",
    "query_project": "Query a Runtime-held project index or snapshot without uploading the project.",
    "propose_task_plan": "Freeze one complete TASK_PLAN_V1 plan after canonical validation.",
    "repair_task_plan": "Perform one bounded canonical repair of a rejected TASK_PLAN_V1 plan; use only when Runtime requires it.",
    "inspect_project_ledger": "Inspect compact project sync and semantic-history provenance without uploading files.",
    "query_project_history": "Query bounded Project Ledger events by type, snapshot, path, or time.",
    "project_sync": "Create or refresh Runtime project context using the smallest selected strategy.",
    "run_command": "Run an explicitly selected command inside the authorized workspace.",
})

_WEB_DIRECT_DENY = frozenset({
    "ask_executor",
    "execute_artifact_bundle",
    "save_session_summary",
    "web_edit_file",
    "final_response",
    "turn_commit",
})


def get_allowed_tools(interface: str) -> frozenset[str]:
    name = str(interface or "").strip().lower().replace("-", "_")
    canonical = frozenset(TOOL_ENVELOPE_SCHEMAS)
    if name in {"web_direct", "webdirect", "remote"}:
        allowed_categories = {
            DETERMINISTIC_LOCAL,
            FILE_TRANSFER,
            PROJECT_SYNC,
            VERIFICATION,
            WEB_LOOKUP,
        }
        return frozenset(
            tool for tool in canonical
            if TOOL_CATEGORIES.get(tool) in allowed_categories
            and tool not in _WEB_DIRECT_DENY
        )
    if name in {"local", "local_agent", "localagent"}:
        return frozenset(
            tool for tool in canonical
            if TOOL_CATEGORIES.get(tool) != PROTOCOL_CONTROL
        )
    raise ValueError(f"unknown_tool_capability_interface:{interface}")


def tool_category(tool: str) -> str:
    if tool not in TOOL_ENVELOPE_SCHEMAS:
        raise KeyError(f"unknown_canonical_tool:{tool}")
    category = TOOL_CATEGORIES.get(tool)
    if not category:
        raise KeyError(f"uncategorized_canonical_tool:{tool}")
    return category


def _type_name(value) -> str:
    values = value if isinstance(value, tuple) else (value,)
    return " | ".join(item.__name__ for item in values)


def render_protocol_tool_section(interface: str) -> str:
    lines = ["Supported shared action tools (canonical schemas):"]
    for tool in sorted(get_allowed_tools(interface)):
        schema = TOOL_ENVELOPE_SCHEMAS[tool]
        required = ", ".join(
            f"{name}:{_type_name(kind)}"
            for name, kind in schema.get("required", {}).items()
            if name != "action_id"
        ) or "(none)"
        optional = ", ".join(
            f"{name}:{_type_name(kind)}"
            for name, kind in schema.get("optional", {}).items()
        ) or "(none)"
        lines.append(
            f"- {tool} [{tool_category(tool)}] required={required}; optional={optional}"
        )
    if str(interface or "").strip().lower().replace("-", "_") in {
        "web_direct", "webdirect", "remote",
    }:
        lines.extend(["", PROJECT_EVIDENCE_ACTION_CONTRACT])
    return "\n".join(lines)


def describe_tools(tools) -> list[dict[str, object]]:
    """Return compact machine-readable descriptions for trusted recovery prompts."""
    descriptions: list[dict[str, object]] = []
    for tool in tools:
        name = str(tool or "")
        schema = TOOL_ENVELOPE_SCHEMAS.get(name)
        if schema is None:
            continue
        descriptions.append({
            "tool": name,
            "purpose": _TOOL_PURPOSES.get(name, "Execute the canonical typed Runtime capability."),
            "required_fields": [
                field for field in schema.get("required", {}) if field != "action_id"
            ],
            "optional_fields": list(schema.get("optional", {})),
        })
    return descriptions


def validate_capability_registry() -> None:
    canonical = set(TOOL_ENVELOPE_SCHEMAS)
    categorized = set(TOOL_CATEGORIES)
    missing = sorted(canonical - categorized)
    extra = sorted(categorized - canonical)
    if missing or extra:
        raise ValueError(
            f"tool_capability_registry_drift:missing={missing};extra={extra}"
        )


validate_capability_registry()

__all__ = [
    "AGENT_ORCHESTRATION", "CAPABILITY_GAPS", "DETERMINISTIC_LOCAL",
    "FILE_TRANSFER", "PROJECT_SYNC", "PROTOCOL_CONTROL", "REMOTE_CONTROL",
    "SEMANTIC_DELEGATION",
    "SELF_REPAIR", "TOOL_CATEGORIES", "VERIFICATION", "WEB_LOOKUP",
    "describe_tools", "get_allowed_tools", "render_protocol_tool_section", "tool_category",
    "validate_capability_registry",
]
