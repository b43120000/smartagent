"""Tool-agnostic PokeClaw Agent Loop for SmartAgent's Executor-only mode.

The model chooses one ToolCall or Finish each turn. The Runtime owns tool
admission, actual ToolResult, action history, side-effect replay protection,
and per-turn Shared Blackboard persistence. No Goal Driver, Prerequisite
planner, task-specific sequence grammar or speculative whole-goal PASS.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from collections.abc import Mapping

from agent_core.executor_run_blackboard import (
    _safe_arguments, begin_executor_run, append_executor_event,
    finish_executor_run, projection,
)
from agent_core.task_progress import set_runtime_state
from agent_core.tool_result_contract import decode_result

from .minimal_admission import admit_minimal_action, action_is_mutating
from .minimal_decision import MinimalDecisionError, parse_minimal_decision_with_diagnostics


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, default=str,
    ).encode("utf-8")).hexdigest()


def _tool_result(status: str, reason: str, payload: object = None) -> dict:
    """Exactly PokeClaw's three tool-facing fields; semantics are action-only."""
    if status == "SUCCEEDED":
        text = json.dumps(
            {"reason": reason, "result": payload if payload is not None else {}},
            ensure_ascii=False, default=str, separators=(",", ":"),
        )
        # Truncation is explicit; an incomplete listing proves no absence.
        if len(text) > 6000:
            text = json.dumps({
                "reason": reason, "data_preview": text[:5500],
                "truncated": True, "complete_result_available_in_blackboard": False,
            }, ensure_ascii=False, separators=(",", ":"))
        return {"isSuccess": True, "data": text, "error": None}
    return {
        "isSuccess": False, "data": None,
        "error": (
            ("EXECUTION_OUTCOME_UNCERTAIN:" if status in {"UNKNOWN", "RUNNING", "PARTIAL"}
             else "ACTION_NOT_EXECUTED:" if status == "REJECTED"
             else "TOOL_FAILED:")
            + str(reason or "UNCLASSIFIED_RESULT")[:240]
        ),
    }


def _prompt(request: str, tools, recent: list[dict], *, round_id: int,
            max_rounds: int, hint: str = "") -> str:
    from .minimal_core_loop import tool_catalog
    catalog = tool_catalog(tools)
    return (
        "[POKECLAW_EXECUTOR_LOOP_V1]\n"
        "You are the planning/decision brain. The local Runtime owns execution.\n"
        "Respond with exactly ONE JSON object per round:\n"
        'Tool call: {"type":"tool_call","name":"observe_fact","arguments":{"path":"C:\\\\example"}}\n'
        'Finish: {"type":"finish","summary":"what you actually observed or did"}\n'
        'Blocked: {"type":"blocked","reason":"specific tool/error blocker"}\n'
        "No Markdown fences, no invented observations, no claimed success without results.\n"
        "Observe first; decide from actual ToolResult. If missing, a permitted "
        "tool may create the requested target. Independently inspect after mutation. "
        "Do not repeat potentially executed mutations without new evidence. "
        "ToolResult.isSuccess means the tool succeeded, NOT that an observed "
        "boolean was true or that the whole user task is verified.\n"
        "Each ToolResult uses EXACT PokeClaw fields: isSuccess, data, error. "
        "Admission rejection is an Action failure, not a filesystem absence. "
        "Incomplete outputs cannot prove missing content. "
        "Runtime security and precise path authorization always apply.\n"
        "Goal Driver and Prerequisite decision engines are DISABLED. "
        "There is no fixed task-specific sequence gate. "
        "Shared Blackboard records each round automatically; never ask a tool "
        "to write its own evidence record. At Finish include the actual status "
        "of every requested step, not invented parent results.\n"
        f"Workspace: {tools.workspace_root}\n"
        "Available tools: " + json.dumps(
            catalog, ensure_ascii=False, separators=(",", ":")) + "\n"
        "Original user request:\n" + request + "\n"
        f"Round: {round_id}/{max_rounds}\n"
        "Recent actual PokeClaw ToolResults:\n" + json.dumps(
            recent[-6:], ensure_ascii=False, separators=(",", ":")) + "\n"
        + (f"Runtime repair information: {hint}\n" if hint else "")
        + "[/POKECLAW_EXECUTOR_LOOP_V1]"
    )


