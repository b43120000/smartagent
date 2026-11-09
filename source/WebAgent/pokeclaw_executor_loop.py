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
    finish_executor_run, record_finish_comparison, projection,
)
from agent_core.task_progress import set_runtime_state
from agent_core.tool_result_contract import decode_result
from agent_core.runtime_finish_comparator import compare_runtime_finish, finish_runtime_field_catalog
from agent_core.finish_evidence_requirement import assess_finish_evidence_requirement

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
    details = ""
    if isinstance(payload, Mapping):
        process = {
            key: payload[key] for key in (
                "process_state", "exit_code", "timed_out",
                "verification_status", "expectation_status",
            )
            if key in payload
        }
        if process:
            details = "; " + json.dumps(
                process, ensure_ascii=False, separators=(",", ":"),
            )[:600]
    return {
        "isSuccess": False, "data": None,
        "error": (
            ("EXECUTION_OUTCOME_UNCERTAIN:" if status in {"UNKNOWN", "RUNNING", "PARTIAL"}
             else "ACTION_NOT_EXECUTED:" if status == "REJECTED"
             else "TOOL_FAILED:")
            + str(reason or "UNCLASSIFIED_RESULT")[:240] + details
        ),
    }


def _prompt(request: str, tools, recent: list[dict], *, round_id: int,
            max_rounds: int, hint: str = "", field_catalog=None,
            evidence_requirement=None) -> str:
    from .minimal_core_loop import tool_catalog
    catalog = tool_catalog(tools)
    return (
        "[POKECLAW_EXECUTOR_LOOP_V1]\n"
        "You are the planning/decision brain. The local Runtime owns execution.\n"
        "Respond with exactly ONE JSON object per round:\n"
        'Tool call: {"type":"tool_call","name":"observe_fact","arguments":{"path":"C:\\\\example"}}\n'
        'Finish: {"type":"finish","summary":"observations","expected_runtime":[{"evidence_ref":"<actual MIN-EV reference>","fields":{"status":"<actual Runtime status>","data.<task-relevant key>":"<typed observed value>"}}],"goal_checks":[{"criterion":"<requirement from original request>","evidence_ref":"<actual MIN-EV reference>","field":"data.<task-relevant key>","operator":"eq","expected":"<typed expected value>"}]}\n'
        'Finish for eligible text: {"type":"finish","summary":"the complete answer","expected_runtime":[]}\n'
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
        "of every requested step, not invented parent results. At Finish include "
        "one expected_runtime entry for EACH actual ToolResult evidence_ref, "
        "using full Runtime status and dotted typed data fields. Never omit failed "
        "tools or invent evidence. Generate the Finish fields yourself from the "
        "original user request and actual ToolResults; there is NO fixed "
        "tool-specific Finish template. Use dotted scalar paths from the "
        "Runtime field catalogue. For goals without independent Runtime "
        "postcondition proof, add goal_checks expressing each requested "
        "completion condition as an evidence-backed predicate. Use typed "
        "boolean/number/string/null values, never strings for booleans. "
        "Runtime compares claims AND checks the original Goal; model "
        "cannot grant PASS merely by declaring it.\n"
        f"Workspace: {tools.workspace_root}\n"
        "Available tools: " + json.dumps(
            catalog, ensure_ascii=False, separators=(",", ":")) + "\n"
        "Original user request:\n" + request + "\n"
        f"Round: {round_id}/{max_rounds}\n"
        "Recent actual PokeClaw ToolResults:\n" + json.dumps(
            recent[-6:], ensure_ascii=False, separators=(",", ":")) + "\n"
        "Upstream evidence requirement (Runtime-owned minimum, not task plan):\n"
        + json.dumps(evidence_requirement or {}, ensure_ascii=False, separators=(",", ":")) + "\n"
        "Finish without ToolResult is allowed ONLY if answer_only_eligible=true, "
        "no action was attempted and the answer is valid. For TEXT-only, "
        "expected_runtime must be [] (or omitted), goal_checks need no tool. "
        "For tasks requiring tools or external state, text alone is NEVER "
        "completion evidence. Mixed tasks must retain all ToolResults.\n"
        "Runtime available Finish fields (derived, NOT a required template):\n"
        + json.dumps(field_catalog or [], ensure_ascii=False, separators=(",", ":"))[:6800] + "\n"
        + (f"Runtime repair information: {hint}\n" if hint else "")
        + "[/POKECLAW_EXECUTOR_LOOP_V1]"
    )


