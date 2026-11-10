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
    "delete_path", "apply_edit_plan", "execute_artifact_bundle",
    "execute_frozen_plan", "project_sync", "web_edit_file",
    "update_semantic_map", "update_semantic_map_file",
    "download_artifact", "build_project_bundle", "build_project_delta",
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
    state = resolve_turbo_state(workspace)
    evidence = sandbox_evidence(state, requested_operation=tool, action_id=action_id)
    if state["sandbox_status"] == "FAILED":
        decision = "BLOCKED"
    elif state["acl_mode"] == "off" or state["turbo_requested"] == "OFF":
        decision = "NORMAL"
    else:
        # ACL ON + requested Turbo ON: production broker/lease not attested.
        decision = "BLOCKED"
    return {
        "decision": decision,
        "reason_code": state["reason_code"],
        "state": state,
        "evidence": evidence,
    }

__all__ = ["MUTATORS", "needs_broker", "assess"]
