"""Request-scoped prerequisite planning, without implicit host operations.

This is an *advisory*, typed Planner input. The Runtime continues to own
capability probing, authorization, execution, and verification. No path or
tool is authorized by a prerequisite assessment.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence


SCHEMA = "SMARTAGENT_PREREQUISITE_RESOLUTION_V2"

_COMMAND_INTENT = re.compile(
    r"\b(?:run_command|python|powershell|shell|command|exit[_ -]?code|compile|build|execute)\b"
    r"|(?:執行(?:命令|程式|測試)|退出碼|結束碼|編譯|運行|執行範圍)",
    re.IGNORECASE,
)
_PROJECT_INTENT = re.compile(
    r"\b(?:query_project|project_sync|inspect_project_scope|snapshot_id|project_root|index_only)\b"
    r"|(?:專案(?:查詢|索引|快照)|專案根目錄|專案範圍|索引查詢)",
    re.IGNORECASE,
)
_WRITE_INTENT = re.compile(
    r"\b(?:write_file|delete_path|install|deploy|commit_file_write|apply_edit_plan)\b"
    r"|(?:寫入|刪除|安裝|部署|修改檔案|產生檔案)",
    re.IGNORECASE,
)


def assess_prerequisites(
    request: str,
    authorized_paths: Sequence[str],
    environment_snapshot: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Classify what needs *actual Runtime evidence* before execution.

    Do not infer a user-approved install, expanded working directory, or
    completed prerequisite merely because a path/tool is mentioned.
    """
    text = str(request or "")
    paths = [str(path) for path in authorized_paths if isinstance(path, str) and path.strip()]
    env = environment_snapshot if isinstance(environment_snapshot, Mapping) else {}
    binaries = env.get("tools") if isinstance(env.get("tools"), Mapping) else {}
    use_command = bool(_COMMAND_INTENT.search(text))
    write_intent = bool(_WRITE_INTENT.search(text))
    project_intent = bool(_PROJECT_INTENT.search(text))
    checks = [
        {"id": "request_scope", "status": "NEEDS_RUNTIME_CONFIRMATION",
         "requirement": "Use only explicit request-owned authorized paths; never infer parents."},
        {"id": "target_path", "status": "NEEDS_RUNTIME_CONFIRMATION",
         "requirement": "Confirm each exact target path separately from surrounding prose."},
    ]
    if use_command:
        checks.extend([
            {"id": "command_contract", "status": "NEEDS_RUNTIME_CONFIRMATION",
             "requirement": "Declare operation, command, and required verification contract before dispatch."},
            {"id": "working_directory", "status": "NEEDS_RUNTIME_CONFIRMATION",
             "requirement": "Confirm existing absolute cwd is within authorized command scope; deny on mismatch."},
        ])
        python_entry = binaries.get("python") if isinstance(binaries.get("python"), Mapping) else {}
        checks.append(
            {"id": "python_capability",
             "status": "AVAILABLE_IN_LEDGER" if python_entry.get("status") == "AVAILABLE"
                       else "REQUIRES_ACTUAL_PROBE",
             "requirement": "Actual executable probe, not a model claim or manifest entry."}
        )
    if project_intent:
        checks.extend([
            {"id": "project_root", "status": "NEEDS_RUNTIME_CONFIRMATION",
             "requirement": "Select one existing explicit request-scoped directory; do not infer or expand to its parent."},
            {"id": "project_workspace", "status": "NEEDS_RUNTIME_CONFIRMATION",
             "requirement": "Use a canonical workspace/project_root accepted by Runtime Scope preflight; never treat a failed preflight as authorization."},
            {"id": "project_snapshot", "status": "NEEDS_RUNTIME_CONFIRMATION",
             "requirement": "Inspect an authorized exact project root and snapshot state; only when approved and needed may INDEX_ONLY build the scoped snapshot, then verify before query_project."},
        ])
    if write_intent:
        checks.append(
            {"id": "side_effect_permission", "status": "NEEDS_RUNTIME_CONFIRMATION",
             "requirement": "Only explicitly authorized writes. No automatic elevation or permission bypass."}
        )
    return {
        "schema": SCHEMA,
        "status": "CHECK_REQUIRED",
        "request_class": "COMMAND" if use_command else "PROJECT_QUERY" if project_intent
                         else "FILE_MUTATION" if write_intent else "OTHER",
        "exact_authorized_path_count": len(paths),
        "checks": checks,
        "on_missing": {
            "recoverable": "Planner may select authorized read/probe or separately authorized setup, including an exact scoped project inspection and INDEX_ONLY only after scope proof; verify result before the original action.",
            "unrecoverable": "Return a truthful BLOCKED/UNKNOWN with the original denial; do not broaden scope, clear journals, or replay uncertain actions.",
        },
        "executed": False,
        "authorization_granted": False,
        "verification_complete": False,
    }


def render_prerequisites(
    request: str,
    authorized_paths: Sequence[str],
    environment_snapshot: Mapping[str, object] | None = None,
) -> str:
    assessment = assess_prerequisites(request, authorized_paths, environment_snapshot)
    # Do not inflate response-only conversations with irrelevant host checks.
    # Their terminal semantics remain the responsibility of the existing
    # progress and safety contracts, not this advisory assessment.
    if assessment["request_class"] == "OTHER" and not assessment["exact_authorized_path_count"]:
        return ""
    return (
        "[SMARTAGENT_PREREQUISITE_RESOLUTION]\n"
        + json.dumps(assessment, ensure_ascii=False, separators=(",", ":"))
        + "\n[/SMARTAGENT_PREREQUISITE_RESOLUTION]"
    )
