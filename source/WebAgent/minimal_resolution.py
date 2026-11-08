"""Mandatory, bounded, read-only Agent Loop for uncertain ToolResult.

This is a child resolution loop, not an extra original atomic-task turn.
It may observe original scope, never replay a possibly executed mutation,
never grant authorization, and never return UNKNOWN as Executor terminal.
"""
from __future__ import annotations
import hashlib
import json
import ntpath
from collections.abc import Mapping

from agent_core.tri_one_runtime_terminal import GoalEvidence
from agent_core.tool_result_contract import decode_result
from .minimal_admission import admit_minimal_action, action_is_mutating
from .minimal_decision import MinimalDecisionError, parse_minimal_decision_with_diagnostics

RESOLUTION_MODEL_DECISIONS = 2
READ_ONLY_RESOLUTION_TOOLS = frozenset({
    "inspect_directory", "list_directory", "read_file", "observe_fact",
    "run_command",
})


def _same_path(first: object, second: object) -> bool:
    a, b = str(first or ""), str(second or "")
    if not a or not b:
        return False
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


def _bounded_original_path(action: Mapping) -> str:
    if action.get("tool") == "run_command":
        return str(action.get("working_directory") or "")
    return str(action.get("path") or "")


def _resolution_action_allowed(original: Mapping, proposed: Mapping) -> bool:
    tool = str(proposed.get("tool") or "")
    if tool not in READ_ONLY_RESOLUTION_TOOLS or action_is_mutating(dict(proposed)):
        return False
    target = _bounded_original_path(original)
    if not target:
        return False
    if tool == "inspect_directory":
        paths = proposed.get("paths")
        return (isinstance(paths, list) and len(paths) == 1
                and _same_path(paths[0], target))
    if tool == "run_command":
        # Arbitrary commands do not become safe merely because a user ran
        # an earlier command. Admission and proven observation still apply.
        return (original.get("tool") == "run_command"
                and _same_path(proposed.get("working_directory"), target))
    if original.get("tool") == "create_directory":
        return tool in {"list_directory", "observe_fact"} and _same_path(
            proposed.get("path"), target)
    return _same_path(proposed.get("path"), target)


def _verified_observation(original: Mapping, action: Mapping,
                          receipt: Mapping, raw: str, goal: str,
                          evidence_ref: str) -> GoalEvidence | None:
    if receipt.get("status") != "SUCCEEDED" or receipt.get("executed") is not True:
        return None
    src = original.get("tool")
    target = _bounded_original_path(original)
    tool = action.get("tool")
    if src == "create_directory":
        from .minimal_executor_contract import single_directory_contract_target
        if not single_directory_contract_target(goal, target):
            return None
        if tool == "inspect_directory":
            try:
                envelope = json.loads(str(raw))
            except (ValueError, TypeError):
                return None
            if not isinstance(envelope, dict):
                return None
            reports = envelope.get("paths")
            if (not isinstance(reports, list) or len(reports) != 1
                    or not isinstance(reports[0], dict)):
                return None
            report = reports[0]
            if (not _same_path(report.get("path"), target)
                    or report.get("authorized") is not True
                    or report.get("exists") is not True
                    or report.get("errors") != []
                    or report.get("inaccessible_count") != 0):
                return None
        elif tool == "list_directory":
            if not _same_path(receipt.get("resolved_path") or
                              receipt.get("requested_path"), target):
                return None
            if (receipt.get("reason") != "DIRECTORY_LISTED"
                    or receipt.get("data", {}).get("terminal_eligible") is not True):
                return None
        else:
            # A file.exists=true can also mean an ordinary file, not a dir.
            return None
        return GoalEvidence("PASS", "RESOLVED_DIRECTORY_POSTCONDITION",
                            (str(action["action_id"]),), target,
                            "RESOLVED_SINGLE_DIRECTORY")
    if src in {"read_file", "list_directory"} and tool == src:
        if not _same_path(action.get("path"), target):
            return None
        if not _same_path(receipt.get("resolved_path") or
                          receipt.get("requested_path"), target):
            return None
        data = receipt.get("data") or {}
        confirmed = (data.get("content_read") is True if src == "read_file"
                     else receipt.get("reason") == "DIRECTORY_LISTED"
                     and data.get("terminal_eligible") is True)
        if not confirmed:
            return None
        if target.casefold() not in goal.casefold():
            return None
        return GoalEvidence("PASS", "RESOLVED_READ_POSTCONDITION",
                            (str(action["action_id"]),), target,
                            "RESOLVED_SINGLE_OBSERVATION")
    if src == "write_file" and tool == "read_file":
        if not _same_path(action.get("path"), target):
            return None
        data = receipt.get("data") or {}
        if data.get("content_read") is not True or not isinstance(data.get("text"), str):
            return None
        if data["text"] != original.get("content"):
            return None
        if target.casefold() not in goal.casefold():
            return None
        return GoalEvidence("PASS", "RESOLVED_FILE_CONTENT_POSTCONDITION",
                            (str(action["action_id"]),), target,
                            "RESOLVED_SINGLE_FILE_WRITE")
    return None