def _terminal_receipt(loop, summary: str, board: Mapping) -> dict:
    """Model reported completion; not fabricated Task Goal Verification."""
    from agent_core.tri_one_runtime_terminal import TERMINAL_SCHEMA
    payload = {
        "schema": TERMINAL_SCHEMA, "admission_status": "ACCEPTED",
        "decision": "POKECLAW_EXECUTOR_MODEL_FINISH_V1",
        "completion_kind": "EXECUTOR_MODEL_REPORTED_FINISH",
        "request_id": loop.run_id, "task_id": loop.task_id,
        "task_epoch": loop.task_epoch, "round": loop.turn_id,
        "task_outcome": "UNKNOWN", "runtime_state": "COMPLETED",
        "evidence_refs": list(board["terminal"]["tool_evidence_refs"]),
        "evidence_verdict": "UNVERIFIED",
        "verification_status": "MODEL_REPORTED_COMPLETE_UNVERIFIED",
        "goal_evidence_reason": "POKECLAW_FINISH_NOT_INDEPENDENT_GOAL_PROOF",
        "handoff_id": board["handoff_id"],
        "blackboard_event_count": len(board["events"]),
        "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
    }
    payload["receipt_id"] = "TERM-" + _digest(payload)[:20].upper()
    return payload


def _finish(loop, root: Path, request: str, *, kind: str,
            summary: str, complete: bool) -> str:
    board = finish_executor_run(
        root, loop, terminal_kind=kind, summary=summary,
        reported_outcome="UNKNOWN",
    )
    loop.executor_blackboard_terminal = board
    loop.executor_result = {
        "schema": "SMARTAGENT_POKECLAW_EXECUTOR_RESULT_V1",
        "outcome": kind, "executor_state": board["executor_state"],
        "terminal_status": kind, "goal_verified": False,
        "handoff_id": board["handoff_id"],
        "tool_evidence_refs": list(board["terminal"]["tool_evidence_refs"]),
    }
    loop.terminal_outcome = "UNKNOWN"
    if complete:
        loop.terminal_acceptance = _terminal_receipt(loop, summary, board)
        loop.protocol_state = "COMPLETED"
        set_runtime_state(loop.task_id, "COMPLETED", root=loop.progress_root)
    else:
        loop.terminal_acceptance = {}
        loop.protocol_state = "BLOCKED"
        set_runtime_state(
            loop.task_id, "PAUSED", reason=kind, root=loop.progress_root,
        )
    if hasattr(loop, "_emit"):
        loop._emit(
            "pokeclaw_executor_terminal", task_id=loop.task_id,
            request_id=loop.run_id, terminal_kind=kind,
            handoff_id=board["handoff_id"],
            model_reported_only=True, goal_verified=False,
            event_count=len(board["events"]),
        )
    p = projection(board)
    # These are observed persisted records, not inferred parent progress.
    trailer = (
        "\n\nExecutor Blackboard（已寫入並回讀）：\n"
        f"Executor：{p['executor_state']}\n"
        f"Goal Driver：{p['goal_driver_state']}\n"
        f"Prerequisite：{p['prerequisite_state']}\n"
        f"Handoff ID：{p['handoff_id']}\n"
        f"Tool Evidence：{', '.join(p['tool_evidence_refs']) or '無'}\n"
        f"Parent Dispatch：{p['parent_dispatch_state']}\n"
        f"Parent Consumption：{p['parent_consumption_state']}\n"
        "整體目標：模型回報完成，尚無獨立 Goal 驗收（非 VERIFIED）。"
    )
    return str(summary) + trailer


