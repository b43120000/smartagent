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
            evidence_requirement=None, environment_inventory=None,
            runtime_status_catalog=None) -> str:
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
        "Admission rejection is a separate REJECTED event, never an executed "
        "ToolResult. A rejected proposal cannot prove filesystem absence. "
        "When the Admission diagnostic contains field_issues, use its "
        "actual_value and allowed_values to choose a new legal declaration; "
        "Runtime never rewrites the command or grants authority. "
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
        "Environment Inventory (Runtime-owned; recorded paths require a fresh scoped re-probe):\n"
        + json.dumps(environment_inventory or {}, ensure_ascii=False,
                     separators=(",", ":"))[:5000] + "\n"
        "If an extra tool was installed, verify its executable using an admitted "
        "absolute-path --version command; only Runtime evidence may update the "
        "shared Software Registry. Reuse existing probed environments before "
        "installing. Recorded paths never grant ACL or execution authority.\n"
        "For local Project Sync, use strategy=INDEX_ONLY (including text-only "
        "browser transport); FULL_BUNDLE and DELTA need browser upload.\n"
        "Available tools: " + json.dumps(
            catalog, ensure_ascii=False, separators=(",", ":")) + "\n"
        "Original user request:\n" + request + "\n"
        f"Round: {round_id}/{max_rounds}\n"
        "Runtime ToolResult Status Ledger (all actual results, not only recent six; "
        "rejected admissions are NOT ToolResults):\n"
        + json.dumps(runtime_status_catalog or [], ensure_ascii=False,
                     separators=(",", ":"))[:8500] + "\n"
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
    max_finish_repairs = 2
    finish_grace_rounds = 2
    finish_repair_only = False
    last_finish_reason = ""
    last_finish_diagnostic = None
    admission_repair_counts = {}
    consecutive_admission_rejections = 0
    max_consecutive_admission_rejections = 10
    hint = ""
    source = getattr(loop, "continuation_source", None) or {}
    if isinstance(source, Mapping) and source:
        # Trusted continuation context is *not* authority; only new human
        # approval can authorize an expired UNKNOWN_COMMAND proposal.
        candidates = [
            row for row in (source.get("pending_actions") or [])[:5]
            if isinstance(row, Mapping) and row.get("approval_required")
        ]
        if candidates:
            hint = (
                "接續先前尚未執行的 UNKNOWN_COMMAND 授權；"
                "若軟體層已完成前置決策，先精確驗證本次接續指令；成功執行後僅同一任務安全同類指令可以沿用；"
                "未預先批准的指令仍須重新詢問。"
                "歷史 command 摘要（不可當授權或執行證據）："
                + json.dumps(candidates, ensure_ascii=False, default=str)[:1200]
            )
    seen_mutations = set()
    pending_unknown_timeout = None
    # The Executor-only path previously skipped Planner's environment context.
    # Build one bounded, observational inventory per task; never refresh
    # executables on every model turn or trust a historic record as authority.
    try:
        from agent_core.runtime_environment import planner_environment_snapshot
        from agent_core.project_environment import planner_environment_hint
        from agent_core.runtime_software_inventory import build_runtime_software_inventory
        from agent_core.software_registry import software_registry_snapshot
        bundled = getattr(loop, "_planner_environment_snapshot", None)
        if not isinstance(bundled, Mapping) or not bundled.get("tools"):
            bundled = planner_environment_snapshot()
        project_index = planner_environment_hint(loop.workspace)
        environment_inventory = build_runtime_software_inventory(
            bundled, project_index,
        )
        environment_inventory["external_software_registry"] = software_registry_snapshot()
        # Also read a few exact user-authorized subprojects. The task's
        # workspace can be a broad parent such as E:\\, while existing
        # environment.json indexes live under E:\\project\\. Recorded
        # candidates remain non-authorizing and require a fresh probe.
        from agent_core.path_security import is_within, reject_reparse_components
        workspace_root = Path(loop.workspace).expanduser().resolve()
        known_paths = list(getattr(loop.tools, "_authorized_local_paths", []) or [])
        known_paths += list(getattr(loop, "authorized_paths", []) or [])
        scoped = []
        seen_roots = {str(workspace_root).casefold()}
        for raw in known_paths:
            if len(scoped) >= 4:
                break
            try:
                candidate = Path(str(raw)).expanduser().absolute()
                if (not candidate.is_dir()
                        or not is_within(candidate, workspace_root)):
                    continue
                reject_reparse_components(candidate, workspace_root)
                actual = candidate.resolve()
                key = str(actual).casefold()
                if key in seen_roots:
                    continue
                seen_roots.add(key)
                info = planner_environment_hint(actual)
                if info.get("index_status") == "PRESENT":
                    scoped.append({"project_root": str(actual),
                                   "candidate_index": info})
            except (OSError, ValueError, RuntimeError):
                continue
        environment_inventory["scoped_project_environment_indexes"] = scoped
    except (OSError, ValueError, RuntimeError, TypeError):
        environment_inventory = {
            "status": "INVENTORY_UNAVAILABLE", "execution_authorized": False,
        }
    max_rounds = min(int(loop.max_turns), 30)
    for turn in range(1, max_rounds + finish_grace_rounds + 1):
        # Extra turns are strictly for repairing an already submitted Finish;
        # they do not increase the number of executable ToolCall rounds.
        if turn > max_rounds and not finish_repair_only:
            break
        loop._check_cancelled()
        loop.turn_id = turn
        loop.tools._protocol_turn_seq = turn
        prompt = _prompt(
            request, loop.tools, recent, round_id=turn,
            max_rounds=max_rounds, hint=hint,
            field_catalog=finish_runtime_field_catalog(loop),
            evidence_requirement=requirement,
            environment_inventory=environment_inventory,
            runtime_status_catalog=(
                __import__("agent_core.runtime_finish_status_repair",
                           fromlist=["finish_runtime_status_catalog"])
                .finish_runtime_status_catalog(loop)
            ),
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
            if finish_repair_only:
                append_executor_event(root, loop, {
                    "round": turn, "kind": "MODEL_FINISH_CONTRACT_INVALID",
                    "reason_code": "REPAIR_DECISION_FORMAT_INVALID",
                    "error_code": exc.code, "repair_attempts": finish_repairs,
                    "executed": False,
                })
                return _finish(
                    loop, root, request, kind="FINISH_CONTRACT_INVALID",
                    summary="FAIL：Finish 修復回合輸出無效：" + exc.code,
                    complete=False,
                )
            if invalids >= 3:
                return _finish(
                    loop, root, request, kind="FORMAT_ERROR",
                    summary="模型決策格式連續三次無效：" + exc.code,
                    complete=False,
                )
            hint = "上一輪 JSON 格式無效：" + exc.code
            continue
        invalids = 0
        if decision.kind == "finish" and pending_unknown_timeout:
            # An unexecuted approval-gated action cannot be hidden by a
            # model Finish or mistaken for positive goal evidence.
            return _finish(
                loop, root, request, kind="BLOCKED",
                summary="FAIL：UNKNOWN_COMMAND_APPROVAL_TIMEOUT；尚缺執行證據。"
                        "可接續任務並重新授權，不會重播原指令。"
                        " approval_id=" + pending_unknown_timeout["approval_id"],
                complete=False,
            )
        if decision.kind == "finish":
            # Each Finish is checked against read-back Runtime evidence before
            # terminalization. Repairable schema failures get precise, bounded
            # diagnostics; the Runtime never modifies claims on the model's behalf.
            if decision.expected_runtime:
                preview = compare_runtime_finish(
                    loop, root, request, decision.expected_runtime,
                    goal_checks=decision.goal_checks, allow_open=True,
                    answer=decision.message,
                )
                from agent_core.runtime_finish_status_repair import diagnose_finish_status_mismatch
                status_repair_diagnostic = diagnose_finish_status_mismatch(preview)
                repairable = {
                    "MODEL_FINISH_GOAL_FACT_NOT_DECLARED",
                    "MODEL_GOAL_CHECKS_REQUIRED",
                    "MODEL_GOAL_CHECK_SCHEMA_INVALID",
                    "MODEL_GOAL_CHECK_OPERATOR_TYPE_INVALID",
                    "EXPECTED_RUNTIME_FIELDS_INVALID",
                    "EXPECTED_RUNTIME_SCHEMA_INVALID",
                    "EXPECTED_RUNTIME_FIELD_TYPE_INVALID",
                }
                if status_repair_diagnostic is not None:
                    repairable.add("MODEL_RUNTIME_MISMATCH")
                # Preserve Git34–35 behavior: after two repairs, missing
                # declarations still reach a formal comparator FAIL. The
                # Git42-specific schema error remains distinctly reportable.
                if (preview["reason_code"] in repairable
                        and not (finish_repairs >= max_finish_repairs
                                 and preview["reason_code"] != "MODEL_GOAL_CHECK_SCHEMA_INVALID")):
                    last_finish_reason = str(preview["reason_code"])
                    last_finish_diagnostic = status_repair_diagnostic or preview.get("finish_diagnostic")
                    if (finish_repairs >= max_finish_repairs
                            or turn >= max_rounds + finish_grace_rounds):
                        append_executor_event(root, loop, {
                            "round": turn, "kind": "MODEL_FINISH_CONTRACT_INVALID",
                            "reason_code": last_finish_reason,
                            "finish_diagnostic": last_finish_diagnostic,
                            "repair_attempts": finish_repairs,
                            "executed": False,
                        })
                        return _finish(
                            loop, root, request, kind="FINISH_CONTRACT_INVALID",
                            summary="FAIL：Finish 契約多次無效："
                                    + last_finish_reason + "；未執行新工具，證據未變。",
                            complete=False,
                        )
                    finish_repairs += 1
                    if turn >= max_rounds:
                        finish_repair_only = True
                    append_executor_event(root, loop, {
                        "round": turn, "kind": (
                            "MODEL_FINISH_STATUS_REPAIR" if status_repair_diagnostic
                            else "MODEL_FINISH_SCHEMA_REPAIR"
                        ),
                        "reason_code": last_finish_reason,
                        "finish_diagnostic": last_finish_diagnostic,
                        "repair_attempt": finish_repairs,
                        "repair_only": finish_repair_only,
                        "expected_runtime_sha256": _digest(decision.expected_runtime),
                        "executed": False,
                    })
                    diagnostic = json.dumps(
                        last_finish_diagnostic or {},
                        ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    )[:1800]
                    hint = (
                        "Finish Schema 不完整；reason_code=" + last_finish_reason
                        + "；欄位級診斷=" + diagnostic
                        + "。FINISH_STATUS_REPAIR 應按照 runtime_actual_status 修正 fields.status；"
                        "不可把 REJECTED 當 FAILED；其他錯誤只修指定的 check_index / field / constraint_code；"
                        "使用實際 Runtime field catalog 的 data.* 與真實 Evidence Reference。"
                        "不得重寫工具輸出、增加虛構 Evidence 或更改原始要求。"
                        "請提交新的 finish（而非 tool_call）。"
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
        # No ToolCall is permitted in a Finish-only grace turn. Refusing to
        # execute is essential: repairs must never create new side effects.
        if finish_repair_only and decision.kind != "blocked":
            append_executor_event(root, loop, {
                "round": turn, "kind": "MODEL_FINISH_CONTRACT_INVALID",
                "reason_code": "REPAIR_ONLY_TOOLCALL_PROHIBITED",
                "repair_attempts": finish_repairs, "executed": False,
            })
            return _finish(
                loop, root, request, kind="FINISH_CONTRACT_INVALID",
                summary="FAIL：Finish 修復僅允許 finish 或 blocked，不得再執行工具。",
                complete=False,
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
        unknown_approval = None
        risk_class = ""
        turbo = None
        if decision.tool == "run_command":
            # Persist EVERY Turbo decision, including disabled/mismatched
            # Workspace and pre-execution Admission rejections. Never only
            # record successful auto approvals: Planner and RCA require cause.
            from agent_core.command_risk import classify_command
            from agent_core.turbo_execution_policy import evaluate_turbo_confirmation
            if action:
                risk_class = classify_command(action)
                turbo = evaluate_turbo_confirmation(loop, action)
            else:
                risk_class = "NOT_ADMITTED"
                turbo = {
                    "schema": "SMARTAGENT_TURBO_DECISION_V2",
                    "auto_confirm": False,
                    "reason_code": "PRIOR_ACTION_ADMISSION_REJECTED",
                    "policy_revision": 0,
                    "turbo_root": "",
                    "workspace_root": str(loop.workspace),
                    "confirmation_scope": "SECONDARY_UNKNOWN_ONLY",
                    "authorization_granted": False,
                }
            auto_confirm = bool(
                action and risk_class == "UNKNOWN"
                and turbo.get("auto_confirm") is True
            )
            decision_reason = (
                str(turbo.get("reason_code") or "TURBO_REASON_MISSING")
                if risk_class == "UNKNOWN" or not action
                else "TURBO_SECOND_GATE_NOT_REQUIRED_" + risk_class
            )
            append_executor_event(root, loop, {
                "round": turn, "kind": "TURBO_DECISION",
                "schema": "SMARTAGENT_TURBO_DECISION_V2",
                "toolName": "run_command",
                "action_id": str((action or {}).get("action_id") or ""),
                "arguments_sha256": _digest(decision.arguments),
                "command_sha256": hashlib.sha256(
                    str((decision.arguments or {}).get("command") or "")
                    .encode("utf-8")
                ).hexdigest(),
                "risk_class": risk_class,
                "admission_status": "ADMITTED" if action else "REJECTED_BEFORE_EXECUTION",
                "admission_reason_code": str(rejected or "")[:128],
                "auto_confirm": auto_confirm,
                "turbo_policy_qualified": bool(turbo.get("auto_confirm")),
                "decision": (
                    "AUTO_CONFIRM" if auto_confirm
                    else "NORMAL_ADMISSION" if action and risk_class != "UNKNOWN"
                    else "HUMAN_CONFIRMATION_REQUIRED" if action
                    else "ADMISSION_REJECTED"
                ),
                "reason_code": decision_reason,
                "workspace_root": str(loop.workspace),
                "working_directory": str(
                    (action or decision.arguments or {}).get("working_directory")
                    or loop.workspace
                )[:1024],
                "turbo_root": str(turbo.get("turbo_root") or ""),
                "policy_revision": int(turbo.get("policy_revision") or 0),
                "confirmation_scope": "SECONDARY_UNKNOWN_ONLY",
                "executed": False, "authorization_granted": False,
            })
        if action and decision.tool == "run_command":
            if auto_confirm:
                append_executor_event(root, loop, {
                    "round": turn, "kind": "TURBO_AUTO_ADMITTED",
                    "toolName": "run_command", "action_id": action["action_id"],
                    "policy_revision": turbo["policy_revision"],
                    "reason_code": turbo["reason_code"],
                    "turbo_root": turbo["turbo_root"],
                    "risk_class": risk_class,
                    "arguments_sha256": _digest(decision.arguments),
                    "executed": False, "authorization_granted": False,
                })
            elif risk_class == "UNKNOWN":
                if pending_unknown_timeout:
                    return _finish(
                        loop, root, request, kind="BLOCKED",
                        summary="FAIL：UNKNOWN_COMMAND_APPROVAL_TIMEOUT；替代指令"
                                "仍無法在既有權限下安全下發。接續任務需重新授權，"
                                "不會自動執行過期指令。",
                        complete=False,
                    )
                from agent_core.unknown_command_approval import (
                    request_unknown_command_approval,
                )
                from agent_core.resume_command_approval import ResumeCommandGate
                from agent_core.command_family_approval import TaskCommandFamilyGrants
                family_gate = TaskCommandFamilyGrants(root)
                family_match = family_gate.match(loop=loop, action=action)
                preapproval = (
                    None if family_match is not None
                    else ResumeCommandGate(root).consume_for_action(loop=loop, action=action)
                )
                append_executor_event(root, loop, {
                    "round": turn, "kind": (
                        "HUMAN_APPROVAL_REUSED" if family_match is not None
                        else "HUMAN_APPROVAL_REQUESTED"
                    ),
                    "toolName": "run_command",
                    "action_id": action["action_id"],
                    "arguments_sha256": _digest(decision.arguments),
                    "risk_class": "UNKNOWN", "executed": False,
                })
                approval = (
                    family_match if family_match is not None
                    else preapproval if preapproval is not None
                    else request_unknown_command_approval(loop, action)
                )
                unknown_approval = approval
                append_executor_event(root, loop, {
                    "round": turn, "kind": "HUMAN_APPROVAL_DECIDED",
                    "toolName": "run_command",
                    "action_id": action["action_id"],
                    "state": approval["state"],
                    "reason_code": approval["reason"],
                    "approval_id": approval["approval_id"],
                    "command_sha256": approval["command_sha256"],
                    "executed": False, "evidence_ref": "",
                })
                if approval["state"] != "APPROVED":
                    recent.append({
                        "round": turn, "tool": "run_command",
                        "admission_verdict": "HUMAN_APPROVAL_" + approval["state"],
                        "approval_id": approval["approval_id"],
                        "risk_class": "UNKNOWN",
                        "executed": False,
                    })
                    if approval["state"] == "DENIED":
                        return _finish(
                            loop, root, request, kind="BLOCKED",
                            summary="FAIL：使用者拒絕這條未知命令："
                                    + approval["reason"] + "。接續任務須重新授權。",
                            complete=False,
                        )
                    pending_unknown_timeout = approval
                    if getattr(loop, "continuation_store", None):
                        # Save exact command only in protected approval state.
                        # The general continuation/Blackboard stores a digest
                        # rather than sensitive command text.
                        ResumeCommandGate(root).remember(
                            loop=loop, action=action,
                            old_approval_id=approval["approval_id"],
                        )
                        # Persist only non-executed metadata, never the raw
                        # command or an authorizing permit. A resumed model
                        # re-proposes an Action and obtains a new approval.
                        loop.continuation_store.pending_batch(loop.task_id, [{
                            "tool": "run_command",
                            "action_id": action["action_id"],
                            "approval_required": True,
                            "approval_kind": "UNKNOWN_COMMAND",
                            "approval_id": approval["approval_id"],
                            "command_sha256": approval["command_sha256"],
                            "operation": str(action.get("operation") or ""),
                            "executed": False,
                        }])
                    hint = (
                        "未知風險命令尚未執行，使用者授權逾時或不可用（"
                        + approval["reason"]
                        + "）；這份 Approval 無法重用。"
                        "可另選一個已明確授權、並通過安全檢查的替代工具或命令。"
                        "若無法取得相同完成條件的證據，回傳 blocked；"
                        "可在新接續任務重新發起一次性授權。"
                    )
                    continue
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
        if action is None and pending_unknown_timeout:
            return _finish(
                loop, root, request, kind="BLOCKED",
                summary="FAIL：UNKNOWN_COMMAND_APPROVAL_TIMEOUT；替代 Action"
                        " 也未通過 Admission：" + str(rejected)
                        + "。接續任務可重新發起精確命令授權。",
                complete=False,
            )
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
            # Scope by literal command for run_command. Distinct commands
            # must not share the same issue counter. A separate consecutive
            # bound prevents endless command changes from evading repair.
            proposal_identity = (
                str((decision.arguments or {}).get("command", ""))
                if decision.tool == "run_command"
                else _digest(decision.arguments)
            )
            repair_key = _digest({
                "tool": decision.tool,
                "proposal_identity_sha256": hashlib.sha256(
                    proposal_identity.encode("utf-8")
                ).hexdigest(),
                "reason": str(rejected),
                "issue_fields": [
                    {"field": item.get("field"), "reason": item.get("reason"),
                     "constraint_code": item.get("constraint_code", "")}
                    for item in diagnostic["field_issues"] if isinstance(item, Mapping)
                ],
            })
            admission_repair_counts[repair_key] = (
                admission_repair_counts.get(repair_key, 0) + 1
            )
            repairs_for_same_issue = admission_repair_counts[repair_key]
            consecutive_admission_rejections += 1
            row = append_executor_event(root, loop, {
                "round": turn, "kind": "ADMISSION_REJECTED",
                "toolName": decision.tool,
                "arguments": _safe_arguments(decision.arguments),
                "arguments_sha256": _digest(decision.arguments),
                "admission_diagnostic": diagnostic,
                "admission_repair_attempt": repairs_for_same_issue,
                "admission_rejection_streak": consecutive_admission_rejections,
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
                "admission_verdict": "REJECTED_BEFORE_EXECUTION",
                "admission_diagnostic": diagnostic,
            })
            hint = (
                "Tool Admission 拒絕（沒有執行）："
                + json.dumps(diagnostic, ensure_ascii=False, separators=(",", ":"))[:2500]
                + "。依 field_issues 修正 JSON 型別、合法 Enum 或契約不一致；"
                "實際 command 和 operation 的語意仍由模型判斷。"
                "需要權限的 Action 不能藉改名為唯讀或改 operation 繞過。"
                "修正後重新提交完整 ToolCall；不要重送既有執行成功的 Action。"
            )
            if (repairs_for_same_issue >= 3
                    or consecutive_admission_rejections >= max_consecutive_admission_rejections):
                budget_reason = (
                    "同一個 Action 契約問題三次未解："
                    if repairs_for_same_issue >= 3 else
                    "連續 Admission 拒絕達上限："
                )
                return _finish(
                    loop, root, request, kind="BLOCKED",
                    summary=budget_reason + str(rejected),
                    complete=False,
                )
            continue

        # An admitted action is independent progress. Its actual ToolResult
        # will determine what to do next; no prior rejection grants proof.
        consecutive_admission_rejections = 0
        admission_repair_counts.clear()
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
            "turbo_decision": (
                "NOT_APPLICABLE_TYPED_ACTION" if action["tool"] != "run_command"
                else ("AUTO_CONFIRM" if auto_confirm
                      else "NORMAL_ADMISSION" if risk_class != "UNKNOWN"
                      else "HUMAN_CONFIRMATION_REQUIRED")
            ),
            "command_risk_class": risk_class,
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
        after_approval_timeout = bool(pending_unknown_timeout)
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
                "turbo_auto_confirm": bool(auto_confirm),
                "turbo_decision_reason_code": (
                    str((turbo or {}).get("reason_code") or "")[:120]
                    if action["tool"] == "run_command" else "NOT_APPLICABLE_TYPED_ACTION"
                ),
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
        # Publish only real successful executable version probes; an install
        # command's success alone never proves an environment is usable.
        try:
            from agent_core.software_registry import register_verified_software_probe
            registered_software = register_verified_software_probe(
                action, receipt, evidence_ref,
            )
            if registered_software is not None:
                append_executor_event(root, loop, {
                    "round": turn, "kind": "SOFTWARE_REGISTRY_UPDATED",
                    "toolName": action["tool"], "action_id": action["action_id"],
                    "evidence_ref": evidence_ref,
                    "software": registered_software["name"],
                    "executed": False, "authorization_granted": False,
                })
                environment_inventory["external_software_registry"] = (
                    __import__(
                        "agent_core.software_registry",
                        fromlist=["software_registry_snapshot"],
                    ).software_registry_snapshot()
                )
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            append_executor_event(root, loop, {
                "round": turn, "kind": "SOFTWARE_REGISTRY_WRITE_FAILED",
                "error_class": type(exc).__name__,
                "evidence_ref": evidence_ref, "executed": False,
            })
        if (unknown_approval is not None
                and unknown_approval.get("state") == "APPROVED"
                and unknown_approval.get("reason") != "same_task_command_family_reused"
                and runtime_status == "SUCCEEDED"
                and receipt.get("executed") is True):
            from agent_core.command_family_approval import TaskCommandFamilyGrants
            if TaskCommandFamilyGrants(root).remember(
                    loop=loop, action=action,
                    approval_id=unknown_approval["approval_id"],
                    evidence_ref=evidence_ref):
                append_executor_event(root, loop, {
                    "round": turn, "kind": "COMMAND_FAMILY_GRANT_RECORDED",
                    "toolName": "run_command", "action_id": action["action_id"],
                    "approval_id": unknown_approval["approval_id"],
                    "evidence_ref": evidence_ref, "executed": False,
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
        if after_approval_timeout:
            if runtime_status == "SUCCEEDED" and receipt.get("executed") is True:
                pending_unknown_timeout = None
                if getattr(loop, "continuation_store", None):
                    loop.continuation_store.pending_batch(loop.task_id, [])
            elif runtime_status not in {"UNKNOWN", "RUNNING", "PARTIAL"}:
                return _finish(
                    loop, root, request, kind="BLOCKED",
                    summary="FAIL：UNKNOWN_COMMAND_APPROVAL_TIMEOUT；"
                            "替代 Action 執行或驗證失敗：" + runtime_reason
                            + "。接續任務可重新發起精確命令授權。",
                    complete=False,
                )
        if runtime_status in {"UNKNOWN", "RUNNING", "PARTIAL"} and mutating:
            return _finish(
                loop, root, request, kind="EXECUTION_OUTCOME_UNKNOWN",
                summary="可能產生副作用的工具結果未確定，禁止重送：" + runtime_reason,
                complete=False,
            )
    if finish_repair_only:
        append_executor_event(root, loop, {
            "round": loop.turn_id, "kind": "MODEL_FINISH_CONTRACT_INVALID",
            "reason_code": last_finish_reason or "FINISH_REPAIR_BUDGET_EXHAUSTED",
            "finish_diagnostic": last_finish_diagnostic,
            "repair_attempts": finish_repairs, "executed": False,
        })
        return _finish(
            loop, root, request, kind="FINISH_CONTRACT_INVALID",
            summary="FAIL：Finish 修復預算已耗盡，未通過欄位契約驗證："
                    + (last_finish_reason or "FINISH_REPAIR_BUDGET_EXHAUSTED"),
            complete=False,
        )
    return _finish(
        loop, root, request, kind="MAX_ROUNDS",
        summary="PokeClaw Executor 已達決策回合上限，尚未收到 Finish。",
        complete=False,
    )


__all__ = ["run_pokeclaw_executor"]
