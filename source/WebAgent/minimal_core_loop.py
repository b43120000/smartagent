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
from .minimal_decision import MinimalDecisionError, parse_minimal_decision


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
                         *, round_id: int, max_turns: int, repair_hint: str = "") -> str:
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
        "For run_command provide command and operation. For commands that "
        "are not provably read-only also provide nonempty success_criteria "
        "and verify checks. Unknown execution outcome is NOT safe to replay.\n"
        "Workspace: " + str(context.workspace_root) + "\n"
        "Available tools: " + json.dumps(tool_catalog(context), separators=(",", ":")) + "\n"
        "Original goal: " + request + "\n"
        "Round: " + str(round_id) + "/" + str(max_turns) + "\n"
        "Observed tool results: " +
        json.dumps(recent_results(history), ensure_ascii=False, separators=(",", ":")) + "\n"
        + ("Last decision rejected before execution: " + repair_hint + "\n"
           if repair_hint else "")
        + "[/SMARTAGENT_MINIMAL_CORE_V1]"
    )


def narrow_goal_verified(goal: str, history: list[dict]) -> bool:
    """No broad semantic inference from a model finish or mere exit code."""
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
        return bool(data.get("directory_created") is True
                    and data.get("observed_is_directory") is True
                    and re.search(r"建立|創立|新增|create|mkdir", goal, re.I))
    return False


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

    for round_id in range(1, loop.max_turns + 1):
        loop._check_cancelled()
        loop.turn_id = round_id
        loop.tools._protocol_turn_seq = round_id
        prompt = build_minimal_prompt(request, loop.tools, history,
                                      round_id=round_id, max_turns=loop.max_turns,
                                      repair_hint=repair_hint)
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
            decision = parse_minimal_decision(response)
        except MinimalDecisionError as exc:
            invalids += 1
            repair_hint = str(exc)
            journal.append("decision_rejected", round=round_id,
                           reason=repair_hint, response_sha256=_digest(response))
            if invalids >= 2:
                loop.protocol_state = "INTERRUPTED"
                raise RuntimeError("MINIMAL_DECISION_FORMAT_FAILED:" + repair_hint)
            continue
        invalids = 0
        repair_hint = ""

        if decision.kind == "blocked":
            loop.protocol_state = "BLOCKED"
            loop.terminal_outcome = "UNKNOWN"
            loop.terminal_acceptance = {
                "decision": "BLOCKED", "outcome": "UNKNOWN", "evidence_verified": False,
            }
            journal.append("model_blocked", round=round_id,
                           reason_sha256=_digest(decision.message))
            set_runtime_state(loop.task_id, "PAUSED", reason="MINIMAL_MODEL_BLOCKED",
                              root=loop.progress_root)
            return "目前無法繼續（模型尚未證明任務完成）：" + decision.message

        if decision.kind == "finish":
            verified = narrow_goal_verified(request, history)
            ref = history[-1]["evidence_ref"] if history else ""
            loop.terminal_outcome = "SUCCESS" if verified else "UNKNOWN"
            loop.protocol_state = "COMPLETED" if verified else "MODEL_FINISHED_UNVERIFIED"
            loop.terminal_acceptance = {
                "decision": "FINISH", "outcome": loop.terminal_outcome,
                "evidence_verified": verified,
                "verification_scope": "SINGLE_OPERATION" if verified else "UNVERIFIED",
                "evidence_refs": [ref] if ref else [],
            }
            journal.append("model_finish", round=round_id,
                           outcome=loop.terminal_outcome, verified=verified,
                           evidence_ref=ref, summary_sha256=_digest(decision.message))
            set_runtime_state(loop.task_id, "COMPLETED" if verified else "PAUSED",
                              reason="" if verified else "MODEL_FINISHED_UNVERIFIED",
                              root=loop.progress_root)
            return (decision.message if verified else
                    "模型已回覆，但 Runtime 尚無足夠證據確認整體目標完成：\n"
                    + decision.message)

        action, rejection = admit_minimal_action(decision, loop.tools)
        if rejection:
            invalids += 1
            repair_hint = rejection
            journal.append("action_rejected", round=round_id,
                           tool=decision.tool, reason=rejection,
                           proposal_sha256=_digest(decision.arguments))
            if invalids >= 2:
                loop.protocol_state = "INTERRUPTED"
                raise RuntimeError("MINIMAL_ACTION_ADMISSION_FAILED:" + rejection)
            continue

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
                   executed=receipt.get("executed") is True)
        loop.protocol_state = "OBSERVED"
        if loop.continuation_store:
            # A fresh action result must be present before a continuation
            # checkpoint is considered complete.
            loop.continuation_store.snapshot(loop)
        if receipt.get("status") in {"UNKNOWN", "RUNNING"}:
            repair_hint = ("Last action outcome is uncertain or running. "
                           "Observe current state; NEVER replay a mutation blindly.")

    loop.protocol_state = "INTERRUPTED"
    journal.append("max_rounds_exhausted", max_rounds=loop.max_turns)
    raise RuntimeError("MINIMAL_CORE_MAX_ROUNDS")