def _terminal_receipt(loop, summary: str, board: Mapping,
                      comparison: Mapping) -> dict:
    """Runtime-owned terminal: PASS or FAIL, never model-authorized acceptance."""
    from agent_core.tri_one_runtime_terminal import TERMINAL_SCHEMA
    verdict = comparison["verdict"]
    payload = {
        "schema": TERMINAL_SCHEMA, "admission_status": "ACCEPTED",
        "decision": "RUNTIME_FINISH_COMPARATOR_V1",
        "completion_kind": comparison.get("completion_kind", "EVIDENCE_BACKED"),
        "request_id": loop.run_id, "task_id": loop.task_id,
        "task_epoch": loop.task_epoch, "round": loop.turn_id,
        "task_outcome": "SUCCESS" if verdict == "PASS" else "FAILED",
        "runtime_state": "COMPLETED",
        "evidence_refs": list(comparison["evidence_refs"]),
        "evidence_verdict": verdict, "verification_status": verdict,
        "final_verdict": verdict,
        "goal_evidence_reason": comparison["goal_evidence_reason"],
        "matched_condition_id": comparison["goal_kind"],
        **({
            "admission_reason": "ANSWER_ONLY_RESPONSE_ADMITTED",
            "answer_sha256": comparison["answer_sha256"],
            "goal_sha256": hashlib.sha256(
                str(getattr(loop, "task_request", "") or "").encode("utf-8")
            ).hexdigest(),
        } if comparison.get("completion_kind") == "ANSWER_ONLY"
             and comparison["verdict"] == "PASS" else {}),
        "goal_coverage_basis": comparison.get("coverage_basis", "NOT_VERIFIED"),
        "comparison": {
            "reason_code": comparison["reason_code"],
            "mismatches": list(comparison["mismatches"]),
            "tool_results": list(comparison["tool_results"]),
        },
        "handoff_id": board["handoff_id"],
        "blackboard_event_count": len(board["events"]),
        "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
    }
    payload["receipt_id"] = "TERM-" + _digest(payload)[:20].upper()
    return payload


