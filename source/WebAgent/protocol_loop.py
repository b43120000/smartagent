#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Independent WebAgent protocol/result/ACK loop."""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Callable

from agent_core.smartagent_protocol import format_tool_parse_diagnostics
from agent_core.protocol_v9 import (
    ProtocolV8Error,
    V8RequestContext,
    action_digest as v8_action_digest,
    admit_action as v8_admit_action,
    build_result as v8_build_result,
    parse_v9_tool_transport,
    validate_model_commit as v8_validate_model_commit,
)
from agent_core.result_exchange import prepare_tool_result
from agent_core.narrative_bridge import NarrativeDecisionBridge
from agent_core.recovery_protocol import ACTION_EXECUTION_MODE
from agent_core.payload_budget import ROUND_INLINE_MAX_BYTES, utf8_size
from agent_core.routing import lazy_context_sync_prompt
from agent_core.image_delivery import plan_image_delivery
from agent_core.stage_executor import execute_stage, preflight_stage
from agent_core.stage_protocol import StageManifestError, validate_stage_manifest
from agent_core.task_progress import (
    TaskProgressError,
    format_prompt_context,
    initialize_progress,
    record_model_progress,
    set_runtime_state,
    validate_model_progress,
)

from .protocol import SUPPORTED_ACTION_TOOLS
from .tool_context import WebAgentToolContext


PlannerCall = Callable[[str, dict, list[str]], str]
EventSink = Callable[[str], None]
WEBAGENT_STAGED_PROTOCOL = str(os.environ.get("SMARTAGENT_STAGED_PROTOCOL", "on")).strip().lower()

_WINDOWS_PATH_START_RE = re.compile(r"(?<![A-Za-z0-9])(?P<drive>[A-Za-z]:[\\/])")
_PATH_SENTENCE_BOUNDARY_RE = re.compile(r"[，；。]|[,;](?=\s|$)|[\r\n]")
_PATH_PROSE_BOUNDARY_RE = re.compile(
    r"\s+(?=(?:然後|接著|接下來|之後|完成後|接續|繼續|你先|請|並且|同時|最後|再(?:去|執行|看|讀|修改|處理)))"
)
_PATH_ATTACHED_PROSE_BOUNDARY_RE = re.compile(
    r"(?=(?:下(?:的|面)|裡面|里面|內(?:有|的)|有多少|有幾個|共有|的檔案))"
)
_PATH_QUOTE_PAIRS = {
    '"': '"', "'": "'", "`": "`",
    "「": "」", "『": "』",
}


def _hard_authorized_path_end(text: str, content_start: int) -> int:
    """Return the next boundary that cannot be part of the current path span."""
    end = len(text)
    newline = re.search(r"[\r\n]", text[content_start:])
    if newline:
        end = content_start + newline.start()
    next_path = _WINDOWS_PATH_START_RE.search(text, content_start)
    if next_path and next_path.start() < end:
        end = next_path.start()
    return end


def _authorized_path_end(text: str, start: int, content_start: int) -> int:
    """Return the end of one explicit Windows path in request prose.

    A request may mention more than one path on the same line.  Treating the
    entire remainder of that line as one path joins prose and the next drive
    designator into a value such as ``C:\\first ... C:\\second``.  The second
    colon is then correctly rejected as a possible NTFS alternate data stream.

    Quoted paths may contain sentence punctuation.  Unquoted paths stop at a
    sentence boundary or before the next drive-qualified path.
    """
    opener = text[start - 1] if start > 0 else ""
    closer = _PATH_QUOTE_PAIRS.get(opener)
    if closer:
        quoted_end = text.find(closer, content_start)
        if quoted_end >= 0:
            return quoted_end

    end = len(text)
    boundary = _PATH_SENTENCE_BOUNDARY_RE.search(text, content_start)
    if boundary:
        end = boundary.start()
    prose_boundary = _PATH_PROSE_BOUNDARY_RE.search(text, content_start)
    if prose_boundary and prose_boundary.start() < end:
        end = prose_boundary.start()
    attached_boundary = _PATH_ATTACHED_PROSE_BOUNDARY_RE.search(text, content_start)
    if attached_boundary and attached_boundary.start() < end:
        end = attached_boundary.start()
    next_path = _WINDOWS_PATH_START_RE.search(text, content_start)
    if next_path and next_path.start() < end:
        end = next_path.start()
    return end


def extract_authorized_paths(request: str) -> list[str]:
    text = str(request or "")
    paths: list[str] = []
    consumed_until = -1
    for match in _WINDOWS_PATH_START_RE.finditer(text):
        if match.start() < consumed_until:
            continue
        hard_end = _hard_authorized_path_end(text, match.end())
        hidden_colon = text.find(":", match.end(), hard_end)
        # Do not let prose/quote parsing erase a possible ADS suffix.  Preserve
        # the colon so canonical path security rejects the candidate.
        end = (
            hidden_colon + 1
            if hidden_colon >= 0
            else _authorized_path_end(text, match.start(), match.end())
        )
        candidate = text[match.start():end].strip().rstrip(" \t.,;，；。")
        consumed_until = max(end, match.end())
        if candidate and candidate not in paths:
            paths.append(candidate)
    return paths


def local_commit_line(expected: dict) -> str:
    payload = {"protocol_name": "web_agent_direct", "protocol_version": 9}
    return "[WEBAGENT_V8_LOCAL_COMMIT] " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def action_signature(action: dict) -> str:
    return v8_action_digest(action)


