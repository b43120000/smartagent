"""Central fail-closed Turbo OS-broker admission for host-side mutations.

Not an OS attestation or permission grant. A requested Turbo command cannot
fall back to a privileged controller subprocess when a broker is unavailable.
"""
from __future__ import annotations
from .turbo_sandbox import resolve_turbo_state, sandbox_evidence

# All known typed mutation / code execution tools; extend this set when adding
# a new mutating Runtime tool. Inspection remains available for recovery.
MUTATORS = frozenset({
    "run_command", "create_directory", "write_file",
    "begin_file_write", "write_file_chunk", "commit_file_write",
    "abort_file_write",
    "delete_path", "apply_edit_plan", "execute_artifact_bundle",
    "execute_frozen_plan", "project_sync", "web_edit_file",
    "update_semantic_map", "update_semantic_map_file",
    "download_artifact", "build_project_bundle", "build_project_delta",
})
# save_session_summary is Runtime-owned metadata at a fixed workspace path,
# NOT a Planner-selected filesystem target or shell execution. It keeps the
# same scoped application behavior for ACL OFF, ACL ON/Turbo OFF and
# ACL ON/Turbo ON; normal workspace permissions still apply.
NON_COMMAND_OPERATIONS = frozenset({
    "web_search", "ask_executor", "read_file", "list_directory",
    "inspect_directory", "find_file", "query_project", "observe_fact",
    "save_session_summary",
})

def needs_broker(tool: str) -> bool:
    return str(tool) in MUTATORS

def assess(workspace=None, *, tool: str, action_id: str = "") -> dict:
    """Use runtime-owned ACL+workspace policy on *every* dispatched mutation.

    A corrupted ACL record or shared policy blocks; a valid ACL OFF always
    follows normal Software Guard regardless of a stale persisted Turbo ON.
    Successful disposable AppContainer tests never authorize real work.
    """
    # Advisory admission is occasionally called directly by planners.
    # A no-command/non-mutation action must be identical to the ordinary
    # execution path in ALL ACL/Turbo modes. Do not read or require a
    # command sandbox for web research, observations or ordinary dialogue.
    # The protected mutation list above is still enforced at dispatch.
    if not needs_broker(tool):
        return {
            "decision": "NORMAL",
            "reason_code": "NON_COMMAND_OPERATION",
            "state": None,
            "evidence": {"requested_operation": str(tool),
                         "executed": False,
                         "authorization_granted": False,
                         "os_execution_result": "NOT_APPLICABLE"},
        }
    # Task workspace may be a project directory (including Turbo Root).
    # Only the operator-owned policy may identify its authorization parent.
    state = resolve_turbo_state(workspace)
    authorization_workspace = workspace
    if (state["acl_mode"] == "on" and state["turbo_requested"] == "ON"
            and state["scope_verdict"] != "TURBO_SCOPE_ALLOWED"):
        from .workspace_access import resolve_turbo_authorization_workspace
        parent = resolve_turbo_authorization_workspace(workspace)
        if parent is not None:
            authorization_workspace = parent
            state = resolve_turbo_state(parent)
    evidence = sandbox_evidence(state, requested_operation=tool, action_id=action_id)
    if state["sandbox_status"] == "FAILED":
        decision = "BLOCKED"
    elif state["acl_mode"] == "off" or state["turbo_requested"] == "OFF":
        decision = "NORMAL"
    elif state["acl_mode"] == "on" and state["turbo_requested"] == "ON":
        # Presence of protected enrollment only permits attempting the
        # native OS broker. It is NOT a permission grant: every action must
        # independently attest its Windows process token, Job Object, OS
        # negative access and active policy revision before execution.
        from .turbo_os_broker_client import active_broker_available
        decision = ("BROKER_REQUIRED" if
                    active_broker_available(authorization_workspace) else "BLOCKED")
    else:
        decision = "BLOCKED"
    return {
        "decision": decision,
        "reason_code": state["reason_code"],
        "state": state,
        "evidence": evidence,
        "authorization_workspace": str(authorization_workspace or ""),
        "task_working_directory": str(workspace or ""),
    }

def admit_action(workspace, action: dict | None) -> dict:
    """One dispatch policy shared by PokeClaw, Minimal Core and typed tools.

    A no-command/observation action must not require native OS isolation.
    Only Runtime-admitted mutating actions are candidates for Broker.
    Returning BROKER_REQUIRED is not permission to execute as the host:
    the typed dispatch re-attests the native Broker for every action.
    """
    if not action:
        return {
            "decision": "NOT_APPLICABLE",
            "reason_code": "ACTION_NOT_ADMITTED",
            "state": None, "evidence": {},
        }
    tool = str(action.get("tool") or "")
    return assess(workspace, tool=tool,
                  action_id=str(action.get("action_id") or ""))


__all__ = [
    "MUTATORS", "NON_COMMAND_OPERATIONS", "needs_broker", "assess",
    "admit_action",
]