def _finish(loop, root: Path, request: str, *, kind: str,
            summary: str, complete: bool, expected_runtime=None,
            goal_checks=None) -> str:
    board = finish_executor_run(
        root, loop, terminal_kind=kind, summary=summary,
        reported_outcome="UNKNOWN",
    )
    comparison = None
    if complete:
        comparison = compare_runtime_finish(loop, root, request, expected_runtime,
                                            goal_checks=goal_checks,
                                            answer=summary)
        board = record_finish_comparison(root, loop, comparison)
    loop.executor_blackboard_terminal = board
    loop.executor_result = {
        "schema": "SMARTAGENT_POKECLAW_EXECUTOR_RESULT_V1",
        "outcome": kind, "executor_state": board["executor_state"],
        "terminal_status": kind,
        "goal_verified": bool(comparison and comparison["verdict"] == "PASS"),
        "handoff_id": board["handoff_id"],
        "tool_evidence_refs": list(board["terminal"]["tool_evidence_refs"]),
    }
    if complete:
        loop.terminal_acceptance = _terminal_receipt(loop, summary, board, comparison)
        loop.terminal_outcome = loop.terminal_acceptance["task_outcome"]
        loop.protocol_state = "COMPLETED"
        set_runtime_state(loop.task_id, "COMPLETED", root=loop.progress_root)
    else:
        loop.terminal_acceptance = {}
        loop.terminal_outcome = "UNKNOWN"
        loop.protocol_state = "BLOCKED"
        set_runtime_state(
            loop.task_id, "PAUSED", reason=kind, root=loop.progress_root,
        )
    if hasattr(loop, "_emit"):
        loop._emit(
            "pokeclaw_executor_terminal", task_id=loop.task_id,
            request_id=loop.run_id, terminal_kind=kind,
            handoff_id=board["handoff_id"],
            model_reported_only=True,
            goal_verified=bool(comparison and comparison["verdict"] == "PASS"),
            event_count=len(board["events"]),
        )
    p = projection(board)
    terminal_line = (
        f"Runtime 最終驗收：{comparison['verdict']}（{comparison['reason_code']}）"
        if comparison else "尚未進入 Runtime 最終驗收。"
    )
    mismatch_lines = ""
    if comparison and comparison["mismatches"]:
        mismatch_lines = "\n不一致欄位：" + json.dumps(
            comparison["mismatches"], ensure_ascii=False, separators=(",", ":"),
        )[:3000]
    trailer = (
        "\n\nExecutor Blackboard（已寫入並回讀）：\n"
        f"Executor：{p['executor_state']}\n"
        f"Goal Driver：{p['goal_driver_state']}\n"
        f"Prerequisite：{p['prerequisite_state']}\n"
        f"Handoff ID：{p['handoff_id']}\n"
        f"Tool Evidence：{', '.join(p['tool_evidence_refs']) or '無'}\n"
        f"Parent Dispatch：{p['parent_dispatch_state']}\n"
        f"Parent Consumption：{p['parent_consumption_state']}\n"
        f"整體目標：{terminal_line}{mismatch_lines}"
    )
    if comparison and comparison["verdict"] == "PASS" and (
        comparison.get("completion_kind") == "ANSWER_ONLY"
    ):
        # A creative/text response is the artifact. Do not append internal
        # Blackboard logs to the actual user's joke/translation/answer.
        return str(summary)
    return str(summary) + trailer


