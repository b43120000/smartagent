"""Request-scoped prerequisite planning, without implicit host operations.

This is an *advisory*, typed Planner input. The Runtime continues to own
capability probing, authorization, execution, and verification. No path or
tool is authorized by a prerequisite assessment.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence


SCHEMA = "SMARTAGENT_PREREQUISITE_RESOLUTION_V1"

_COMMAND_INTENT = re.compile(
    r"\b(?:run_command|python|powershell|shell|command|exit[_ -]?code|compile|build|execute)\b"
    r"|(?:執行(?:命令|程式|測試)|退出碼|結束碼|編譯|運行|執行範圍)",
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
    if write_intent:
        checks.append(
            {"id": "side_effect_permission", "status": "NEEDS_RUNTIME_CONFIRMATION",
             "requirement": "Only explicitly authorized writes. No automatic elevation or permission bypass."}
        )
    return {
        "schema": SCHEMA,
        "status": "CHECK_REQUIRED",
        "request_class": "COMMAND" if use_command else "FILE_MUTATION" if write_intent else "OTHER",
        "exact_authorized_path_count": len(paths),
        "checks": checks,
        "on_missing": {
            "recoverable": "Planner may select an authorized read/probe or separately authorized setup tool; verify its result before retrying the original action.",
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