class WebAgentProtocolLoop:
    def __init__(
        self,
        workspace: str | Path,
        planner: PlannerCall,
        *,
        tool_context: WebAgentToolContext | None = None,
        max_turns: int = 250,
        event_sink: Callable[..., None] | None = None,
        display_name: str = "WebAgent",
        cancel_check: Callable[[], None] | None = None,
        progress_root: str | Path | None = None,
    ):
        self.workspace = Path(workspace).expanduser().resolve()
        self.planner = planner
        self.tools = tool_context or WebAgentToolContext(self.workspace)
        self.max_turns = max(1, int(max_turns))
        self.event_sink = event_sink
        self.tools._project_sync_event_sink = event_sink
        self.display_name = str(display_name or "WebAgent")
        self.cancel_check = cancel_check
        self.progress_root = Path(progress_root).resolve() if progress_root is not None else None
        self.progress_ledger = None
        self.terminal_outcome = "UNKNOWN"
        self.tools.configure_project_sync_transport(
            lambda prompt, attachments: self.planner(prompt, None, attachments)
        )
        self.run_id = ""
        self.turn_id = 0
        self.pending_result_ack_id = ""
        self.pending_web_ack_id = ""
        self.seen_web_ack_ids: set[str] = set()
        self.accepted_ack_ids: list[str] = []
        self.sent_attachment_paths: list[str] = []
        self.action_ledger: dict[str, dict] = {}
        self.action_result_ledger: dict[str, dict] = {}
        self.v8_admitted_actions: dict[str, dict] = {}
        self.active_stage_manifest: dict | None = None
        self.image_delivery_plan: dict = {}
        self.authorized_paths: list[str] = []
        self.pending_verification_requirement: dict = {}
        self.execution_state = "NOT_STARTED"
        self.protocol_state = "IDLE"
        self.terminal_candidate: dict = {}

    def _check_cancelled(self) -> None:
        if self.cancel_check is not None:
            self.cancel_check()

    def _emit(self, event: str, **fields) -> None:
        if self.event_sink is not None:
            self.event_sink(event, **fields)

    def _new_commit(self) -> dict:
        self.turn_id += 1
        self.tools._protocol_turn_seq = self.turn_id
        commit = {
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "local_nonce": uuid.uuid4().hex,
            "ack_result_id": self.pending_result_ack_id,
            "ack_web_ack_id": self.pending_web_ack_id,
        }
        if getattr(self, "task_id", ""):
            commit["task_id"] = self.task_id
        if getattr(self, "task_epoch", ""):
            commit["task_epoch"] = self.task_epoch
        if getattr(self, "intent_digest", ""):
            commit["intent_digest"] = self.intent_digest
        if self.image_delivery_plan:
            ledger = self.progress_ledger
            existing_steps = list(getattr(ledger, "steps", []) or [])
            existing_total = getattr(ledger, "total_steps", 0) or 0
            existing_current = getattr(ledger, "current_step", 0) or 0
            if existing_steps and float(existing_total) > 0:
                runtime_progress = {
                    "base_evaluation": str(
                        getattr(ledger, "base_evaluation", "")
                        or "Runtime 已確認本輪圖片生成完成並取得 request-scoped artifact。"
                    ),
                    "total_steps": existing_total,
                    "current_step": existing_current,
                    "steps": existing_steps,
                    "current_focus": "Runtime 正在交付已暫存的 PNG 原檔。",
                    "next_action": "完成本機寫入與必要的 Telegram 排程後回報交付結果。",
                }
            else:
                runtime_progress = {
                    "base_evaluation": "Runtime 已確認本輪圖片生成完成並取得 request-scoped artifact。",
                    "total_steps": 2,
                    "current_step": 1,
                    "steps": [
                        {"step": 1, "desc": "生成並暫存本輪圖片原檔", "status": "COMPLETED"},
                        {"step": 2, "desc": "交付圖片到指定位置", "status": "IN_PROGRESS"},
                    ],
                    "current_focus": "Runtime 正在交付已暫存的 PNG 原檔。",
                    "next_action": "完成本機寫入與必要的 Telegram 排程後回報交付結果。",
                    "completion_contract": {
                        "success": ["本輪圖片原檔已寫入指定位置並完成必要交付"],
                        "failure": ["圖片生成已結束但原檔下載、寫入或交付失敗"],
                        "in_progress": ["本輪 fresh image 已確認，原檔仍在下載或交付"],
                        "interrupted": ["Runtime 無法繼續取得或交付本輪 fresh image"],
                    },
                    "decision": "CONTINUE",
                    "outcome": "PENDING",
                    "matched_condition": "本輪 fresh image 已確認，原檔仍在下載或交付",
                    "evidence_refs": ["RUNTIME_STATUS"],
                    "decision_reason": "Runtime 已確認 request-scoped fresh image，仍需完成下載與交付。",
                }
            commit.update({
                "artifact_save_expected": True,
                "artifact_kind": "image",
                "artifact_delivery": self.image_delivery_plan["delivery"],
                "artifact_output_path": self.image_delivery_plan["output_path"],
                "artifact_expected_filename": self.image_delivery_plan["expected_filename"],
                "runtime_image_progress": runtime_progress,
            })
        return commit

    def _narrative_recovery_context(self, prompt: str) -> dict:
        """Build bounded, read-only context for one semantic continuation."""
        ledger = self.progress_ledger
        compact_steps = []
        for item in list(getattr(ledger, "steps", []) or [])[:64]:
            if not isinstance(item, dict):
                continue
            compact_steps.append({
                "step": item.get("step"),
                "desc": str(item.get("desc", "") or "")[:240],
                "status": str(item.get("status", "") or "")[:32],
            })
        progress = {
            "base_evaluation": str(getattr(ledger, "base_evaluation", "") or ""),
            "total_steps": getattr(ledger, "total_steps", 0),
            "current_step": getattr(ledger, "current_step", 0),
            "steps": compact_steps,
            "current_focus": str(getattr(ledger, "current_focus", "") or ""),
            "next_action": str(getattr(ledger, "next_action", "") or ""),
            "runtime_state": str(getattr(ledger, "runtime_state", "PROCESSING") or "PROCESSING"),
            "completion_contract": dict(getattr(ledger, "completion_contract", {}) or {}),
            "decision": str(getattr(ledger, "decision", "CONTINUE") or "CONTINUE"),
            "outcome": str(getattr(ledger, "outcome", "PENDING") or "PENDING"),
            "matched_condition": str(getattr(ledger, "matched_condition", "") or ""),
            "evidence_refs": list(getattr(ledger, "evidence_refs", []) or []),
            "decision_reason": str(getattr(ledger, "decision_reason", "") or ""),
        }
        latest_context = str(prompt or "")
        # Correlation remains runtime-owned. The semantic continuation only
        # needs the tool result/progress meaning, never their transport IDs.
        latest_context = re.sub(
            r"(?m)^(?:RUN_ID|RESULT_ID|request_id|task_id|task_epoch|round|attempt|previous_ack_id)=.*$",
            "",
            latest_context,
        ).strip()
        return {
            "goal": str(getattr(self, "task_request", "") or "")[:16000],
            "progress": progress,
            "latest_runtime_context": latest_context[-16000:],
            "authorized_paths": list(self.authorized_paths),
        }

    def _with_commit(self, prompt: str, expected: dict) -> str:
        trace = (
            "[WEBAGENT_REQUEST_TRACE]\n"
            f"request_id={expected['run_id']}\n"
            f"round={expected['turn_id']}\n"
            "attempt=1\n"
            f"previous_ack_id={expected['ack_web_ack_id']}\n"
            "[/WEBAGENT_REQUEST_TRACE]"
        )
        return (
            f"[SMARTAGENT_MODE] {ACTION_EXECUTION_MODE}\n"
            + str(prompt).rstrip()
            + "\n\n"
            + self._runtime_evidence_context()
            + "\n"
            + trace
            + "\n[WEBAGENT_ACK_REQUIRED]\n"
            + "只能輸出 compact v9 smartagent_tool blocks，不得輸出 blocks 以外的自然語言；"
            + "每輪恰好一個 report_progress，最後一個 block 必須是 "
            + "{\"tool\":\"turn_commit\",\"action_count\":N}。\n"
            + "若已輸出 final_response 且 outcome 為 SUCCESS/FAILED/PARTIAL，"
            + "decision 必須是 COMPLETE，不得填 CONTINUE。\n"
            + local_commit_line(expected)
        )

    def _runtime_evidence_refs(self) -> set[str]:
        refs = {"REQUEST_ACCEPTED", "RUNTIME_STATUS"}
        refs.update(str(action_id) for action_id in self.action_result_ledger)
        return refs

    def _runtime_evidence_context(self) -> str:
        evidence = []
        for action_id, item in self.action_result_ledger.items():
            evidence.append({
                "action_id": str(action_id),
                "tool": str(item.get("tool", "") or ""),
                "execution_status": str(item.get("execution_status", "UNKNOWN") or "UNKNOWN"),
                "verification_status": str(item.get("verification_status", "UNKNOWN") or "UNKNOWN"),
            })
        payload = {
            "available_refs": sorted(self._runtime_evidence_refs()),
            "verification_status": self.tools.last_verification_status,
            "execution_state": self.execution_state,
            "protocol_state": self.protocol_state,
            "action_evidence": evidence[-16:],
        }
        return (
            "[RUNTIME_EVIDENCE]\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n[/RUNTIME_EVIDENCE]"
        )

    @staticmethod
    def _has_structured_verification_action(actions: list[dict]) -> bool:
        """Return whether this round can produce a new PASS/FAIL state."""
        for action in actions:
            if str(action.get("tool", "") or "") != "run_command":
                continue
            criteria = str(action.get("success_criteria", "") or "").strip()
            verify = action.get("verify")
            checks = list(verify) if isinstance(verify, list) else ([verify] if isinstance(verify, dict) else [])
            if criteria and checks and all(isinstance(item, dict) and item for item in checks):
                return True
        return False

    @staticmethod
    def _run_command_verification_fields(action: dict) -> list[str]:
        """Return missing fields required before a normal command may run.

        Asking for verification after a state-changing command has already run
        is too late: a format failure in the next model turn can otherwise turn
        successful work into an apparent task failure.  Bootstrap commands have
        their own deterministic verifier and are exempted by the caller.
        """
        missing = []
        if not str(action.get("success_criteria", "") or "").strip():
            missing.append("success_criteria")
        verify = action.get("verify")
        checks = list(verify) if isinstance(verify, list) else ([verify] if isinstance(verify, dict) else [])
        if not checks or not all(isinstance(item, dict) and item for item in checks):
            missing.append("verify")
        return missing

    @staticmethod
    def _classify_action_evidence(action: dict, result: str) -> dict:
        """Separate execution facts from postcondition verification facts."""
        text = str(result or "")
        verification_match = re.search(
            r"(?m)^VERIFICATION_STATUS:\s*(PASS|FAIL|UNVERIFIED)\s*$", text,
        )
        verification = verification_match.group(1) if verification_match else "UNKNOWN"
        execution = "SUCCEEDED"
        exit_match = re.search(r"(?m)^exit_code:\s*([^\r\n]+)", text)
        if exit_match:
            raw_exit = exit_match.group(1).strip()
            execution = "SUCCEEDED" if raw_exit == "0" else "FAILED"
        elif any(marker in text for marker in (
            "[SECURITY_COMMAND_REJECTED]", "[TOOL_SCOPE_REJECTED]",
            "[WEBAGENT_TOOL_REJECTED]", "[PROTOCOL_ERROR]",
        )):
            execution = "FAILED"
        return {
            "tool": str(action.get("tool", "") or ""),
            "execution_status": execution,
            "verification_status": verification,
        }

    def _verification_requirement_diagnostic(
        self,
        normalized_progress: dict,
        *,
        reason: str,
    ) -> dict:
        condition = str(normalized_progress.get("matched_condition", "") or "").strip()
        requirement = dict(self.pending_verification_requirement or {})
        if not requirement:
            requirement = {
                "missing_condition": condition or "取得支持 terminal SUCCESS 的 request-scoped PASS evidence",
                "current_verification_status": str(self.tools.last_verification_status or "UNKNOWN").upper(),
                "required_action": "run_command",
                "required_fields": ["command", "success_criteria", "verify"],
                "supported_verify_actions": ["run_command", "file_exists", "file_contains"],
                "evidence_refs": list(normalized_progress.get("evidence_refs") or []),
            }
            self.pending_verification_requirement = dict(requirement)
        detail = json.dumps(requirement, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "marker": "[WEBAGENT_VERIFICATION_REQUIRED]",
            "reason": reason,
            "detail": detail,
            "suggestion": (
                "下一輪必須使用 decision=CONTINUE，並輸出至少一個 run_command action；"
                "該 action 必須同時包含 command、描述精確成功條件的 success_criteria，以及非空 verify。"
                "verify 可用 run_command/file_exists/file_contains。Runtime 執行後會回傳 PASS 或 FAIL；"
                "在取得新狀態前不得只回 report_progress，也不得再次宣告 SUCCESS。"
            ),
        }

    @staticmethod
    def _rejected_exchange_signature(
        calls: list[dict], diagnostics: list[dict], raw_response: str,
    ) -> str:
        semantic_calls: list[dict] = []
        for call in calls:
            if not isinstance(call, dict) or call.get("tool") == "turn_commit":
                continue
            tool = str(call.get("tool", "") or "")
            if tool == "report_progress":
                semantic_calls.append({
                    "tool": tool,
                    "decision": str(call.get("decision", "") or "").upper(),
                    "outcome": str(call.get("outcome", "") or "").upper(),
                    "evidence_refs": sorted(str(item) for item in list(call.get("evidence_refs") or [])),
                })
            elif tool == "final_response":
                # User-facing wording is not progress.  Excluding it lets the
                # Runtime recognize the same rejected terminal decision even
                # when the model paraphrases the answer or changes action_id.
                semantic_calls.append({"tool": tool})
            else:
                semantic_calls.append({
                    key: value for key, value in call.items() if key != "action_id"
                })
        answer: object = semantic_calls
        if not semantic_calls:
            answer = re.sub(r"\s+", " ", str(raw_response or "")).strip()
        return json.dumps(
            {
                "diagnostics": [
                    {"reason": item.get("reason", "")}
                    for item in diagnostics
                ],
                "answer": answer,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _bind_terminal_result_evidence(
        self,
        progress_action: dict,
        normalized_progress: dict,
    ) -> bool:
        """Bind the newest completed Runtime result to a terminal decision.

        Correlation is Runtime-owned.  The model is not required to copy an
        action id that the Runtime already knows belongs to this request.
        """
        if normalized_progress.get("decision") not in {"COMPLETE", "INTERRUPT"}:
            return False
        result_refs = list(self.action_result_ledger)
        if not result_refs:
            return False
        current = list(normalized_progress.get("evidence_refs") or [])
        if any(item in self.action_result_ledger for item in current):
            return False
        verification = str(self.tools.last_verification_status or "").upper()
        if (
            normalized_progress.get("decision") == "COMPLETE"
            and normalized_progress.get("outcome") == "SUCCESS"
            and verification in {"FAIL", "UNVERIFIED"}
        ):
            return False
        latest = result_refs[-1]
        current.append(latest)
        progress_action["evidence_refs"] = current
        normalized_progress["evidence_refs"] = current
        self._emit(
            "terminal_evidence_bound",
            request_id=self.run_id,
            round=self.turn_id,
            action_id=latest,
            decision=normalized_progress.get("decision"),
            outcome=normalized_progress.get("outcome"),
        )
        return True

    def _normalize_unambiguous_terminal_decision(
        self,
        progress_action: dict,
        operational: list[dict],
    ) -> bool:
        """Repair only a structurally unambiguous CONTINUE/terminal mismatch.

        The Runtime does not infer whether the task succeeded.  It only fixes
        the decision enum when the model has already supplied every terminal
        semantic: a terminal outcome, completed plan, declared matching
        condition, request-scoped action evidence, and an exclusive final
        response.  Ambiguous or contradicted responses remain rejected.
        """
        decision = str(progress_action.get("decision", "") or "").strip().upper()
        outcome = str(progress_action.get("outcome", "") or "").strip().upper()
        if decision != "CONTINUE" or outcome not in {"SUCCESS", "FAILED", "PARTIAL"}:
            return False
        if len(operational) != 1 or operational[0].get("tool") != "final_response":
            return False

        try:
            total = float(progress_action.get("total_steps"))
            current = float(progress_action.get("current_step"))
        except (TypeError, ValueError):
            return False
        if total <= 0 or current < total:
            return False

        supplied_steps = progress_action.get("steps")
        steps = supplied_steps
        if steps is None and self.progress_ledger is not None:
            steps = getattr(self.progress_ledger, "steps", None)
        if not isinstance(steps, list) or not steps:
            return False
        if any(
            not isinstance(item, dict)
            or str(item.get("status", "") or "").strip().upper() != "COMPLETED"
            for item in steps
        ):
            return False

        condition_class = "success" if outcome == "SUCCESS" else "failure"
        accepted_contract = dict(
            getattr(self.progress_ledger, "completion_contract", {}) or {}
        )
        proposed_contract = progress_action.get("completion_contract")
        declared_conditions = list(accepted_contract.get(condition_class) or [])
        if isinstance(proposed_contract, dict):
            candidate_conditions = proposed_contract.get(condition_class)
            if isinstance(candidate_conditions, list):
                declared_conditions.extend(
                    str(item) for item in candidate_conditions if str(item).strip()
                )
        matched = str(progress_action.get("matched_condition", "") or "").strip()
        if not matched or matched not in declared_conditions:
            return False

        evidence_refs = {
            str(item) for item in list(progress_action.get("evidence_refs") or [])
        }
        known_refs = self._runtime_evidence_refs()
        if not evidence_refs or evidence_refs - known_refs:
            return False
        result_refs = evidence_refs & set(self.action_result_ledger)
        if not result_refs:
            return False
        if outcome == "SUCCESS":
            evidence = [self.action_result_ledger[item] for item in result_refs]
            if not any(
                str(item.get("execution_status", "") or "").upper() == "SUCCEEDED"
                for item in evidence
            ):
                return False
            if str(self.tools.last_verification_status or "").upper() == "FAIL":
                return False

        progress_action["decision"] = "COMPLETE"
        self._emit(
            "terminal_decision_normalized",
            request_id=self.run_id,
            round=self.turn_id,
            original_decision="CONTINUE",
            normalized_decision="COMPLETE",
            outcome=outcome,
            matched_condition=matched,
            evidence_refs=sorted(result_refs),
            reason="terminal_semantics_complete_but_decision_was_continue",
        )
        return True

    def _accept_ack(self, calls: list[dict], expected: dict) -> tuple[list[dict], list[dict]]:
        return self._accept_v8_ack(calls, expected)

    def _accept_v8_ack(self, calls: list[dict], expected: dict) -> tuple[list[dict], list[dict]]:
        self.active_stage_manifest = None
        if not calls or calls[-1].get("tool") != "turn_commit":
            return [], [{
                "marker": "[WEBAGENT_V8_REJECTED]",
                "reason": "missing_turn_commit",
                "detail": "v8 response must end with compact turn_commit",
                "suggestion": "最後輸出 {\"tool\":\"turn_commit\",\"action_count\":N}。",
            }]
        actions = list(calls[:-1])
        try:
            v8_validate_model_commit(calls[-1], len(actions))
        except ProtocolV8Error as exc:
            return [], [{
                "marker": "[WEBAGENT_V8_REJECTED]",
                "reason": exc.code,
                "detail": exc.detail,
                "suggestion": "只修正 compact v9 turn_commit，不要重做 action。",
            }]
        progress_actions = [action for action in actions if action.get("tool") == "report_progress"]
        if len(progress_actions) != 1:
            return [], [{
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "progress_count_invalid",
                "detail": f"expected=1;actual={len(progress_actions)}",
                "suggestion": "每輪必須輸出且只輸出一個 report_progress。",
            }]
        operational = [action for action in actions if action.get("tool") != "report_progress"]
        if getattr(self.tools, "_bootstrap_install_root", None) is None:
            for action in operational:
                if str(action.get("tool", "") or "") != "run_command":
                    continue
                missing = self._run_command_verification_fields(action)
                if missing:
                    return [], [{
                        "marker": "[WEBAGENT_VERIFICATION_REQUIRED]",
                        "reason": "run_command_verification_contract_required",
                        "detail": "missing=" + ",".join(missing),
                        "suggestion": (
                            "保留同一 command 與決策，先補上精確 success_criteria 及非空 verify；"
                            "Runtime 必須在執行前取得完成條件，避免工具成功後才追討證據。"
                        ),
                    }]
        if any(action.get("tool") == "final_response" for action in operational) and (
            len(operational) != 1 or operational[0].get("tool") != "final_response"
        ):
            return [], [{
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "final_response_not_exclusive",
                "detail": "final_response may only share a round with report_progress",
                "suggestion": "將其他 action 留在前一輪；完成輪只輸出 report_progress、final_response、turn_commit。",
            }]
        self._normalize_unambiguous_terminal_decision(
            progress_actions[0], operational,
        )
        try:
            normalized_progress = validate_model_progress(progress_actions[0], self.progress_ledger)
        except TaskProgressError as exc:
            return [], [{
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "invalid_progress_payload",
                "detail": str(exc),
                "suggestion": "保留決策，只修正 report_progress 的階段、Base、steps 或文字欄位。",
            }]
        decision = normalized_progress["decision"]
        outcome = normalized_progress["outcome"]
        evidence_refs = set(normalized_progress["evidence_refs"])
        unknown_refs = sorted(evidence_refs - self._runtime_evidence_refs())
        if unknown_refs:
            return [], [{
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "runtime_evidence_ref_unknown",
                "detail": "unknown=" + ",".join(unknown_refs),
                "suggestion": "evidence_refs 只能引用 [RUNTIME_EVIDENCE] 列出的值。",
            }]
        self._bind_terminal_result_evidence(progress_actions[0], normalized_progress)
        evidence_refs = set(normalized_progress["evidence_refs"])
        result_refs = set(self.action_result_ledger)
        if decision in {"COMPLETE", "INTERRUPT"} and result_refs and not (
            evidence_refs & result_refs
        ):
            return [], [{
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "terminal_decision_missing_action_result_evidence",
                "detail": "terminal decision must reference at least one completed action result",
                "suggestion": "從 [RUNTIME_EVIDENCE] 引用支持終止判斷的 action_id。",
            }]
        if decision == "COMPLETE" and (
            len(operational) != 1 or operational[0].get("tool") != "final_response"
        ):
            return [], [{
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "complete_decision_requires_final_response",
                "detail": "decision=COMPLETE requires exactly one final_response",
                "suggestion": "完成輪只輸出 report_progress、final_response、turn_commit。",
            }]
        if decision == "CONTINUE" and any(
            action.get("tool") == "final_response" for action in operational
        ):
            return [], [{
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "continue_decision_forbids_final_response",
                "detail": "decision=CONTINUE cannot terminate the task",
                "suggestion": "仍需執行就輸出明確 action；已完成則改為 decision=COMPLETE。",
            }]
        if decision == "INTERRUPT" and (
            len(operational) > 1
            or (operational and operational[0].get("tool") != "final_response")
        ):
            return [], [{
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "interrupt_decision_forbids_action",
                "detail": "decision=INTERRUPT may only include an explanatory final_response",
                "suggestion": "中斷時不要再要求本機 action；可附一個 final_response 說明原因。",
            }]
        verification_status = str(self.tools.last_verification_status or "").upper()
        if verification_status in {"PASS", "FAIL"}:
            self.pending_verification_requirement = {}
        if (
            self.pending_verification_requirement
            and verification_status == "UNVERIFIED"
            and decision == "CONTINUE"
            and not self._has_structured_verification_action(operational)
        ):
            return [], [self._verification_requirement_diagnostic(
                normalized_progress,
                reason="verification_evidence_action_required",
            )]
        if decision == "COMPLETE" and outcome == "SUCCESS" and verification_status == "UNVERIFIED":
            final_action = next(
                (item for item in operational if item.get("tool") == "final_response"),
                {},
            )
            self.terminal_candidate = {
                "decision": decision,
                "outcome": outcome,
                "content": str(final_action.get("content", "") or ""),
                "matched_condition": str(normalized_progress.get("matched_condition", "") or ""),
                "evidence_refs": list(normalized_progress.get("evidence_refs") or []),
                "verification_status": "UNVERIFIED",
            }
            self.pending_verification_requirement = {
                "missing_condition": (
                    str(normalized_progress.get("matched_condition", "") or "").strip()
                    or "取得支持 terminal SUCCESS 的 request-scoped PASS evidence"
                ),
                "current_verification_status": "UNVERIFIED",
                "required_action": "run_command",
                "required_fields": ["command", "success_criteria", "verify"],
                "supported_verify_actions": ["run_command", "file_exists", "file_contains"],
                "evidence_refs": list(normalized_progress.get("evidence_refs") or []),
            }
            return [], [self._verification_requirement_diagnostic(
                normalized_progress,
                reason="terminal_success_verification_missing",
            )]
        if decision == "COMPLETE" and outcome == "SUCCESS" and verification_status == "FAIL":
            return [], [{
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "success_contradicts_runtime_verification",
                "detail": f"model_outcome=SUCCESS;verification={verification_status}",
                "suggestion": "依目前 FAIL evidence 回覆 FAILED/PARTIAL，或執行修正 action 後再用結構化 verify 重新驗證。",
            }]
        context = V8RequestContext(
            self.run_id,
            self.task_id or self.run_id,
            self.task_epoch or self.run_id,
            self.intent_digest,
        )
        admitted: dict[str, dict] = {}
        diagnostics: list[dict] = []
        for index, action in enumerate(actions, 1):
            tool_name = str(action.get("tool", "") or "")
            if tool_name not in {"final_response", "report_progress"} and tool_name not in SUPPORTED_ACTION_TOOLS:
                diagnostics.append({
                    "marker": "[WEBAGENT_V8_REJECTED]",
                    "reason": "unsupported_tool",
                    "detail": f"action_index={index};tool={tool_name}",
                    "suggestion": "只使用 protocol.py 宣告的 supported action tool。",
                })
                continue
            try:
                record = v8_admit_action(action, context)
            except ProtocolV8Error as exc:
                diagnostics.append({
                    "marker": "[WEBAGENT_V8_REJECTED]",
                    "reason": exc.code,
                    "detail": f"action_index={index};action_id={action.get('action_id', '')};{exc.detail}",
                    "suggestion": "只補缺少的決策欄位，不要修改已提供欄位。",
                })
                continue
            action_id = record["action_id"]
            if action_id in admitted:
                diagnostics.append({
                    "marker": "[WEBAGENT_V8_REJECTED]",
                    "reason": "duplicate_action_id",
                    "detail": f"action_id={action_id}",
                    "suggestion": "同一回合每個 action_id 只能出現一次。",
                })
            else:
                admitted[action_id] = record
        if diagnostics:
            return [], diagnostics
        self.v8_admitted_actions = admitted
        self.tools._v8_admitted_actions = admitted
        self.protocol_state = "ACCEPTED"
        self.pending_result_ack_id = ""
        self.pending_web_ack_id = "WEBACK-V8-" + uuid.uuid4().hex[:16].upper()
        self.seen_web_ack_ids.add(self.pending_web_ack_id)
        self.accepted_ack_ids.append(self.pending_web_ack_id)
        self._emit(
            "ack_accepted", request_id=self.run_id, round=self.turn_id,
            previous_ack_id=expected.get("ack_web_ack_id", ""),
            ack_id=self.pending_web_ack_id,
        )
        return actions, []

    def _execute_action(self, action: dict) -> str:
        action_id = str(action.get("action_id", "") or "")
        signature = action_signature(action)
        cached = self.action_ledger.get(action_id)
        if cached:
            if cached["signature"] != signature:
                raise RuntimeError(f"action_id payload mismatch: {action_id}")
            return str(cached["result"])
        raw_result = self.tools.execute(action)
        result = prepare_tool_result(action, raw_result, self.tools)
        evidence = self._classify_action_evidence(action, result)
        self.action_ledger[action_id] = {
            "signature": signature,
            "result": result,
            **evidence,
        }
        self.execution_state = evidence["execution_status"]
        return str(result)

    def run(
        self,
        request: str,
        *,
        request_id: str = "",
        initial_attachments: list[str] | None = None,
        source_tag: str = "",
        task_id: str = "",
        task_epoch: str = "",
        skill_context: dict | None = None,
    ) -> str:
        request = str(request or "").strip()
        if not request:
            raise ValueError("WebAgent request 不可為空")
        self.run_id = str(request_id or "").strip() or (
            "WA-" + uuid.uuid4().hex[:12].upper()
        )
        from agent_core.request_ownership import digest_intent
        self.task_id = str(task_id or self.run_id)
        self.task_epoch = str(task_epoch or self.run_id)
        self.intent_digest = digest_intent(request)
        self.task_request = request
        self.progress_ledger = initialize_progress(
            self.task_id,
            request_id=self.run_id,
            goal=request,
            root=self.progress_root,
        )
        self.turn_id = 0
        self.terminal_outcome = "UNKNOWN"
        self.execution_state = "NOT_STARTED"
        self.protocol_state = "ACTIVE"
        self.terminal_candidate = {}
        last_no_action_decision_signature: str | None = None
        last_rejected_exchange_signature: str | None = None
        self.pending_result_ack_id = ""
        self.pending_web_ack_id = ""
        self.seen_web_ack_ids.clear()
        self.accepted_ack_ids.clear()
        self.sent_attachment_paths.clear()
        self.action_ledger.clear()
        self.action_result_ledger.clear()
        self.v8_admitted_actions.clear()
        self.active_stage_manifest = None
        self.tools.current_task_id = self.task_id
        self.tools.current_task_epoch = self.task_epoch
        self.tools.current_intent_digest = self.intent_digest
        authorized = extract_authorized_paths(request)
        self.authorized_paths = list(authorized)
        self.tools.begin_run(self.run_id, request, authorized)
        self.image_delivery_plan = plan_image_delivery(
            request,
            workspace=self.workspace,
            interface_name=self.tools.interface_name,
            source_tag=source_tag,
            request_id=self.run_id,
        )
        self.tools.artifact_delivery_mode = str(self.image_delivery_plan.get("delivery", ""))
        if initial_attachments:
            queued = self.tools.queue_attachments(list(initial_attachments))
            if "[WEBAGENT_ATTACHMENT_ERROR]" in queued:
                raise RuntimeError(queued)
        self._emit("request_started", request_id=self.run_id, request=request, authorized_paths=authorized)
        print(f"[{self.display_name}] Request ID: {self.run_id}", flush=True)

        source = str(source_tag or "").strip().upper()
        source_block = (
            f"[WEBAGENT_REQUEST_SOURCE]\n{source}\n[/WEBAGENT_REQUEST_SOURCE]\n"
            if source else ""
        )
        skill_payload = dict(skill_context or {})
        if skill_payload:
            active_skills = {
                "selected_skills": [str(skill_payload.get("name", ""))],
                "scope": "request_only",
                "delivery": str(skill_payload.get("delivery", "")),
                "bundle": str(skill_payload.get("bundle", "")),
                "sha256": str(skill_payload.get("sha256", "")),
                "sources": list(skill_payload.get("sources") or []),
            }
            skill_instruction = (
                "The named Markdown attachment contains the authoritative skill "
                "instructions for this request. Read and apply it before deciding "
                "actions. Do not apply any other optional skill."
            )
        else:
            active_skills = {"selected_skills": [], "scope": "request_only"}
            skill_instruction = (
                "No optional skill is selected for this request. Do not carry an "
                "optional skill forward from an earlier request in this conversation."
            )
        skill_block = (
            "[WEBAGENT_ACTIVE_SKILLS]\n"
            + json.dumps(active_skills, ensure_ascii=False, separators=(",", ":"))
            + "\n"
            + skill_instruction
            + "\n[/WEBAGENT_ACTIVE_SKILLS]\n"
        )
        prompt = (
            source_block + skill_block + "[WEBAGENT_USER_REQUEST]\n"
            + request
            + "\n[/WEBAGENT_USER_REQUEST]\n"
            + f"[WEBAGENT_WORKSPACE]\n{self.workspace}\n[/WEBAGENT_WORKSPACE]"
        )
        if self.image_delivery_plan:
            prompt += (
                "\n[WEBAGENT_IMAGE_DELIVERY_PLAN]\n"
                "Generate exactly one image and wait for the media UI to finish. Do not generate a second "
                "image and do not emit a phase-2 download envelope. After a request-scoped fresh image is "
                "stable, the software runtime will stage and deliver the PNG through its validated action path.\n"
                f"delivery={self.image_delivery_plan['delivery']}\n"
                "[/WEBAGENT_IMAGE_DELIVERY_PLAN]"
            )
        prompt += "\n" + lazy_context_sync_prompt(request, self.workspace)
        prompt += "\n" + format_prompt_context(self.progress_ledger)
        self._emit(
            "context_sync_deferred",
            request_id=self.run_id,
            context_state="NOT_LOADED",
            selection_owner="WEBGPT",
        )

        for _ in range(self.max_turns):
            self._check_cancelled()
            expected = self._new_commit()
            expected["protocol_mode"] = ACTION_EXECUTION_MODE
            expected["narrative_recovery_context"] = self._narrative_recovery_context(prompt)
            draft_base = self.progress_root if self.progress_root is not None else self.workspace
            expected["narrative_draft_root"] = str(
                Path(draft_base) / "localdata" / "runtime" / "narrative_drafts"
            )
            print(
                f"[{self.display_name}] {self.run_id} round={self.turn_id} attempt=1 "
                f"previous_ack_id={self.pending_web_ack_id or '-'}",
                flush=True,
            )
            self._emit(
                "planner_round_started",
                request_id=self.run_id,
                round=self.turn_id,
                attempt=1,
                previous_ack_id=self.pending_web_ack_id,
            )
            attachments = self.tools.take_pending_attachments()
            try:
                response = self.planner(self._with_commit(prompt, expected), expected, attachments)
            except BaseException as exc:
                self.protocol_state = "INTERRUPTED"
                self._emit(
                    "protocol_transport_interrupted",
                    request_id=self.run_id,
                    round=self.turn_id,
                    execution_state=self.execution_state,
                    protocol_state=self.protocol_state,
                    terminal_candidate=dict(self.terminal_candidate),
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            self._check_cancelled()
            self.sent_attachment_paths.extend(
                path for path in attachments if path not in self.sent_attachment_paths
            )
            self._emit(
                "planner_response_received",
                request_id=self.run_id,
                round=self.turn_id,
                response=response,
            )
            calls, v8_parse_diagnostics = parse_v9_tool_transport(response)
            diagnostics = [
                {
                    "marker": "[WEBAGENT_V8_PARSE_ERROR]",
                    "reason": item.get("reason", "v8_parse_error"),
                    "detail": item.get("detail", ""),
                    "suggestion": "修正 compact v9 transport 後重送同一決策。",
                }
                for item in v8_parse_diagnostics
            ]
            if diagnostics:
                self.protocol_state = "REJECTED"
                rejection_signature = self._rejected_exchange_signature(
                    calls, diagnostics, response,
                )
                if rejection_signature == last_rejected_exchange_signature:
                    reason = "semantic_stagnation:repeated_protocol_rejection"
                    set_runtime_state(self.task_id, "PAUSED", reason=reason, root=self.progress_root)
                    raise RuntimeError(
                        "WebAgent 連續回傳相同的無效協議內容，Runtime 已暫停重複問答"
                    )
                last_rejected_exchange_signature = rejection_signature
                self._emit("protocol_rejected", request_id=self.run_id, round=self.turn_id, diagnostics=diagnostics)
                prompt = (
                    "[WEBAGENT_PROTOCOL_REJECTED]\n"
                    + format_tool_parse_diagnostics(diagnostics)
                    + "\n修正 transport/schema 後重送同一決策；不得假設 action 已執行。"
                )
                continue

            actions, ack_diagnostics = self._accept_ack(calls, expected)
            if ack_diagnostics:
                self.protocol_state = "REJECTED"
                self._emit(
                    "protocol_rejected",
                    request_id=self.run_id,
                    round=self.turn_id,
                    diagnostics=ack_diagnostics,
                )
                rejection_signature = self._rejected_exchange_signature(
                    calls, ack_diagnostics, response,
                )
                if rejection_signature == last_rejected_exchange_signature:
                    reason = "semantic_stagnation:repeated_progress_contract_rejection"
                    set_runtime_state(self.task_id, "PAUSED", reason=reason, root=self.progress_root)
                    raise RuntimeError(
                        "WebAgent 連續回傳相同且不符合完成契約的決策，Runtime 已暫停重複問答"
                    )
                last_rejected_exchange_signature = rejection_signature
                repair_note = ""
                if any(item.get("reason") == "MISSING_OR_INVALID_FIELD" for item in ack_diagnostics):
                    repair_note = (
                        "\n這是 FIELD_REPAIR：保留原 tool、action_id 與所有已提供欄位，只補 diagnostic 指定欄位。"
                    )
                prompt = (
                    "[WEBAGENT_ACK_REJECTED]\n"
                    + format_tool_parse_diagnostics(ack_diagnostics)
                    + "\n修正 compact v9 response 後重送同一決策；不得重做尚未執行的 action。"
                    + repair_note
                )
                continue
            last_rejected_exchange_signature = None

            progress_action = next(action for action in actions if action.get("tool") == "report_progress")
            self.progress_ledger = record_model_progress(
                self.task_id,
                progress_action,
                request_id=self.run_id,
                goal=request,
                round_id=self.turn_id,
                root=self.progress_root,
            )
            if len(actions) == 1:
                NarrativeDecisionBridge.mark_terminal_result(
                    action_id=str(progress_action.get("action_id", "")),
                    root=expected["narrative_draft_root"],
                    result=(
                        f"step={self.progress_ledger.current_step}/"
                        f"{self.progress_ledger.total_steps}"
                    ),
                    state="PROGRESS_RECORDED",
                )
            self._emit(
                "task_progress_updated",
                request_id=self.run_id,
                task_id=self.task_id,
                round=self.turn_id,
                current_step=self.progress_ledger.current_step,
                total_steps=self.progress_ledger.total_steps,
                current_focus=self.progress_ledger.current_focus,
                next_action=self.progress_ledger.next_action,
                decision=self.progress_ledger.decision,
                outcome=self.progress_ledger.outcome,
                matched_condition=self.progress_ledger.matched_condition,
                evidence_refs=self.progress_ledger.evidence_refs,
            )
            actions = [action for action in actions if action.get("tool") != "report_progress"]

            if self.progress_ledger.decision == "INTERRUPT":
                content = self.progress_ledger.decision_reason
                if len(actions) == 1 and actions[0].get("tool") == "final_response":
                    content = str(actions[0].get("content", "") or content)
                set_runtime_state(
                    self.task_id,
                    "INTERRUPTED",
                    reason=self.progress_ledger.decision_reason,
                    root=self.progress_root,
                )
                from agent_core.request_ownership import release_active_request
                release_active_request(self.run_id)
                self._emit(
                    "protocol_loop_interrupted",
                    request_id=self.run_id,
                    round=self.turn_id,
                    reason=self.progress_ledger.decision_reason,
                    matched_condition=self.progress_ledger.matched_condition,
                    evidence_refs=self.progress_ledger.evidence_refs,
                )
                raise RuntimeError("WebAgent 模型決策中斷：" + content)

            if len(actions) == 1 and actions[0].get("tool") == "final_response":
                if float(self.progress_ledger.current_step) < float(self.progress_ledger.total_steps):
                    prompt = (
                        "[WEBAGENT_PROGRESS_COMPLETION_GATE]\n"
                        "final_response 被拒絕：Progress 尚未到最後階段。請根據目標、Base 與目前結果繼續 action，"
                        "或先如實更新已完成的最後階段。\n"
                        + format_prompt_context(self.progress_ledger)
                    )
                    continue
                verification_status = str(self.tools.last_verification_status or "").upper()
                self.terminal_outcome = self.progress_ledger.outcome
                self.protocol_state = "CLOSED"
                content = str(actions[0].get("content", ""))
                NarrativeDecisionBridge.mark_terminal_result(
                    action_id=str(actions[0].get("action_id", "")),
                    root=expected["narrative_draft_root"],
                    result=content,
                    state="COMPLETED",
                )
                set_runtime_state(self.task_id, "COMPLETED", root=self.progress_root)
                from agent_core.request_ownership import release_active_request
                release_active_request(self.run_id)
                self._emit(
                    "protocol_loop_completed",
                    request_id=self.run_id,
                    round=self.turn_id,
                    ack_id=self.pending_web_ack_id,
                    task_outcome=self.terminal_outcome,
                    verification_status=verification_status or None,
                    final_content=content,
                )
                return content

            if not actions:
                decision_signature = json.dumps(
                    {
                        "current_step": self.progress_ledger.current_step,
                        "total_steps": self.progress_ledger.total_steps,
                        "steps": [
                            {
                                "step": item.get("step"),
                                "status": item.get("status"),
                            }
                            for item in self.progress_ledger.steps
                        ],
                        "decision": self.progress_ledger.decision,
                        "outcome": self.progress_ledger.outcome,
                        "matched_condition": self.progress_ledger.matched_condition,
                        "evidence_refs": self.progress_ledger.evidence_refs,
                        "operational_tools": [],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if decision_signature == last_no_action_decision_signature:
                    reason = "semantic_stagnation:repeated_model_decision_without_new_runtime_evidence"
                    set_runtime_state(
                        self.task_id, "PAUSED", reason=reason, root=self.progress_root
                    )
                    self._emit(
                        "protocol_loop_paused",
                        request_id=self.run_id,
                        round=self.turn_id,
                        reason=reason,
                        repeated_progress_signature=decision_signature,
                    )
                    raise RuntimeError(
                        "WebAgent 持續回傳相同 Progress 決策且沒有新的 Runtime evidence 或 action；Runtime 已暫停"
                    )
                last_no_action_decision_signature = decision_signature
            else:
                last_no_action_decision_signature = None

            results = [{
                "action_id": progress_action.get("action_id", ""),
                "tool": "report_progress",
                "result": (
                    "[TASK_PROGRESS_RECORDED] "
                    f"step={self.progress_ledger.current_step}/{self.progress_ledger.total_steps}"
                ),
            }]
            def execute_with_events(action: dict) -> str:
                self._check_cancelled()
                scope = self.tools.preview_tool_scope(action)
                self._emit(
                    "tool_started",
                    request_id=self.run_id,
                    round=self.turn_id,
                    action_id=action.get("action_id", ""),
                    tool=action.get("tool", ""),
                    requested_paths=scope.get("requested_paths", []),
                    resolved_paths=scope.get("resolved_paths", []),
                    scope_source=scope.get("scope_source", "none"),
                    scope_allowed=scope.get("allowed", True),
                    scope_error=scope.get("error", ""),
                )
                resolved_scope = ", ".join(scope.get("resolved_paths", [])) or "(none)"
                scope_suffix = (
                    f" resolved_scope={resolved_scope}"
                    if scope.get("allowed", True)
                    else f" scope_rejected={scope.get('error', '')}"
                )
                print(
                    f"[{self.display_name}] {self.run_id} round={self.turn_id} 正在執行 "
                    f"{action.get('tool', '')} ({action.get('action_id', '')})...{scope_suffix}",
                    flush=True,
                )
                result = self._execute_action(action)
                if action.get("tool") == "query_project":
                    try:
                        query_payload = json.loads(result)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        query_payload = {}
                    index_context = dict(query_payload.get("index_context") or {})
                    self._emit(
                        "project_access_preflight",
                        request_id=self.run_id,
                        round=self.turn_id,
                        action_id=action.get("action_id", ""),
                        status=query_payload.get("status", "UNKNOWN"),
                        error=query_payload.get("error", ""),
                        requested_root=index_context.get(
                            "requested_root", query_payload.get("project_root", "")
                        ),
                        effective_root=index_context.get("effective_root", ""),
                        requested_snapshot_id=index_context.get("requested_snapshot_id", ""),
                        effective_snapshot_id=index_context.get("effective_snapshot_id", ""),
                        recovery_action=index_context.get(
                            "recovery_action", query_payload.get("recovery_action", "NONE")
                        ),
                        normalized_query_count=index_context.get("normalized_query_count", 0),
                    )
                NarrativeDecisionBridge.mark_terminal_result(
                    action_id=str(action.get("action_id", "")),
                    root=expected["narrative_draft_root"],
                    result=result,
                )
                self._check_cancelled()
                admitted = self.v8_admitted_actions.get(str(action.get("action_id", "")))
                if admitted is not None:
                    action_result_id = "RES-ACTION-" + uuid.uuid4().hex[:12].upper()
                    action_evidence = self._classify_action_evidence(action, result)
                    ledger_entry = v8_build_result(
                        admitted, action_result_id, "COMMITTED", result,
                    )
                    ledger_entry.update(action_evidence)
                    self.action_result_ledger[str(action.get("action_id", ""))] = ledger_entry
                    self._emit(
                        "action_evidence_recorded",
                        request_id=self.run_id,
                        round=self.turn_id,
                        action_id=action.get("action_id", ""),
                        **action_evidence,
                    )
                self._emit(
                    "tool_completed",
                    request_id=self.run_id,
                    round=self.turn_id,
                    action_id=action.get("action_id", ""),
                    tool=action.get("tool", ""),
                    result=result,
                )
                return result
            if self.active_stage_manifest is not None and WEBAGENT_STAGED_PROTOCOL == "on":
                stage_result = execute_stage(self.active_stage_manifest, actions, execute_with_events, workspace=str(self.workspace))
                for action in actions:
                    outcome = stage_result["actions"].get(str(action.get("action_id", "")), {})
                    results.append({
                        "action_id": action.get("action_id", ""),
                        "tool": action.get("tool", ""),
                        "stage_status": outcome.get("status", "FAILED"),
                        "result": outcome.get("result", outcome.get("error", "")),
                    })
            else:
                for action in actions:
                    result = execute_with_events(action)
                    results.append({
                    "action_id": action.get("action_id", ""),
                    "tool": action.get("tool", ""),
                    "result": result,
                    })
            result_id = "RES-" + uuid.uuid4().hex[:12].upper()
            self.pending_result_ack_id = result_id
            serialized_results = json.dumps(results, ensure_ascii=False, separators=(",", ":"))
            if utf8_size(serialized_results) > ROUND_INLINE_MAX_BYTES:
                compact = prepare_tool_result(
                    {"tool": "round_results", "action_id": f"ROUND-{self.turn_id}", "full_result_required": True},
                    serialized_results,
                    self.tools,
                    force_attachment=True,
                )
                results = [{"action_id": f"ROUND-{self.turn_id}", "tool": "round_results", "result": compact}]
            prompt = (
                f"[WEBAGENT_TOOL_RESULTS]\nRUN_ID={self.run_id}\nRESULT_ID={result_id}\n"
                + json.dumps(results, ensure_ascii=False, separators=(",", ":"))
                + "\n[/WEBAGENT_TOOL_RESULTS]\n"
                + "根據結果與已接受的 Progress 決定下一步；下一輪 turn_commit 必須 ACK 此 RESULT_ID。\n"
                + format_prompt_context(self.progress_ledger)
            )
        raise RuntimeError(f"WebAgent 超過最大 protocol turns: {self.max_turns}")