def run_pokeclaw_executor(loop, request: str) -> str:
    """Execute arbitrary registered tools; Blackboard is the only recorder."""
    root = Path(loop.progress_root or loop.workspace)
    if root.name.casefold() == "source" and (root.parent / "collect.bat").is_file():
        root = root.parent
    requirement = assess_finish_evidence_requirement(
        request,
        attachments=bool(getattr(loop, "_tri_one_has_attached_context", False)),
        authorized_paths=bool(getattr(loop.tools, "_authorized_local_paths", ())),
        image_or_artifact_request=bool(getattr(loop, "image_delivery_plan", None)),
        continuation=bool(getattr(loop, "continuation_source", None)),
    )
    board = begin_executor_run(
        root, loop, request, evidence_requirement=requirement,
    )
    loop.executor_blackboard_terminal = board
    loop.executor_result = {}
    loop.terminal_acceptance = {}
    loop.terminal_outcome = "UNKNOWN"
    recent = []
    last_fingerprint = ""
    repeated = 0
    invalids = 0
    finish_repairs = 0
    admission_repair_counts = {}
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
            field_catalog=finish_runtime_field_catalog(loop),
            evidence_requirement=requirement,
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
            # Only an incomplete Finish contract is repairable. Never re-run
            # an Action, and never let a semantic/evidence mismatch be rewritten
            # into PASS by another model attempt.
            if decision.expected_runtime and finish_repairs < 2:
                preview = compare_runtime_finish(
                    loop, root, request, decision.expected_runtime,
                    goal_checks=decision.goal_checks, allow_open=True,
                    answer=decision.message,
                )
                repairable = {
                    "MODEL_FINISH_GOAL_FACT_NOT_DECLARED",
                    "MODEL_GOAL_CHECKS_REQUIRED",
                    "MODEL_GOAL_CHECK_SCHEMA_INVALID",
                    "MODEL_GOAL_CHECK_OPERATOR_TYPE_INVALID",
                    "EXPECTED_RUNTIME_FIELDS_INVALID",
                    "EXPECTED_RUNTIME_SCHEMA_INVALID",
                    "EXPECTED_RUNTIME_FIELD_TYPE_INVALID",
                }
                if preview["reason_code"] in repairable:
                    finish_repairs += 1
                    append_executor_event(root, loop, {
                        "round": turn, "kind": "MODEL_FINISH_SCHEMA_REPAIR",
                        "reason_code": preview["reason_code"],
                        "expected_runtime_sha256": _digest(decision.expected_runtime),
                        "executed": False,
                    })
                    hint = (
                        "Finish Schema 不完整：" + preview["reason_code"]
                        + "。請僅依上一輪真實 ToolResult、原始任務與"
                        " Runtime available Finish fields 重新產生 finish，"
                        "挑選與 Goal 有關的 data 欄位及 goal_checks。"
                        " 不要重新呼叫工具，不要自行更改觀測值。"
                    )
                    continue
            append_executor_event(root, loop, {
                "round": turn, "kind": "MODEL_FINISH",
                "summary_sha256": hashlib.sha256(
                    decision.message.encode("utf-8")
                ).hexdigest(),
                "prior_tool_observation_count": len(recent),
                "expected_runtime_sha256": _digest(decision.expected_runtime),
            "goal_checks_sha256": _digest(decision.goal_checks),
            })
            return _finish(
                loop, root, request, kind="MODEL_REPORTED_COMPLETE_UNVERIFIED",
                summary=decision.message, complete=True,
                expected_runtime=decision.expected_runtime,
            goal_checks=decision.goal_checks,
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
            # This is a rejected proposal, NEVER an executed ToolResult.
            # Keep bounded typed field diagnostics for model repair and RCA.
            diagnostic = {
                "schema": str(diagnosis.get("schema") or "SMARTAGENT_ACTION_ADMISSION_DIAGNOSTIC_V1")[:100],
                "stage": str(diagnosis.get("stage") or "ADMISSION")[:60],
                "tool": decision.tool,
                "reason_code": str(rejected)[:120],
                "repair_class": str(diagnosis.get("repair_class") or "ACTION_REASSESSMENT")[:60],
                "field_issues": list(diagnosis.get("field_issues") or [])[:16],
                "executed": False,
                "authorization_granted": False,
                "scope_failure_stage": str(diagnosis.get("scope_failure_stage") or "")[:80],
                "scope_cause_digest": str(diagnosis.get("scope_cause_digest") or "")[:64],
            }
            repair_key = _digest({
                "tool": decision.tool, "reason": str(rejected),
                "issues": diagnostic["field_issues"],
            })
            admission_repair_counts[repair_key] = (
                admission_repair_counts.get(repair_key, 0) + 1
            )
            repairs_for_same_issue = admission_repair_counts[repair_key]
            tool_result = _tool_result("REJECTED", rejected)
            row = append_executor_event(root, loop, {
                "round": turn, "kind": "TOOL_RESULT",
                "toolName": decision.tool,
                "arguments": _safe_arguments(decision.arguments),
                "arguments_sha256": _digest(decision.arguments),
                "admission_diagnostic": diagnostic,
                "admission_repair_attempt": repairs_for_same_issue,
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
                "admission_diagnostic": diagnostic,
            })
            hint = (
                "Tool Admission 拒絕（沒有執行）："
                + json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":"))[:2500]
                + "。只根據 field_issues 修正 JSON 型別/必要欄位；"
                "需要權限的 Action 不能藉改名為唯讀或改 operation 繞過。"
                "修正後重新提交完整 ToolCall；不要重送既有執行成功的 Action。"
            )
            if repairs_for_same_issue >= 3:
                return _finish(
                    loop, root, request, kind="BLOCKED",
                    summary="相同 Admission 契約問題三次未解：" + str(rejected),
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
            from agent_core.tool_result_contract import encode_result
            raw_result = encode_result(
                "UNKNOWN", "TOOL_EXCEPTION_OUTCOME_UNKNOWN",
                tool=action["tool"], executed=True,
                data={"exception_class": type(exc).__name__},
            )
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
                "process": {
                    key: result_data[key] for key in (
                        "process_state", "exit_code", "timed_out",
                        "verification_status", "execution_status",
                        "expectation_status", "pid",
                    )
                    if isinstance(result_data, Mapping) and key in result_data
                },
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