def run_pokeclaw_executor(loop, request: str) -> str:
    """Execute arbitrary registered tools; Blackboard is the only recorder."""
    root = Path(loop.progress_root or loop.workspace)
    if root.name.casefold() == "source" and (root.parent / "collect.bat").is_file():
        root = root.parent
    board = begin_executor_run(root, loop, request)
    loop.executor_blackboard_terminal = board
    loop.executor_result = {}
    loop.terminal_acceptance = {}
    loop.terminal_outcome = "UNKNOWN"
    recent = []
    last_fingerprint = ""
    repeated = 0
    invalids = 0
    hint = ""
    seen_mutations = set()
    max_rounds = min(int(loop.max_turns), 30)
    for turn in range(1, max_rounds + 1):
        loop._check_cancelled()
        loop.turn_id = turn
        loop.tools._protocol_turn_seq = turn
        prompt = _prompt(
            request, loop.tools, recent, round_id=turn,
            max_rounds=max_rounds, hint=hint,
        )
        hint = ""
        attachments = loop.tools.take_pending_attachments()
        loop.protocol_state = "DECIDING"
        # Model DOM transport interruption may have sent a turn. Do not retry
        # the transport without its established correlation protocol.
        response = loop.planner(prompt, None, attachments)
        try:
            decision, metadata = parse_minimal_decision_with_diagnostics(
                response, original_goal=request,
            )
        except MinimalDecisionError as exc:
            invalids += 1
            append_executor_event(root, loop, {
                "round": turn, "kind": "MODEL_DECISION_REJECTED",
                "decision_sha256": _digest(str(response)),
                "error_code": exc.code, "executed": False,
            })
            if invalids >= 3:
                return _finish(
                    loop, root, request, kind="FORMAT_ERROR",
                    summary="模型決策格式連續三次無效：" + exc.code,
                    complete=False,
                )
            hint = "上一輪 JSON 格式無效：" + exc.code
            continue
        invalids = 0
        if decision.kind == "finish":
            append_executor_event(root, loop, {
                "round": turn, "kind": "MODEL_FINISH",
                "summary_sha256": hashlib.sha256(
                    decision.message.encode("utf-8")
                ).hexdigest(),
                "prior_tool_observation_count": sum(
                    row.get("kind") == "TOOL_RESULT" for row in board["events"]
                ),
            })
            return _finish(
                loop, root, request, kind="MODEL_REPORTED_COMPLETE_UNVERIFIED",
                summary=decision.message, complete=True,
            )
        if decision.kind == "blocked":
            append_executor_event(root, loop, {
                "round": turn, "kind": "MODEL_BLOCKED",
                "reason_sha256": _digest(decision.message),
            })
            return _finish(
                loop, root, request, kind="BLOCKED",
                summary="Planner 回報無法繼續：" + decision.message,
                complete=False,
            )
        proposed = {"tool": decision.tool, **dict(decision.arguments or {})}
        signature = _digest(proposed)
        repeated = repeated + 1 if signature == last_fingerprint else 0
        last_fingerprint = signature
        action, rejected = admit_minimal_action(decision, loop.tools)
        if action and action_is_mutating(action):
            fingerprint = _digest({
                key: value for key, value in action.items() if key != "action_id"
            })
            if fingerprint in seen_mutations:
                action = None
                rejected = "MUTATION_REPLAY_DENIED_WITHOUT_INDEPENDENT_PROOF"
            else:
                # An admitted action may have a side effect even when the
                # executor or transport fails after the side effect.
                seen_mutations.add(fingerprint)
        if action is None:
            diagnosis = dict(getattr(
                loop.tools, "_last_minimal_admission_diagnostic", {},
            ) or {})
            tool_result = _tool_result("REJECTED", rejected)
            row = append_executor_event(root, loop, {
                "round": turn, "kind": "TOOL_RESULT",
                "toolName": decision.tool,
                "arguments": _safe_arguments(decision.arguments),
                "arguments_sha256": _digest(decision.arguments),
                "toolResult": tool_result,
                "runtimeEvidence": {
                    "status": "REJECTED", "executed": False,
                    "reason_code": str(rejected)[:160],
                    "scope_failure_stage": diagnosis.get("scope_failure_stage", ""),
                    "scope_cause_digest": diagnosis.get("scope_cause_digest", ""),
                    "directory_preconditions": diagnosis.get("directory_preconditions", {}),
                    "evidence_ref": "",
                },
            })
            recent.append({
                "round": turn, "tool": decision.tool,
                "toolResult": tool_result,
                "admission_verdict": "REJECTED_BEFORE_EXECUTION",
            })
            hint = f"Tool Admission 拒絕：{rejected}。不可把拒絕視為檔案不存在。"
            if repeated >= 2:
                return _finish(
                    loop, root, request, kind="BLOCKED",
                    summary="相同工具請求反覆被拒：" + str(rejected),
                    complete=False,
                )
            continue

        loop._canonical_action_proposals[action["action_id"]] = dict(action)
        loop._planner_original_actions[action["action_id"]] = {
            **proposed, "action_id": action["action_id"],
        }
        mutating = action_is_mutating(action)
        append_executor_event(root, loop, {
            "round": turn, "kind": "ACTION_ADMITTED",
            "toolName": action["tool"], "action_id": action["action_id"],
            "arguments": _safe_arguments(decision.arguments),
            "arguments_sha256": _digest(decision.arguments),
            "mutating": mutating,
        })
        loop.protocol_state = "EXECUTING"
        try:
            raw_result = loop._execute_action(action)
        except Exception as exc:
            # Never replay a possibly side-effecting command.
            raw_result = json.dumps({
                "_runtime_result": {
                    "schema": "SMARTAGENT_RUNTIME_TOOL_RESULT_V1",
                    "tool": action["tool"], "status": "UNKNOWN",
                    "reason": "TOOL_EXCEPTION_OUTCOME_UNKNOWN",
                    "executed": True, "requested_path": "",
                    "resolved_path": None,
                    "data": {"exception_class": type(exc).__name__},
                },
            })
        receipt = decode_result(raw_result) or {
            "status": "UNKNOWN", "reason": "UNPARSEABLE_TOOL_RESULT",
            "executed": False, "data": {},
        }
        runtime_status = str(receipt.get("status") or "UNKNOWN")
        runtime_reason = str(receipt.get("reason") or "UNCLASSIFIED_TOOL_RESULT")
        digest = hashlib.sha256(str(raw_result).encode("utf-8")).hexdigest()
        evidence_ref = "MIN-EV-" + hashlib.sha256(
            (action["action_id"] + "\0" + digest).encode("utf-8")
        ).hexdigest()[:24]
        result_data = receipt.get("data")
        tool_result = _tool_result(runtime_status, runtime_reason, result_data)
        loop.action_result_ledger[action["action_id"]] = {
            "action_id": action["action_id"], "tool": action["tool"],
            "request_id": loop.run_id, "task_id": loop.task_id,
            "task_epoch": loop.task_epoch, "produced_round": turn,
            "result_id": evidence_ref, "result": str(raw_result),
            "result_sha256": digest, "result_status": runtime_status,
            "result_reason": runtime_reason,
            "execution_status": runtime_status,
            "verification_status": str(
                (result_data or {}).get("verification_status", "UNVERIFIED")
            ) if isinstance(result_data, Mapping) else "UNVERIFIED",
            "executed": receipt.get("executed") is True,
            "evidence_status": "ACTIVE",
            "evidence_ref": evidence_ref,
        }
        row = append_executor_event(root, loop, {
            "round": turn, "kind": "TOOL_RESULT",
            "toolName": action["tool"], "action_id": action["action_id"],
            "toolResult": tool_result,
            "runtimeEvidence": {
                "status": runtime_status, "reason_code": runtime_reason[:160],
                "executed": receipt.get("executed") is True,
                "result_sha256": digest, "evidence_ref": evidence_ref,
                "side_effect_may_have_occurred": bool(
                    mutating and receipt.get("executed")
                ),
            },
        })
        recent.append({
            "round": turn, "tool": action["tool"],
            "action_id": action["action_id"],
            "toolResult": tool_result, "evidence_ref": evidence_ref,
        })
        loop.protocol_state = "OBSERVED"
        loop.executor_result = {
            "schema": "SMARTAGENT_POKECLAW_EXECUTOR_RESULT_V1",
            "executor_state": "OBSERVED", "tool_status": runtime_status,
            "tool_result": dict(tool_result), "evidence_ref": evidence_ref,
            "goal_verified": False,
        }
        loop._emit(
            "pokeclaw_executor_tool_result", request_id=loop.run_id,
            task_id=loop.task_id, task_epoch=loop.task_epoch,
            round=turn, tool=action["tool"], evidence_ref=evidence_ref,
            is_success=tool_result["isSuccess"],
            runtime_status=runtime_status, executed=receipt.get("executed") is True,
        )
        if getattr(loop, "continuation_store", None):
            loop.continuation_store.snapshot(loop)
        if runtime_status in {"UNKNOWN", "RUNNING", "PARTIAL"} and mutating:
            return _finish(
                loop, root, request, kind="EXECUTION_OUTCOME_UNKNOWN",
                summary="可能產生副作用的工具結果未確定，禁止重送：" + runtime_reason,
                complete=False,
            )
    return _finish(
        loop, root, request, kind="MAX_ROUNDS",
        summary="PokeClaw Executor 已達決策回合上限，尚未收到 Finish。",
        complete=False,
    )


__all__ = ["run_pokeclaw_executor"]
