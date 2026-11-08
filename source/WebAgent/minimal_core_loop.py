"""PokeClaw-style primary loop with SmartAgent-owned runtime records and tools.

The model sends one small JSON decision. Runtime controls identity, tool
admission, execution, evidence, progress events, and bounded observations.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time

from .minimal_admission import admit_minimal_action, action_is_mutating
from .minimal_decision import MinimalDecisionError, parse_minimal_decision_with_diagnostics
from agent_core.tri_one_runtime_terminal import (
    GoalEvidence, evaluate_goal_evidence, accept_runtime_terminal,
    format_runtime_goal_summary, accept_runtime_answer_only,
)
from .minimal_goal_driver import (
    build_goal_driver_context, compact_goal_driver_context,
    gate_known_absent_directory, classify_rejection,
)


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class MinimalCoreJournal:
    """Append-only task receipts with no raw prompts, commands or tool output."""
    def __init__(self, base: Path, task_id: str):
        name = re.sub(r"[^A-Za-z0-9_-]", "_", task_id)[:110]
        self.path = base / "localdata" / "runtime" / "minimal_core" / (name + ".jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **fields) -> None:
        payload = {"schema": "SMARTAGENT_MINIMAL_CORE_JOURNAL_V1",
                   "time_unix": time.time(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def tool_catalog(context) -> list[dict]:
    from agent_core.tool_capabilities import get_allowed_tools
    from agent_core.smartagent_protocol import TOOL_ENVELOPE_SCHEMAS
    names = sorted(get_allowed_tools(context.interface_name))
    transfer = context.web_file_capabilities()
    if not transfer.get("browser_upload", False):
        names = [name for name in names if name not in {
            "upload_file", "upload_files", "project_sync"}]
    if not transfer.get("browser_download", False):
        names = [name for name in names if name != "download_artifact"]
    return [{
        "name": name,
        "required": [k for k in TOOL_ENVELOPE_SCHEMAS[name]["required"]
                     if k != "action_id"],
        "optional": [k for k in TOOL_ENVELOPE_SCHEMAS[name]["optional"]
                     if k != "action_id"],
    } for name in names]


def recent_results(history: list[dict]) -> list[dict]:
    result = []
    for entry in history[-4:]:
        receipt = entry["receipt"]
        data = receipt.get("data") if isinstance(receipt.get("data"), dict) else {}
        encoded_data = json.dumps(data, ensure_ascii=False, default=str)
        result.append({
            "tool": entry["action"]["tool"], "action_id": entry["action"]["action_id"],
            "status": receipt.get("status", "UNKNOWN"),
            "reason": receipt.get("reason", "UNPARSEABLE"),
            "executed": receipt.get("executed", False),
            "evidence_ref": entry["evidence_ref"],
            "data_preview": encoded_data[:4500],
            "preview_truncated": len(encoded_data) > 4500,
        })
    return result


def build_minimal_prompt(request: str, context, history: list[dict],
                         *, round_id: int, max_turns: int, repair_hint: str = "",
                         goal_driver_context: str = "") -> str:
    return (
        "[SMARTAGENT_MINIMAL_CORE_V1]\n"
        "PRIMARY DECISION PROTOCOL FOR THIS REQUEST. This is NOT a legacy v9 "
        "tool transport turn. Respond with exactly ONE compact JSON object. "
        "Do not output report_progress, turn_commit, smartagent_tool fences, "
        "extra prose, request IDs, evidence refs or progress bookkeeping. "
        "The local Runtime owns all of those.\n"
        'Action: {"type":"tool_call","name":"list_directory","arguments":{"path":"."}}\n'
        'Finish: {"type":"finish","summary":"..."}\n'
        'Blocked: {"type":"blocked","reason":"..."}\n'
        "Choose ONE tool per round. Tool names and fields are canonical; "
        "all paths must be inside authorized scope. JSON Windows backslashes "
        "must be escaped. The Runtime will execute only admitted actions. "
        "Never invent observations or claim an unverified success. If a tool observation preview is truncated, do not claim to have seen the entire file or listing. "
        "Windows shell for run_command is PowerShell 5.1 (cmd /c invokes CMD). "
        "For run_command provide command and operation. Use only the canonical "
        "operation values INSPECT, MUTATE, BUILD, TEST, VERIFY, GIT_INSPECT, "
        "GIT_MUTATE, PROCESS, TRANSFER, GENERAL. READ/READ_ONLY/LIST are invalid. "
        "For registered remote source probes use GIT_INSPECT from an existing "
        "authorized workspace; never use a missing deployment target as CWD. "
        "Do not inspect broader unrequested project/workspace scope. "
        "For commands that "
        "are not provably read-only also provide nonempty success_criteria "
        "and verify checks. Unknown execution outcome is NOT safe to replay.\n"
        "Workspace: " + str(context.workspace_root) + "\n"
        "Available tools: " + json.dumps(tool_catalog(context), separators=(",", ":")) + "\n"
        "Original goal: " + request + "\n"
        "Runtime Goal-Driven Blackboard (read-only, never permission): "
        + goal_driver_context + "\n"
        "If this is a no-tool TEXT_ONLY request, output Finish with your "
        "actual answer. Runtime admits it without Action evidence only when "
        "the trusted original request is answer-only and no tool was executed. "
        "Otherwise use Goal Gaps, confirmed Prerequisites, Action Readiness "
        "and failure causes to replan one admitted action at a time. "
        "NEVER infer missing files from failed or denied tools. "
        "A suggested action is not scope authorization.\n"
        "Round: " + str(round_id) + "/" + str(max_turns) + "\n"
        "Observed tool results: " +
        json.dumps(recent_results(history), ensure_ascii=False, separators=(",", ":")) + "\n"
        + ("Last decision rejected before execution: " + repair_hint + "\n"
           if repair_hint else "")
        + "[/SMARTAGENT_MINIMAL_CORE_V1]"
    )


def narrow_goal_verified(goal: str, history: list[dict]) -> bool:
    """No broad semantic inference from a model finish or mere exit code."""
    if evaluate_goal_evidence(goal, history).verified:
        return True
    # Do not let one successful listing finish a multi-file deployment Goal.
    from agent_core.target_path_contract import extract_target_contract
    target_contract = extract_target_contract(goal)
    if target_contract and target_contract.get("required_top_level_entries"):
        return False
    if len(history) != 1:
        return False
    entry = history[0]
    action, receipt = entry["action"], entry["receipt"]
    if receipt.get("status") != "SUCCEEDED" or receipt.get("executed") is not True:
        return False
    data = receipt.get("data") if isinstance(receipt.get("data"), dict) else {}
    tool = action["tool"]
    if tool in {"list_directory", "read_file"}:
        if len(json.dumps(data, ensure_ascii=False, default=str)) > 4500:
            return False
    if tool == "run_command":
        from agent_core.action_dependencies import command_is_proven_observation
        command = str(action.get("command") or "")
        operation = str(action.get("operation") or "").upper()
        if not command or command.lower() not in goal.lower():
            return False
        if data.get("exit_code") != 0 or data.get("timed_out"):
            return False
        if not data.get("terminal_eligible"):
            return False
        if operation in {"INSPECT", "GIT_INSPECT"}:
            return command_is_proven_observation(action)
        if operation in {"BUILD", "TEST"}:
            return str(data.get("verification_status") or "").upper() == "PASS"
        # Mutation correctness is never inferred from only one command result.
        return False
    path = str(action.get("path") or "")
    if not path or path.lower() not in goal.lower():
        return False
    if tool == "list_directory":
        return bool(data.get("terminal_eligible") is True and re.search(
            r"列出|清單|檔案|資料夾|list|files|directory", goal, re.I))
    if tool == "read_file":
        return bool(data.get("terminal_eligible") is True and re.search(
            r"讀取|查看|顯示|read|show|cat", goal, re.I))
    if tool == "create_directory":
        # Runtime's strict GoalEvidence evaluator already handles mkdir.
        # Never promote a larger multi-step goal because a single mkdir passed.
        return False
    return False


def _verified_proof(goal: str, history: list[dict], loop=None) -> GoalEvidence:
    """Runtime-owned proof: preserve simple goals, fail closed on deployments."""
    if loop is not None:
        from .minimal_deployment_terminal import evaluate_deployment_goal
        deployment = evaluate_deployment_goal(loop, goal, history)
        if deployment is not None:
            return deployment
    proof = evaluate_goal_evidence(goal, history)
    if proof.verified:
        return proof
    if len(history) == 1 and narrow_goal_verified(goal, history):
        entry = history[0]
        action = entry["action"]
        ref = str(action.get("action_id") or "")
        if ref:
            return GoalEvidence(
                "PASS", "SINGLE_OPERATION_RESULT_VERIFIED", (ref,),
                str(action.get("path") or action.get("command") or ""),
                "SINGLE_OBSERVATION",
            )
    return proof


def _accept_and_record_goal(loop, proof: GoalEvidence, journal, *, round_id: int):
    receipt = accept_runtime_terminal(loop, proof)
    journal.append("runtime_goal_verified", round=round_id,
                   evidence_refs=list(proof.evidence_refs),
                   goal_kind=proof.goal_kind, receipt_id=receipt["receipt_id"])
    loop._emit("runtime_goal_verified", request_id=loop.run_id,
               task_id=loop.task_id, round=round_id,
               outcome="SUCCESS", evidence_refs=list(proof.evidence_refs),
               receipt_id=receipt["receipt_id"], goal_kind=proof.goal_kind)
    loop._emit("runtime_terminal_accepted", **receipt)
    return receipt


def _format_repair_hint(error: MinimalDecisionError) -> str:
    """Return bounded, actionable format guidance; no raw rejected model text."""
    code = error.code
    sample = json.dumps({
        "type": "tool_call", "name": "create_directory",
        "arguments": {"path": r"E:\ExampleFolder"},
    }, ensure_ascii=False, separators=(",", ":"))
    if code == "json_invalid_escape":
        return ("Invalid JSON escape in previous response. Escape Windows path "
                "backslashes by writing TWO backslashes for each path separator. "
                "Valid example: " + sample)
    if code in {"json_syntax_invalid", "json_control_character",
                "json_unterminated_string"}:
        loc = error.diagnostic.get("json_error_line")
        col = error.diagnostic.get("json_error_column")
        return (f"JSON syntax invalid at line {loc}, column {col}. "
                "Return exactly ONE complete JSON object, no commentary.")
    if code in {"legacy_protocol_response", "json_object_not_found",
                "decision_not_found", "decision_type_required"}:
        return ("Do NOT use the older smartagent_tool v9 format. "
                "Use the v10 one-object decision contract with type=tool_call, "
                "name and arguments. No progress, turn_commit or prose. "
                "One canonical example: " + sample)
    if code.startswith("ambiguous_"):
        return ("Multiple JSON objects were detected. Return exactly ONE actual "
                "decision. Do not quote examples or include explanations.")
    if code in {"tool_call_fields_invalid", "tool_name_or_arguments_invalid",
                "conflicting_alias_fields", "decision_required_field_missing"}:
        return ("Tool decision fields invalid: emit one JSON object with "
                "type=tool_call, name=<canonical tool>, arguments=<object>. "
                "Do not include runtime-owned fields.")
    return ("Previous response rejected: " + code +
            ". Return ONE minimal decision JSON object only.")


def _decision_event_fields(meta: dict) -> dict:
    """Primitive-only, allowlisted telemetry suitable for sanitized RCA."""
    return {
        "response_sha256": str(meta.get("response_sha256", "")),
        "response_chars": int(meta.get("response_chars", 0)),
        "candidate_count": int(meta.get("candidate_count", 0)),
        "valid_decision_count": int(meta.get("valid_decision_count", 0)),
        "normalization": str(meta.get("normalization", "NONE")),
        "source_kind": str(meta.get("source_kind", "UNKNOWN")),
        "json_error_message_code": str(meta.get("json_error_message_code", "")),
        "json_error_line": int(meta.get("json_error_line", 0)),
        "json_error_column": int(meta.get("json_error_column", 0)),
        "legacy_fence_detected": bool(meta.get("legacy_fence_detected", False)),
        "fence_detected": bool(meta.get("fence_detected", False)),
    }


def run_minimal_core(loop, request: str) -> str:
    """Use existing WebAgentToolContext, action ledger and security dispatcher."""
    from agent_core.tool_result_contract import decode_result
    from agent_core.task_progress import set_runtime_state

    root = Path(loop.progress_root or loop.workspace)
    journal = MinimalCoreJournal(root, loop.task_id)
    journal.append("task_started", request_id=loop.run_id,
                   task_id=loop.task_id, task_epoch=loop.task_epoch,
                   goal_sha256=hashlib.sha256(request.encode("utf-8")).hexdigest())
    history = []
    seen_mutations = set()
    last_action_digest = ""
    repeated = 0
    invalids = 0
    repair_hint = ""
    action_rejections = []
    consecutive_action_rejections = 0
    consecutive_unverified_finishes = 0

    for round_id in range(1, loop.max_turns + 1):
        loop._check_cancelled()
        loop.turn_id = round_id
        loop.tools._protocol_turn_seq = round_id
        goal_context = build_goal_driver_context(
            loop, request, history, action_rejections)
        goal_context_text = compact_goal_driver_context(goal_context)
        context_digest = hashlib.sha256(goal_context_text.encode("utf-8")).hexdigest()
        journal.append("goal_driver_context_ready", round=round_id,
                       snapshot_source=goal_context.get("snapshot_source"),
                       next_category=goal_context.get("next_category"),
                       active_goal_gap_count=goal_context.get("goal_gap_count", 0),
                       context_sha256=context_digest)
        loop._emit("minimal_goal_driver_context", request_id=loop.run_id,
                   task_id=loop.task_id, round=round_id,
                   snapshot_source=goal_context.get("snapshot_source"),
                   next_category=goal_context.get("next_category"),
                   active_goal_gap_count=goal_context.get("goal_gap_count", 0),
                   prerequisite_transition_stage=goal_context.get("prerequisite_transition_stage"),
                   terminal_readiness_status=goal_context.get("terminal_readiness_status"),
                   source_requirement=goal_context.get("source_requirement"),
                   context_sha256=context_digest)
        prompt = build_minimal_prompt(request, loop.tools, history,
                                      round_id=round_id, max_turns=loop.max_turns,
                                      repair_hint=repair_hint,
                                      goal_driver_context=goal_context_text)
        attachments = loop.tools.take_pending_attachments()
        loop.protocol_state = "DECIDING"
        try:
            response = loop.planner(prompt, None, attachments)
        except BaseException as exc:
            # Submit may have happened even when DOM transport raises.
            journal.append("transport_interrupted", round=round_id,
                           error_type=type(exc).__name__)
            loop.protocol_state = "INTERRUPTED"
            raise
        try:
            decision, decision_meta = parse_minimal_decision_with_diagnostics(
                response, original_goal=request)
        except MinimalDecisionError as exc:
            invalids += 1
            meta = exc.diagnostic
            repair_hint = _format_repair_hint(exc)
            trace_fields = _decision_event_fields(meta)
            journal.append("decision_rejected", round=round_id,
                           reason=exc.code, **trace_fields,
                           response_structure_redacted=meta.get("response_structure_redacted", ""))
            loop._emit("minimal_decision_rejected",
                       request_id=loop.run_id, task_id=loop.task_id,
                       round=round_id, diagnostic_reason=exc.code,
                       **trace_fields)
            if invalids >= 2:
                loop.protocol_state = "INTERRUPTED"
                raise RuntimeError("MINIMAL_DECISION_FORMAT_FAILED:" + exc.code)
            continue
        journal.append("decision_accepted", round=round_id,
                       decision_type=decision.kind,
                       **_decision_event_fields(decision_meta))
        loop._emit("minimal_decision_accepted", request_id=loop.run_id,
                   task_id=loop.task_id, round=round_id,
                   decision_type=decision.kind,
                   **_decision_event_fields(decision_meta))
        invalids = 0
        repair_hint = ""
        if decision.kind == "tool_call":
            from agent_core.command_operation import COMMAND_OPERATIONS
            raw_operation = str((decision.arguments or {}).get("operation") or "")
            safe_operation = (raw_operation[:40].upper()
                              if re.fullmatch(r"[A-Za-z_]{1,40}", raw_operation)
                              else "INVALID_OR_MISSING")
            proposal_fields = sorted(
                key for key in (decision.arguments or {})
                if isinstance(key, str) and re.fullmatch(r"[a-z_]{1,40}", key)
            )[:24]
            proposal_hash = _digest(decision.arguments)
            journal.append("action_proposed", round=round_id, tool=decision.tool,
                           proposed_operation=safe_operation,
                           proposed_fields=proposal_fields,
                           proposal_sha256=proposal_hash)
            loop._emit("minimal_action_proposed",
                       request_id=loop.run_id, task_id=loop.task_id,
                       task_epoch=loop.task_epoch, round=round_id,
                       tool=decision.tool, operation=safe_operation,
                       operation_valid=(safe_operation in COMMAND_OPERATIONS
                                        if decision.tool == "run_command" else True),
                       proposed_fields=proposal_fields,
                       proposal_sha256=proposal_hash, executed=False)

        if decision.kind == "blocked":
            loop.protocol_state = "BLOCKED"
            loop.terminal_outcome = "UNKNOWN"
            loop.terminal_acceptance = {}  # PAUSED is nonterminal, no accepted receipt.
            journal.append("model_blocked", round=round_id,
                           reason_sha256=_digest(decision.message))
            set_runtime_state(loop.task_id, "PAUSED", reason="MINIMAL_MODEL_BLOCKED",
                              root=loop.progress_root)
            return "目前無法繼續（模型尚未證明任務完成）：" + decision.message

        if decision.kind == "finish":
            # Text-only is a distinct Runtime terminal class; no fake
            # ActionResult or filesystem evidence is created for prose.
            if not history:
                answer_receipt, answer_reason = accept_runtime_answer_only(
                    loop, goal=request, answer=decision.message,
                    had_attachments=bool(attachments),
                )
                if answer_receipt:
                    journal.append("runtime_text_only_admitted", round=round_id,
                                   receipt_id=answer_receipt["receipt_id"],
                                   answer_sha256=answer_receipt["answer_sha256"])
                    loop._emit("runtime_text_only_admitted",
                               request_id=loop.run_id, task_id=loop.task_id,
                               round=round_id, receipt_id=answer_receipt["receipt_id"],
                               completion_kind="ANSWER_ONLY")
                    loop._emit("runtime_terminal_accepted", **answer_receipt)
                    set_runtime_state(loop.task_id, "COMPLETED", root=loop.progress_root)
                    return decision.message
                journal.append("runtime_text_only_ineligible", round=round_id,
                               reason=answer_reason)
            proof = _verified_proof(request, history, loop)
            verified = proof.verified
            if verified:
                receipt = _accept_and_record_goal(loop, proof, journal,
                                                 round_id=round_id)
                set_runtime_state(loop.task_id, "COMPLETED", root=loop.progress_root)
                summary = (format_runtime_goal_summary(proof)
                           if proof.goal_kind in {
                               "SINGLE_CREATE_DIRECTORY", "CONDITIONAL_DIRECTORY_EXISTS",
                               "DEPLOYMENT_CONTENT_VERIFIED",
                           } else decision.message)
                journal.append("model_finish", round=round_id,
                               outcome="SUCCESS", verified=True,
                               evidence_refs=list(proof.evidence_refs),
                               receipt_id=receipt["receipt_id"],
                               summary_sha256=_digest(summary))
                return summary
            # Goal-Driven repair is allowed only when evidence/known gaps
            # indicate a legitimate further operation, not for unknown prose.
            pending_gaps = list(goal_context.get("active_goal_gaps") or [])
            can_replan = (
                goal_context.get("request_route") != "ANSWER_ONLY"
                and bool(history) and bool(pending_gaps)
                and consecutive_unverified_finishes < 1
            )
            journal.append("model_finish_rejected", round=round_id,
                           outcome="UNKNOWN", reason=proof.reason,
                           pending_gap_count=len(pending_gaps))
            if can_replan:
                consecutive_unverified_finishes += 1
                repair_hint = (
                    "Finish rejected by Runtime: Goal gaps remain. "
                    "Use the current Blackboard prerequisites and admitted "
                    "Tool actions to close them. Do NOT claim success or "
                    "repeat a denied action without changed evidence."
                )
                journal.append("goal_driven_replan_requested", round=round_id,
                               reason="GOAL_GAPS_UNRESOLVED",
                               gap_count=len(pending_gaps))
                continue
            loop.terminal_outcome = "UNKNOWN"
            loop.protocol_state = "MODEL_FINISHED_UNVERIFIED"
            loop.terminal_acceptance = {}
            journal.append("model_finish", round=round_id,
                           outcome="UNKNOWN", verified=False,
                           summary_sha256=_digest(decision.message),
                           reason=proof.reason)
            set_runtime_state(loop.task_id, "PAUSED",
                              reason="MODEL_FINISHED_UNVERIFIED",
                              root=loop.progress_root)
            return ("模型已回覆，但 Runtime 尚無足夠證據確認整體目標完成：\n"
                    + decision.message)

        # No operational ToolCall can be inferred from an answer-only goal.
        # Text-only content delivery is not a Scope/Write authorization.
        if goal_context.get("request_route") == "ANSWER_ONLY":
            journal.append("answer_only_tool_rejected", round=round_id,
                           tool=decision.tool,
                           reason="ANSWER_ONLY_OPERATION_NOT_REQUESTED",
                           executed=False)
            loop._emit("minimal_goal_action_rejected",
                       request_id=loop.run_id, task_id=loop.task_id,
                       round=round_id, tool=decision.tool,
                       reason_code="ANSWER_ONLY_OPERATION_NOT_REQUESTED",
                       repair_mode="TEXT_ONLY_FINISH", executed=False)
            consecutive_action_rejections += 1
            repair_hint = (
                "This original user goal is text-only. Do not call filesystem, "
                "shell, Git, web or external tools. Return ONE finish JSON "
                "containing only the requested text answer."
            )
            if consecutive_action_rejections >= 2:
                loop.protocol_state = "BLOCKED"
                set_runtime_state(loop.task_id, "PAUSED",
                                  reason="ANSWER_ONLY_REPEATED_TOOL_PROPOSAL",
                                  root=loop.progress_root)
                return "純文字任務收到未授權的工具提案，未執行任何工具；任務暫停。"
            continue

        action, rejection = admit_minimal_action(decision, loop.tools)
        if rejection:
            consecutive_action_rejections += 1
            item = classify_rejection(decision.tool, rejection, round_id)
            action_rejections.append(item)
            repair_hint = (
                "Runtime Action Admission denied: " + rejection +
                ". Repair the schema or select an evidence-grounded "
                "prerequisite from Goal-Driven Blackboard. "
                "Denied execution does not prove filesystem absence."
            )
            if rejection == "run_command_operation_invalid":
                repair_hint += (
                    " Operation MUST be one of INSPECT, MUTATE, BUILD, TEST, "
                    "VERIFY, GIT_INSPECT, GIT_MUTATE, PROCESS, TRANSFER, "
                    "GENERAL. READ/READ_ONLY/LIST are not valid operation "
                    "values. Retain actual command semantics."
                )
            if rejection.startswith("scope_rejected:resolved_scope_expands_beyond_explicit_path"):
                repair_hint += (
                    " This is a valid request-scope denial: do NOT attempt "
                    "inspect_project_scope on the fallback or parent workspace. "
                    "Use only the explicit target scope or a separately "
                    "admitted registered source prerequisite. If no exact "
                    "source probe is authorized, report a precise blocker."
                )
            journal.append("action_rejected", round=round_id,
                           tool=decision.tool, reason=rejection,
                           repair_mode=item["repair_mode"],
                           proposal_sha256=_digest(decision.arguments))
            loop._emit("minimal_goal_action_rejected",
                       request_id=loop.run_id, task_id=loop.task_id,
                       round=round_id, tool=decision.tool,
                       reason_code=item["reason_code"],
                       repair_mode=item["repair_mode"],
                       proposed_operation=(safe_operation if decision.tool == "run_command" else ""),
                       proposal_sha256=_digest(decision.arguments),
                       goal_context_sha256=context_digest,
                       executed=False)
            if consecutive_action_rejections >= 3:
                loop.protocol_state = "BLOCKED"
                set_runtime_state(loop.task_id, "PAUSED",
                                  reason="GOAL_ACTION_REPAIR_EXHAUSTED",
                                  root=loop.progress_root)
                return "Runtime 已阻止重複無效 Action，任務尚未完成：" + rejection
            continue

        # Causal guard: a confirmed absent directory cannot be read as an
        # existing project. Resolve its missing prerequisite instead.
        missing_guard = gate_known_absent_directory(action, goal_context)
        if missing_guard:
            consecutive_action_rejections += 1
            code = missing_guard["reason_code"]
            action_rejections.append({
                **classify_rejection(action["tool"], code, round_id),
                "repair_mode": missing_guard["repair_mode"],
            })
            journal.append("goal_driver_preexecution_blocked", round=round_id,
                           tool=action["tool"], reason=code,
                           prerequisite_evidence_ref=missing_guard[
                               "prerequisite_evidence_ref"], executed=False)
            loop._emit("minimal_goal_driver_blocked",
                       request_id=loop.run_id, task_id=loop.task_id,
                       round=round_id, tool=action["tool"],
                       reason_code=code, executed=False,
                       prerequisite_evidence_ref=missing_guard[
                           "prerequisite_evidence_ref"])
            repair_hint = (
                "Known verified missing directory prerequisite. "
                "The requested read has NOT executed. Do NOT repeat it. "
                "Use the Blackboard's next_category, find a genuinely "
                "admitted prerequisite, then retry only after new evidence."
            )
            if consecutive_action_rejections >= 3:
                loop.protocol_state = "BLOCKED"
                set_runtime_state(loop.task_id, "PAUSED",
                                  reason="GOAL_PREREQUISITE_REPLAN_EXHAUSTED",
                                  root=loop.progress_root)
                return "已確認必要資料夾不存在，無可驗收的後續步驟；任務暫停。"
            continue
        consecutive_action_rejections = 0
        consecutive_unverified_finishes = 0
        loop._emit("minimal_action_admitted",
                   request_id=loop.run_id, task_id=loop.task_id,
                   task_epoch=loop.task_epoch, round=round_id,
                   action_id=action["action_id"], tool=action["tool"],
                   operation=str(action.get("operation") or ""),
                   proposal_sha256=_digest(decision.arguments),
                   executed=False)

        fingerprint = _digest({k: v for k, v in action.items() if k != "action_id"})
        mutating = action_is_mutating(action)
        if mutating and fingerprint in seen_mutations:
            loop.protocol_state = "INTERRUPTED"
            journal.append("mutation_replay_blocked", round=round_id,
                           tool=action["tool"], fingerprint=fingerprint)
            raise RuntimeError("MINIMAL_MUTATION_REPLAY_UNCERTAIN")
        repeated = repeated + 1 if fingerprint == last_action_digest else 0
        if repeated >= 2:
            loop.protocol_state = "INTERRUPTED"
            journal.append("stuck_action", round=round_id,
                           tool=action["tool"], fingerprint=fingerprint)
            raise RuntimeError("MINIMAL_REPEATED_ACTION_STUCK")
        last_action_digest = fingerprint

        # Write an execution-intent receipt BEFORE any side effect.
        journal.append("action_prepared", round=round_id, tool=action["tool"],
                       action_id=action["action_id"], fingerprint=fingerprint,
                       mutating=mutating)
        if mutating:
            seen_mutations.add(fingerprint)
        loop._planner_original_actions[action["action_id"]] = dict(action)
        loop._canonical_action_proposals[action["action_id"]] = dict(action)
        loop.protocol_state = "EXECUTING"
        result = loop._execute_action(action)
        receipt = decode_result(result) or {
            "status": "UNKNOWN", "reason": "UNPARSEABLE_RESULT",
            "executed": True, "data": {},
        }
        result_digest = hashlib.sha256(str(result).encode("utf-8")).hexdigest()
        ref = "MIN-EV-" + hashlib.sha256(
            (action["action_id"] + "\0" + result_digest).encode("utf-8")
        ).hexdigest()[:24]
        history.append({"action": action, "receipt": receipt, "evidence_ref": ref})
        # Publish the same observed result into the shared Runtime action ledger.
        # Neither the model's summary nor the synthetic ref creates proof:
        # the immutable real ToolResult and digest are retained locally.
        data = receipt.get("data") if isinstance(receipt.get("data"), dict) else {}
        verification = str(data.get("verification_status", "UNVERIFIED") or "UNVERIFIED")
        loop.action_result_ledger[action["action_id"]] = {
            "action_id": action["action_id"],
            "tool": action["tool"],
            "request_id": loop.run_id,
            "task_id": loop.task_id,
            "task_epoch": loop.task_epoch,
            "produced_round": round_id,
            "result_id": ref,
            "result": str(result),
            "result_sha256": result_digest,
            "result_status": receipt.get("status", "UNKNOWN"),
            "result_reason": receipt.get("reason", "UNKNOWN"),
            "execution_status": receipt.get("status", "UNKNOWN"),
            "verification_status": verification,
            "effective_verification_status": verification,
            "executed": receipt.get("executed") is True,
            "evidence_status": "ACTIVE",
            "evidence_ref": ref,
        }
        journal.append("action_observed", round=round_id, tool=action["tool"],
                       action_id=action["action_id"], fingerprint=fingerprint,
                       result_sha256=result_digest, evidence_ref=ref,
                       status=receipt.get("status", "UNKNOWN"),
                       reason=receipt.get("reason", "UNKNOWN"),
                       executed=receipt.get("executed", False))
        loop._emit("minimal_core_action_observed", request_id=loop.run_id,
                   task_id=loop.task_id, round=round_id,
                   action_id=action["action_id"], tool=action["tool"],
                   status=receipt.get("status", "UNKNOWN"), evidence_ref=ref,
                   result_reason=str(receipt.get("reason") or "UNKNOWN")[:120],
                   verification_status=verification,
                   executed=receipt.get("executed") is True)
        loop.protocol_state = "OBSERVED"
        if loop.continuation_store:
            # A fresh action result must be present before a continuation
            # checkpoint is considered complete.
            loop.continuation_store.snapshot(loop)
        # Runtime, not Planner, owns the observed completion decision. A
        # verified single-folder creation does not consume another model turn,
        # and another harmless inspection cannot invalidate this proof.
        # Keep the historical narrow read-only task on the model's Finish
        # route. Only strict Runtime-owned Goal proof may auto-finish.
        proof = evaluate_goal_evidence(request, history)
        from .minimal_deployment_terminal import evaluate_deployment_goal
        deployment = evaluate_deployment_goal(loop, request, history)
        if deployment is not None:
            proof = deployment
        if proof.verified:
            terminal_receipt = _accept_and_record_goal(
                loop, proof, journal, round_id=round_id)
            set_runtime_state(loop.task_id, "COMPLETED", root=loop.progress_root)
            journal.append("runtime_autofinish", round=round_id,
                           receipt_id=terminal_receipt["receipt_id"],
                           goal_kind=proof.goal_kind)
            return format_runtime_goal_summary(proof)
        if receipt.get("status") in {"UNKNOWN", "RUNNING"}:
            repair_hint = ("Last action outcome is uncertain or running. "
                           "Observe current state; NEVER replay a mutation blindly.")

    loop.protocol_state = "INTERRUPTED"
    journal.append("max_rounds_exhausted", max_rounds=loop.max_turns)
    raise RuntimeError("MINIMAL_CORE_MAX_ROUNDS")