def resolve_unknown_action(loop, *, original: dict, original_receipt: dict,
                           history: list[dict], goal: str, journal) -> dict:
    """Finish with VERIFIED/NEEDS_PREREQUISITE/BLOCKED, not model uncertainty.

    Nested resolution has 2 fresh model decisions. The original action is
    never replayed; every probe still passes normal admission and dispatch.
    """
    from .minimal_executor_contract import blocked_result
    original_id = str(original.get("action_id") or "")
    original_status = str(original_receipt.get("status") or "UNKNOWN")
    mutating = action_is_mutating(original)
    refs = [str(history[-1]["evidence_ref"])] if history else []
    last_status = original_status
    last_reason = str(original_receipt.get("reason") or "TOOL_OUTCOME_UNKNOWN")
    attempts = 0
    final_proof = None
    terminal = None

    def event(name, **fields):
        journal.append(name, original_action_id=original_id, **fields)
        loop._emit(name, request_id=loop.run_id, task_id=loop.task_id,
                   original_action_id=original_id, **fields)

    event("minimal_resolution_started", original_tool=original.get("tool"),
          original_status=original_status, is_mutating=mutating,
          original_evidence_ref=refs[0] if refs else "")
    for attempt in range(1, RESOLUTION_MODEL_DECISIONS + 1):
        attempts = attempt
        prompt = (
            "[SMARTAGENT_EVIDENCE_RESOLUTION_V1]\n"
            "This is a child Agent Loop to resolve an ambiguous ToolResult. "
            "The original action MAY have executed. NEVER replay the mutation. "
            "Select ONE read-only observation of the SAME exact target within "
            "original authorization; Runtime performs normal admission. "
            "For a directory use inspect_directory with paths:[exact_target] "
            "and recursive:false. If observation is prohibited, return blocked "
            "with precise reason; NEVER claim successful mutation. "
            "Return ONE JSON type=tool_call or type=blocked only.\n"
            "Goal: " + str(goal)[:1500] + "\n"
            "Original action: " + json.dumps({
                "tool": original.get("tool"), "path": _bounded_original_path(original),
                "action_id": original_id,
                "status": original_status, "reason": last_reason,
            }, ensure_ascii=False) + "\n"
            "Previously observed: " + json.dumps(
                [{"tool": e["action"].get("tool"),
                  "status": e["receipt"].get("status"),
                  "reason": e["receipt"].get("reason")}
                 for e in history[-3:]], ensure_ascii=False) + "\n"
            "Resolution attempt: " + str(attempt) + "/" +
            str(RESOLUTION_MODEL_DECISIONS) + "\n"
            "[/SMARTAGENT_EVIDENCE_RESOLUTION_V1]"
        )
        event("minimal_resolution_deciding", resolution_attempt=attempt)
        try:
            response = loop.planner(prompt, None, [])
            decision, _ = parse_minimal_decision_with_diagnostics(response,
                                                                  original_goal=goal)
        except (MinimalDecisionError, ValueError) as exc:
            last_reason = "RESOLUTION_DECISION_INVALID"
            event("minimal_resolution_decision_rejected", resolution_attempt=attempt,
                  reason_code=last_reason, error_type=type(exc).__name__)
            continue
        except BaseException as exc:
            # Transport may fail after submission; no unknown mutation retry.
            last_reason = "RESOLUTION_TRANSPORT_INTERRUPTED"
            event("minimal_resolution_transport_interrupted", resolution_attempt=attempt,
                  reason_code=last_reason, error_type=type(exc).__name__)
            break
        if decision.kind != "tool_call":
            last_reason = ("RESOLUTION_PLANNER_BLOCKED" if decision.kind == "blocked"
                           else "RESOLUTION_OBSERVATION_NOT_PERFORMED")
            event("minimal_resolution_no_observation", resolution_attempt=attempt,
                  reason_code=last_reason)
            continue
        action, rejection = admit_minimal_action(decision, loop.tools)
        if rejection or not action or not _resolution_action_allowed(original, action):
            last_reason = ("RESOLUTION_ACTION_DENIED" if rejection
                           else "RESOLUTION_NONOBSERVATION_PROPOSED")
            event("minimal_resolution_action_rejected", resolution_attempt=attempt,
                  reason_code=last_reason, executed=False)
            continue
        event("minimal_resolution_action_admitted", resolution_attempt=attempt,
              action_id=action["action_id"], tool=action["tool"], executed=False)
        loop._planner_original_actions[action["action_id"]] = dict(action)
        loop._canonical_action_proposals[action["action_id"]] = dict(action)
        try:
            raw = loop._execute_action(action)
        except BaseException as exc:
            last_reason = "RESOLUTION_TOOL_EXECUTION_INTERRUPTED"
            event("minimal_resolution_tool_interrupted", resolution_attempt=attempt,
                  error_type=type(exc).__name__, reason_code=last_reason)
            break
        receipt = decode_result(raw) or {
            "status": "UNKNOWN", "reason": "UNPARSEABLE_RESULT",
            "executed": False, "data": {},
        }
        digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()
        ev = "MIN-EV-" + hashlib.sha256(
            (action["action_id"] + "\0" + digest).encode("utf-8")).hexdigest()[:24]
        history.append({"action": action, "receipt": receipt, "evidence_ref": ev})
        refs.append(ev)
        last_status = str(receipt.get("status") or "UNKNOWN")
        last_reason = str(receipt.get("reason") or "UNKNOWN")
        loop.action_result_ledger[action["action_id"]] = {
            "action_id": action["action_id"], "tool": action["tool"],
            "request_id": loop.run_id, "task_id": loop.task_id,
            "task_epoch": loop.task_epoch,
            "produced_round": loop.turn_id,
            "result_id": ev, "result": str(raw), "result_sha256": digest,
            "result_status": last_status, "result_reason": last_reason,
            "execution_status": last_status,
            "verification_status": str((receipt.get("data") or {}).get(
                "verification_status") or "UNVERIFIED"),
            "executed": receipt.get("executed") is True,
            "evidence_status": "ACTIVE", "evidence_ref": ev,
        }
        event("minimal_resolution_observed", resolution_attempt=attempt,
              action_id=action["action_id"], evidence_ref=ev,
              tool=action["tool"], status=last_status,
              reason_code=last_reason, executed=receipt.get("executed") is True)
        if receipt.get("status") == "REJECTED":
            # A real denied read is an objective inability to resolve.
            terminal = "BLOCKED"
            last_reason = "RESOLUTION_OBSERVATION_ACCESS_DENIED"
            break
        final_proof = _verified_observation(original, action, receipt, raw, goal, ev)
        if final_proof and final_proof.verified:
            terminal = "VERIFIED"
            last_reason = final_proof.reason
            break
        # Positive absence/inspection failure does not demonstrate a prior
        # mutation failed. Observe again only when a distinct safe probe exists.
    if terminal is None:
        terminal = "BLOCKED"
        last_reason = "RESOLUTION_EVIDENCE_EXHAUSTED:" + last_reason[:75]
    result = (dict(blocked_result(terminal, last_reason, loop.turn_id))
              if terminal != "VERIFIED" else {
                  "schema": "SMARTAGENT_ATOMIC_EXECUTOR_RESULT_V1",
                  "outcome": "VERIFIED", "reason_code": last_reason,
                  "tool_status": last_status, "action_id": original_id,
                  "evidence_ref": refs[-1] if refs else "",
                  "model_round": loop.turn_id,
                  "executed": True, "goal_verified": True,
              })
    result.update(
        original_tool_status=original_status,
        last_observed_tool_status=last_status,
        side_effect_may_have_occurred=mutating,
        side_effect_outcome_confirmed=(terminal == "VERIFIED"),
        original_action_id=original_id,
        resolution_attempts=attempts,
        resolution_state=("EVIDENCE_VERIFIED" if terminal == "VERIFIED"
                          else "RESOLUTION_BLOCKED"),
        evidence_refs=refs,
    )
    event("minimal_resolution_concluded", terminal_status=terminal,
          resolution_attempts=attempts, reason_code=last_reason,
          evidence_ref=refs[-1] if refs else "")
    return {"result": result, "proof": final_proof, "evidence_refs": refs}
