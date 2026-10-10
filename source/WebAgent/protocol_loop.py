#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Independent WebAgent protocol/result/ACK loop."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_core.smartagent_protocol import (
    PROJECT_EVIDENCE_ACTION_CONTRACT,
    format_tool_parse_diagnostics,
    validate_tool_envelope,
)
from agent_core.protocol_v9 import (
    ProtocolV8Error,
    SINGLE_FENCE_TRANSPORT_CONTRACT,
    V8RequestContext,
    action_digest as v8_action_digest,
    admit_action as v8_admit_action,
    build_result as v8_build_result,
    parse_v9_tool_transport,
    validate_model_commit as v8_validate_model_commit,
)
from agent_core.result_exchange import prepare_tool_result
from agent_core.narrative_bridge import NarrativeDecisionBridge
from agent_core.capability_recovery import (
    build_evidence_to_action_route,
    build_result_evidence_to_action_route,
    render_evidence_to_action_guidance,
    render_result_evidence_to_action_guidance,
)
from agent_core.recovery_protocol import ACTION_EXECUTION_MODE
from agent_core.payload_budget import ROUND_INLINE_MAX_BYTES, utf8_size
from agent_core.routing import lazy_context_sync_prompt
from agent_core.image_delivery import plan_image_delivery
from agent_core.stage_executor import execute_stage, preflight_stage
from agent_core.stage_protocol import StageManifestError, validate_stage_manifest
from agent_core.task_progress import (
    TaskProgressError,
    completion_condition_choices,
    format_prompt_context,
    initialize_progress,
    record_model_progress,
    set_runtime_state,
    validate_model_progress,
)
from agent_core.action_loop_state import (
    build_action_loop_state,
    classify_evidence_state,
    phase_for_action,
    render_action_loop_state,
    semantic_action_signature,
)
from agent_core.command_operation import operation_terminal_eligible

from .protocol import SUPPORTED_ACTION_TOOLS, render_initial_planner_toolkit
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
        self.condition_result_ledger: dict[str, dict] = {}
        self.v8_admitted_actions: dict[str, dict] = {}
        self.pending_progress_repair_actions: list[dict] = []
        self.active_stage_manifest: dict | None = None
        self.image_delivery_plan: dict = {}
        self.authorized_paths: list[str] = []
        self.pending_verification_requirement: dict = {}
        self.pending_result_recovery: dict = {}
        self.execution_state = "NOT_STARTED"
        self.protocol_state = "IDLE"
        self.terminal_candidate: dict = {}
        self.action_loop_state = build_action_loop_state()

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
            "matched_condition_id": str(
                getattr(ledger, "matched_condition_id", "") or ""
            ),
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
            + "只能輸出 compact v9 smartagent_tool transport，不得輸出 blocks 以外的自然語言。\n"
            + SINGLE_FENCE_TRANSPORT_CONTRACT
            + "\n"
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
                "effective_verification_status": str(
                    item.get("effective_verification_status", item.get("verification_status", "UNKNOWN"))
                    or "UNKNOWN"
                ),
                "effective_verification_id": str(item.get("effective_verification_id", "") or ""),
                "expectation_status": str(item.get("expectation_status", "") or ""),
                "verifies_action_id": str(item.get("verifies_action_id", "") or ""),
                "condition_id": str(item.get("condition_id", "") or ""),
            })
        payload = {
            "available_refs": sorted(self._runtime_evidence_refs()),
            "verification_status": self._effective_verification_status(),
            "legacy_last_verification_status": self.tools.last_verification_status,
            "execution_state": self.execution_state,
            "protocol_state": self.protocol_state,
            "action_evidence": evidence[-16:],
            "condition_evidence": list(self.condition_result_ledger.values())[-16:],
        }
        return (
            "[RUNTIME_EVIDENCE]\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n[/RUNTIME_EVIDENCE]"
            + "\n"
            + render_action_loop_state(self.action_loop_state)
        )

    def _progress_state_payload(self) -> dict:
        ledger = self.progress_ledger
        if ledger is None:
            return {}
        return {
            "decision": str(getattr(ledger, "decision", "CONTINUE") or "CONTINUE"),
            "outcome": str(getattr(ledger, "outcome", "PENDING") or "PENDING"),
            "current_step": getattr(ledger, "current_step", 0),
            "total_steps": getattr(ledger, "total_steps", 0),
            "next_action": str(getattr(ledger, "next_action", "") or ""),
        }

    def _refresh_action_loop_state(self) -> dict:
        action: dict | None = None
        result = ""
        execution_status = "UNKNOWN"
        verification_status = "UNKNOWN"
        expectation_status = ""
        action_records: list[dict] = []
        if self.action_ledger:
            action_id = next(reversed(self.action_ledger))
            entry = dict(self.action_ledger.get(action_id) or {})
            action = dict(entry.get("action") or {})
            result = str(entry.get("result", "") or "")
            execution_status = str(entry.get("execution_status", "UNKNOWN") or "UNKNOWN")
            evidence = dict(self.action_result_ledger.get(action_id) or {})
            verification_status = str(
                evidence.get(
                    "effective_verification_status",
                    evidence.get("verification_status", entry.get("verification_status", "UNKNOWN")),
                )
                or "UNKNOWN"
            )
            expectation_status = str(evidence.get("expectation_status", "") or "")
            for recorded_id, recorded_entry_value in self.action_ledger.items():
                recorded_entry = dict(recorded_entry_value or {})
                recorded_action = dict(recorded_entry.get("action") or {})
                recorded_evidence = dict(self.action_result_ledger.get(recorded_id) or {})
                action_records.append({
                    "action": recorded_action,
                    "result": str(recorded_entry.get("result", "") or ""),
                    "execution_status": str(
                        recorded_entry.get("execution_status", "UNKNOWN") or "UNKNOWN"
                    ),
                    "verification_status": str(
                        recorded_evidence.get(
                            "effective_verification_status",
                            recorded_evidence.get(
                                "verification_status",
                                recorded_entry.get("verification_status", "UNKNOWN"),
                            ),
                        )
                        or "UNKNOWN"
                    ),
                    "expectation_status": str(
                        recorded_evidence.get(
                            "expectation_status", recorded_entry.get("expectation_status", "")
                        ) or ""
                    ),
                })
        self.action_loop_state = build_action_loop_state(
            transport_state=self.protocol_state,
            progress=self._progress_state_payload(),
            action=action,
            result=result,
            execution_status=execution_status,
            verification_status=verification_status,
            expectation_status=expectation_status,
            pending_recovery=self.pending_result_recovery,
            pending_verification=self.pending_verification_requirement,
            action_records=action_records,
        )
        return dict(self.action_loop_state)

    @staticmethod
    def _selected_action_label(actions: list[dict]) -> str:
        tools = [str(item.get("tool", "") or "") for item in actions]
        if not tools:
            return "none"
        if len(tools) == 1:
            return tools[0]
        return "batch:" + ",".join(tools)

    @staticmethod
    def _selected_next_phase(actions: list[dict], current_phase: str) -> str:
        phases = [phase_for_action(item) for item in actions]
        for candidate in ("TERMINAL", "VERIFY", "EXECUTE", "PLAN", "DISCOVERY"):
            if candidate in phases:
                return candidate
        return str(current_phase or "DISCOVERY")

    def _normalize_action_loop_decision(
        self, progress: dict, operational: list[dict],
    ) -> dict | None:
        """Bind unambiguous state fields and reject only semantic conflicts."""
        state = dict(self.action_loop_state or {})
        state_id = str(state.get("state_id", "") or "")
        # These fields describe Runtime facts already present in the same turn.
        # Never ask the model to echo them and never reject a valid action because
        # the model copied an old or semantically different value.
        progress["runtime_state_ref"] = state_id

        actual_action = self._selected_action_label(operational)
        progress["selected_action"] = actual_action

        actual_phase = self._selected_next_phase(
            operational, str(state.get("phase", "DISCOVERY") or "DISCOVERY"),
        )
        progress["next_phase"] = actual_phase

        transition = str(state.get("required_transition", "CONTINUE_PLAN") or "CONTINUE_PLAN")
        allowed = {str(item) for item in list(state.get("allowed_next_actions") or []) if str(item)}
        actual_tools = [str(item.get("tool", "") or "") for item in operational]
        if transition in {"PREREQUISITE_REQUIRED", "VERIFY_REQUIRED", "REPLAN_REQUIRED"}:
            # Progress-only replies are admitted here so the existing bounded
            # dead-end/verification recovery can issue one complete prompt and
            # then pause.  Do not turn a missing action into repeated field-level
            # ACK repair.  When an action is present it must follow the state.
            if actual_tools and any(tool not in allowed for tool in actual_tools):
                return {
                    "marker": "[ACTION_LOOP_STATE_REJECTED]",
                    "reason": "required_transition_action_missing",
                    "detail": json.dumps({
                        "required_transition": transition,
                        "allowed_next_actions": sorted(allowed),
                        "actual_tools": actual_tools,
                    }, ensure_ascii=False, separators=(",", ":")),
                    "suggestion": "依 Runtime 狀態一次輸出一個允許的 prerequisite/verification action。",
                }
        blocked_tools = {
            str(item) for item in list(state.get("blocked_actions") or []) if str(item)
        }
        if blocked_tools and any(tool in blocked_tools for tool in actual_tools):
            return {
                "marker": "[ACTION_LOOP_STATE_REJECTED]",
                "reason": "blocked_action_selected",
                "detail": "blocked=" + json.dumps(
                    sorted(blocked_tools), ensure_ascii=False, separators=(",", ":"),
                ) + ";actual=" + json.dumps(actual_tools, separators=(",", ":")),
                "suggestion": "先執行 Runtime 提供的 prerequisite action；在證據到齊前不得重送 blocked action。",
            }
        if transition == "TERMINAL" and actual_tools != ["final_response"]:
            return {
                "marker": "[ACTION_LOOP_STATE_REJECTED]",
                "reason": "terminal_transition_requires_final_response",
                "detail": "actual_tools=" + json.dumps(actual_tools, separators=(",", ":")),
                "suggestion": "TERMINAL 狀態只輸出 terminal Progress 與 final_response。",
            }
        blocked_signatures = {
            str(item) for item in list(state.get("blocked_action_signatures") or []) if str(item)
        }
        repeated = [
            item for item in operational
            if semantic_action_signature(item) in blocked_signatures
        ]
        if repeated:
            return {
                "marker": "[ACTION_LOOP_STATE_REJECTED]",
                "reason": "blocked_semantic_action_repeated",
                "detail": "tools=" + json.dumps(
                    [str(item.get("tool", "") or "") for item in repeated], separators=(",", ":"),
                ),
                "suggestion": "前一 action 未增加 evidence；改變計畫或使用 materially different 的精確 action。",
            }
        return None

    @staticmethod
    def _has_structured_verification_action(actions: list[dict]) -> bool:
        """Return whether this round can produce a new PASS/FAIL state."""
        for action in actions:
            tool = str(action.get("tool", "") or "")
            if tool == "aggregate_verification":
                commands = action.get("commands")
                if isinstance(commands, list) and commands and all(
                    isinstance(item, str) and item.strip() for item in commands
                ):
                    return True
                continue
            if tool != "run_command":
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
    def _run_command_verification_detail(action: dict) -> str:
        """Describe a malformed verification contract without calling it missing."""
        issues: list[str] = []
        if not str(action.get("success_criteria", "") or "").strip():
            issues.append("missing=success_criteria")
        verify = action.get("verify")
        if verify is None or verify == []:
            issues.append("missing=verify")
        else:
            checks = list(verify) if isinstance(verify, list) else ([verify] if isinstance(verify, dict) else [])
            if not checks:
                issues.append(f"invalid=verify:expected_object_or_list_of_objects;actual={type(verify).__name__}")
            else:
                for index, item in enumerate(checks):
                    if not isinstance(item, dict) or not item:
                        issues.append(
                            f"invalid=verify[{index}]:expected_non_empty_object;actual={type(item).__name__}"
                        )
        return ";".join(issues)

    @staticmethod
    def _contains_unquoted_shell_operator(command: str, operator: str) -> bool:
        quote = ""
        escaped = False
        index = 0
        while index < len(command):
            char = command[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if char == "`":
                escaped = True
                index += 1
                continue
            if quote:
                if char == quote:
                    quote = ""
                index += 1
                continue
            if char in {"'", '"'}:
                quote = char
                index += 1
                continue
            if command.startswith(operator, index):
                return True
            index += 1
        return False

    @classmethod
    def _run_command_shell_mismatch_detail(cls, action: dict) -> str:
        """Reject unambiguous CMD syntax before the PowerShell executor runs it."""
        command = str(action.get("command", "") or "").strip()
        if not command or re.match(r"(?i)^cmd(?:\.exe)?\s+/(?:c|k)\b", command):
            return ""
        detected: list[str] = []
        if re.search(r"(?i)(?:^|[;&|]\s*)cd\s+/d(?:\s|$)", command):
            detected.append("cmd_cd_d")
        if cls._contains_unquoted_shell_operator(command, "&&"):
            detected.append("cmd_and_operator")
        if cls._contains_unquoted_shell_operator(command, "||"):
            detected.append("cmd_or_operator")
        if not detected:
            return ""
        return json.dumps(
            {
                "executor": "Windows PowerShell 5.1",
                "detected": detected,
                "command": command,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _classify_action_evidence(action: dict, result: str) -> dict:
        """Separate execution facts from postcondition verification facts."""
        text = str(result or "")
        tool = str(action.get("tool", "") or "")
        verification_match = re.search(
            r"(?m)^VERIFICATION_STATUS:\s*(PASS|FAIL|UNVERIFIED|SPEC_INVALID)\s*$", text,
        )
        verification = verification_match.group(1) if verification_match else "UNKNOWN"
        expectation_match = re.search(
            r"(?m)^EXPECTATION_STATUS:\s*(PASS|FAIL|SPEC_INVALID)\s*$", text,
        )
        expectation = expectation_match.group(1) if expectation_match else ""
        try:
            structured = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            structured = {}
        structured_status = (
            str(structured.get("status", "") or "").upper()
            if isinstance(structured, dict) else ""
        )
        if tool in {"aggregate_verification", "execute_frozen_plan"}:
            if structured_status == "PASS":
                verification = "PASS"
            elif structured_status in {"FAIL", "FAILED", "ROLLED_BACK", "ERROR"}:
                verification = "FAIL"
        execution = "UNKNOWN" if not text.strip() else "SUCCEEDED"
        exit_match = re.search(r"(?m)^exit_code:\s*([^\r\n]+)", text)
        if exit_match:
            raw_exit = exit_match.group(1).strip()
            execution = "SUCCEEDED" if raw_exit == "0" else "FAILED"
        elif any(marker in text for marker in (
            "[SECURITY_COMMAND_REJECTED]", "[TOOL_SCOPE_REJECTED]",
            "[WEBAGENT_TOOL_REJECTED]", "[WEBAGENT_EVIDENCE_ROUTE_REQUIRED]",
            "[PROTOCOL_ERROR]",
        )):
            execution = "FAILED"
        elif tool == "query_project":
            query_payload = structured if isinstance(structured, dict) else {}
            if str(query_payload.get("status", "") or "").upper() in {"REJECTED", "ERROR", "FAILED"}:
                execution = "FAILED"
        elif structured_status in {"REJECTED", "ERROR", "FAILED"} and tool not in {
            "aggregate_verification", "execute_frozen_plan",
        }:
            execution = "FAILED"
        evidence_state, _missing = classify_evidence_state(
            action, text, execution, verification, expectation,
        )
        operation = str(action.get("operation", "") or "").upper()
        terminal_eligible = True
        if tool == "run_command":
            try:
                terminal_eligible = (
                    expectation == "PASS"
                    if expectation
                    else operation_terminal_eligible(operation, verification)
                )
            except ValueError:
                terminal_eligible = False
        return {
            "tool": tool,
            "operation": operation,
            "execution_status": execution,
            "verification_status": verification,
            "expectation_status": expectation,
            "evidence_state": evidence_state,
            "terminal_eligible": terminal_eligible,
        }

    @staticmethod
    def _entry_verification_status(item: dict) -> str:
        expectation = str(
            item.get("effective_expectation_status", item.get("expectation_status", "")) or ""
        ).upper()
        if expectation in {"PASS", "FAIL", "SPEC_INVALID"}:
            return expectation
        return str(
            item.get("effective_verification_status", item.get("verification_status", "UNKNOWN"))
            or "UNKNOWN"
        ).upper()

    @staticmethod
    def _canonical_condition_id(condition: object) -> str:
        text = str(condition or "").strip()
        if not text:
            return ""
        return "COND-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:20].upper()

    def _resolve_effective_evidence_entries(
        self,
        evidence_refs: list[str] | set[str] | tuple[str, ...] | None,
    ) -> list[tuple[str, dict]]:
        """Collapse verifier history onto the action's current effective result.

        Explicit evidence refs commonly include both an action and every VERIFY
        action used to test it.  A superseded verifier remains useful audit
        history, but it must not independently poison the terminal verdict once
        the target action points at a newer effective verification.

        A verifier is collapsed only when its verification record is present in
        the target's history.  Broken or unrelated verifier references therefore
        retain their own execution/result state and cannot be hidden by a prior
        target PASS.
        """
        resolved: dict[str, dict] = {}
        for ref in [str(item) for item in list(evidence_refs or [])]:
            entry = self.action_result_ledger.get(ref)
            if entry is None:
                continue
            effective_ref = ref
            effective_entry = entry
            target_id = str(entry.get("verifies_action_id", "") or "").strip()
            target = self.action_result_ledger.get(target_id) if target_id else None
            verification_id = str(
                entry.get("verification_id", entry.get("effective_verification_id", "")) or ""
            ).strip()
            if target is not None and verification_id:
                target_history_ids = {
                    str(item.get("verification_id", "") or "").strip()
                    for item in list(target.get("verification_history") or [])
                    if isinstance(item, dict)
                }
                if verification_id in target_history_ids:
                    effective_ref = target_id
                    effective_entry = target
            resolved[effective_ref] = effective_entry
        return list(resolved.items())

    def _effective_verification_status(
        self,
        evidence_refs: list[str] | set[str] | tuple[str, ...] | None = None,
        *,
        matched_condition: str = "",
    ) -> str:
        """Resolve terminal verification from request-scoped action evidence.

        A compatibility fallback to ``last_verification_status`` is used only
        when no referenced action evidence exists.  Once action evidence is
        available, an unrelated later PASS cannot erase a referenced FAIL.
        """
        refs = [str(item) for item in list(evidence_refs or [])]
        if refs:
            entries = [item for _action_id, item in self._resolve_effective_evidence_entries(refs)]
        else:
            # A verifier copies its newest status onto the target action.  Do
            # not count the verifier entry again during the request-wide
            # summary or an obsolete verifier FAIL can poison a later PASS.
            entries = [
                item for item in self.action_result_ledger.values()
                if not str(item.get("verifies_action_id", "") or "").strip()
            ]
            if not entries and self.action_result_ledger:
                entries = [next(reversed(self.action_result_ledger.values()))]
        condition = str(matched_condition or "").strip()
        canonical_condition_id = self._canonical_condition_id(condition)
        condition_entry = self.condition_result_ledger.get(canonical_condition_id)
        if condition_entry and (
            not refs or str(condition_entry.get("action_id", "") or "") in refs
        ):
            status = self._entry_verification_status(condition_entry)
            if status in {"PASS", "FAIL", "UNVERIFIED", "SPEC_INVALID"}:
                return status
        if condition:
            scoped = [
                item for item in entries
                if str(item.get("condition_id", "") or "").strip()
                in {condition, canonical_condition_id}
            ]
            if scoped:
                entries = scoped
        statuses = [self._entry_verification_status(item) for item in entries]
        statuses = [item for item in statuses if item in {"PASS", "FAIL", "UNVERIFIED", "SPEC_INVALID"}]
        if not statuses:
            return str(self.tools.last_verification_status or "UNKNOWN").upper()
        if "FAIL" in statuses:
            return "FAIL"
        if "SPEC_INVALID" in statuses:
            return "SPEC_INVALID"
        if "UNVERIFIED" in statuses:
            return "UNVERIFIED"
        return "PASS"

    def _terminal_evidence_verdict(
        self, evidence_refs: list[str] | set[str] | tuple[str, ...] | None,
        *,
        matched_condition: str = "",
    ) -> str:
        """Return PASS/FAIL/UNVERIFIED from action-scoped Runtime evidence."""
        refs = [str(item) for item in list(evidence_refs or [])]
        canonical_condition_id = self._canonical_condition_id(matched_condition)
        condition_entry = self.condition_result_ledger.get(canonical_condition_id)
        if condition_entry and str(condition_entry.get("action_id", "") or "") in refs:
            status = self._entry_verification_status(condition_entry)
            if status in {"PASS", "FAIL", "SPEC_INVALID"}:
                return "FAIL" if status in {"FAIL", "SPEC_INVALID"} else "PASS"
        entries = self._resolve_effective_evidence_entries(refs)
        if not entries:
            return "UNVERIFIED"
        passed = False
        unresolved = False
        for action_id, entry in entries:
            execution = str(entry.get("execution_status", "UNKNOWN") or "UNKNOWN").upper()
            verification = self._entry_verification_status(entry)
            expectation = str(entry.get("expectation_status", "") or "").upper()
            if expectation in {"FAIL", "SPEC_INVALID"}:
                return "FAIL"
            if execution == "FAILED" and expectation != "PASS":
                return "FAIL"
            if verification in {"FAIL", "SPEC_INVALID"}:
                return "FAIL"
            if not bool(entry.get("terminal_eligible", True)):
                unresolved = True
                continue
            if verification == "PASS":
                passed = True
                continue
            action = dict((self.action_ledger.get(action_id) or {}).get("action") or {})
            role = phase_for_action(action) if action else "DISCOVERY"
            evidence_state = str(entry.get("evidence_state", "") or "").upper()
            if role in {"DISCOVERY", "PLAN"} and execution == "SUCCEEDED" and evidence_state in {
                "AVAILABLE", "SUFFICIENT",
            }:
                passed = True
            else:
                unresolved = True
        if passed and not unresolved:
            return "PASS"
        return "UNVERIFIED"

    def _record_action_result_evidence(self, action: dict, ledger_entry: dict) -> dict:
        """Append verification history and apply an explicit scoped supersession."""
        action_id = str(action.get("action_id", "") or "")
        verifies_action_id = str(action.get("verifies_action_id", "") or "").strip()
        target = self.action_result_ledger.get(verifies_action_id) if verifies_action_id else None
        condition_id = str(action.get("condition_id", "") or "").strip()
        if not condition_id and target is not None:
            condition_id = str(target.get("condition_id", "") or "").strip()
        pending_condition_id = str(
            (self.pending_verification_requirement or {}).get("condition_id", "") or ""
        ).strip()
        if (
            str(action.get("operation", "") or "").upper() == "VERIFY"
            and target is None
            and pending_condition_id
        ):
            ledger_entry["model_condition_id"] = condition_id
            condition_id = pending_condition_id
        observed_status = str(
            ledger_entry.get("verification_status", "UNKNOWN") or "UNKNOWN"
        ).upper()
        expectation_status = str(ledger_entry.get("expectation_status", "") or "").upper()
        status = expectation_status if expectation_status in {"PASS", "FAIL", "SPEC_INVALID"} else observed_status
        ledger_entry["condition_id"] = condition_id
        ledger_entry["verifies_action_id"] = verifies_action_id
        ledger_entry["effective_verification_status"] = status
        ledger_entry["effective_expectation_status"] = expectation_status
        ledger_entry.setdefault("verification_history", [])
        if status in {"PASS", "FAIL", "UNVERIFIED", "SPEC_INVALID"}:
            verification_id = "VER-" + uuid.uuid4().hex[:12].upper()
            prior_id = str(target.get("effective_verification_id", "") or "") if target else ""
            record = {
                "verification_id": verification_id,
                "action_id": action_id,
                "status": status,
                "observed_verification_status": observed_status,
                "expectation_status": expectation_status,
                "round": self.turn_id,
                "condition_id": condition_id,
                "supersedes_verification_id": prior_id,
            }
            ledger_entry["verification_id"] = verification_id
            ledger_entry["effective_verification_id"] = verification_id
            ledger_entry["verification_history"].append(dict(record))
            if target is not None:
                target.setdefault("verification_history", []).append(dict(record))
                target["effective_verification_id"] = verification_id
                target["effective_verification_status"] = status
        self.action_result_ledger[action_id] = ledger_entry
        if condition_id:
            self.condition_result_ledger[condition_id] = {
                "condition_id": condition_id,
                "action_id": action_id,
                "status": status,
                "verification_status": status,
                "observed_verification_status": observed_status,
                "expectation_status": expectation_status,
                "effective_verification_status": status,
                "execution_status": str(
                    ledger_entry.get("execution_status", "UNKNOWN") or "UNKNOWN"
                ).upper(),
                "terminal_eligible": bool(ledger_entry.get("terminal_eligible", True)),
                "verification_id": str(ledger_entry.get("verification_id", "") or ""),
                "round": self.turn_id,
            }
        return ledger_entry

    def _verification_requirement_diagnostic(
        self,
        normalized_progress: dict,
        *,
        reason: str,
    ) -> dict:
        condition = str(normalized_progress.get("matched_condition", "") or "").strip()
        requirement = dict(self.pending_verification_requirement or {})
        if not requirement:
            condition_id = self._canonical_condition_id(condition)
            requirement = {
                "missing_condition": condition or "取得支持 terminal SUCCESS 的 request-scoped PASS evidence",
                "condition_id": condition_id,
                "current_verification_status": self._effective_verification_status(
                    normalized_progress.get("evidence_refs") or [],
                    matched_condition=condition,
                ),
                "required_action": "run_command",
                "required_fields": ["operation", "command", "success_criteria", "verify"],
                "supported_verify_actions": ["run_command", "aggregate_verification"],
                "supported_verify_checks": ["file_exists", "file_contains", "expect_regex"],
                "evidence_refs": list(normalized_progress.get("evidence_refs") or []),
            }
            self.pending_verification_requirement = dict(requirement)
        detail = json.dumps(requirement, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "marker": "[WEBAGENT_VERIFICATION_REQUIRED]",
            "reason": reason,
            "detail": detail,
            "suggestion": (
                "下一輪必須使用 decision=CONTINUE，並輸出一個 Runtime 列出的 canonical verification action。"
                "使用 run_command 時必須同時包含 operation、command、描述精確成功條件的 success_criteria，以及非空 verify；"
                "補驗證的 operation 固定為 VERIFY；若有原 action，使用 verifies_action_id；"
                "若是直接驗證 completion condition，condition_id 必須使用 detail 中 Runtime 指定的值。"
                "使用 aggregate_verification 時必須提供非空 commands。補驗證時用 verifies_action_id 指向原 action。"
                "run_command.verify 可用 run_command/file_exists/file_contains，並可用 expect_regex 驗證結構化輸出。"
                "Runtime 執行後會回傳 PASS、FAIL 或 SPEC_INVALID；"
                "在取得新狀態前不得只回 report_progress，也不得再次宣告 SUCCESS。"
            ),
        }

    @staticmethod
    def _normalize_report_progress_envelope(
        action: dict, existing: object | None = None,
    ) -> tuple[dict, dict | None]:
        """Accept one unambiguous legacy/narrative ``progress`` wrapper.

        The canonical wire shape keeps Progress fields beside ``tool`` and
        ``action_id``.  Some otherwise valid model replies wrap those fields in
        ``progress``.  Runtime may flatten that shape only when no outer value
        conflicts with the nested value; ambiguity remains fail-closed.
        """
        normalized = dict(action or {})
        # runtime_state is displayed to the model as read-only context.  Models
        # sometimes echo it back; strip it instead of rejecting an otherwise
        # valid progress decision because only Runtime may mutate this field.
        normalized.pop("runtime_state", None)
        if "progress" not in normalized:
            return normalized, None
        nested = normalized.get("progress")
        if isinstance(nested, str) and nested.strip() and existing is not None:
            # A narrative ``progress`` value contains no new semantic decision.
            # Preserve the last accepted ledger and use the text only as the
            # current observation; this is deterministic format normalization,
            # not a model decision invented by Runtime.
            observation = nested.strip()
            normalized.pop("progress", None)
            normalized.setdefault("base_evaluation", str(getattr(existing, "base_evaluation", "") or observation))
            normalized.setdefault("total_steps", getattr(existing, "total_steps", 0))
            normalized.setdefault("current_step", getattr(existing, "current_step", 0))
            normalized.setdefault("steps", list(getattr(existing, "steps", []) or []))
            normalized.setdefault("current_focus", observation)
            normalized.setdefault("next_action", str(getattr(existing, "next_action", "") or ""))
            normalized.setdefault("completion_contract", dict(getattr(existing, "completion_contract", {}) or {}))
            normalized.setdefault("decision", str(getattr(existing, "decision", "CONTINUE") or "CONTINUE"))
            normalized.setdefault("outcome", str(getattr(existing, "outcome", "PENDING") or "PENDING"))
            normalized.setdefault("matched_condition", str(getattr(existing, "matched_condition", "") or ""))
            normalized.setdefault("evidence_refs", list(getattr(existing, "evidence_refs", []) or []))
            normalized.setdefault("decision_reason", observation)
            return normalized, None
        if not isinstance(nested, dict):
            return {}, {
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "progress_wrapper_invalid",
                "detail": "progress must be an object when the compatibility wrapper is used",
                "suggestion": "移除 progress wrapper，將 Progress 欄位直接放在 report_progress 最外層。",
            }
        nested = dict(nested)
        nested.pop("runtime_state", None)
        conflicts = sorted(
            key for key, value in nested.items()
            if key in normalized and key != "progress" and normalized.get(key) != value
        )
        if conflicts:
            return {}, {
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "progress_wrapper_conflict",
                "detail": "conflicting_fields=" + ",".join(conflicts),
                "suggestion": (
                    "外層與 progress wrapper 欄位衝突；不得猜測。移除 wrapper，並在 report_progress "
                    "最外層為每個欄位只保留一個值。"
                ),
            }
        normalized.pop("progress", None)
        for key, value in nested.items():
            normalized.setdefault(key, value)
        return normalized, None

    @staticmethod
    def _progress_capability_guidance(progress: dict) -> str:
        """Return bounded tool guidance when the model incorrectly waits for tools."""
        haystack = "\n".join(
            str(progress.get(key, "") or "")
            for key in (
                "base_evaluation", "current_focus", "next_action", "decision_reason",
            )
        ).casefold()
        blocked_markers = (
            "沒有提供可執行", "沒有可用", "尚無可執行", "等待可用", "等待修改 action",
            "取得檔案修改", "取得支援檔案修改", "無法取得修改", "no available tool",
            "tool is unavailable", "waiting for a tool", "waiting for edit",
        )
        if not any(marker.casefold() in haystack for marker in blocked_markers):
            return ""
        available = [
            name for name in (
                "write_file", "begin_file_write", "write_file_chunk", "commit_file_write",
                "web_edit_file", "run_command", "read_file", "query_project", "project_sync",
            )
            if name in SUPPORTED_ACTION_TOOLS
        ]
        return (
            "[SMARTAGENT_CAPABILITY_GUIDANCE] Runtime 已提供以下可執行 action："
            + ", ".join(available)
            + "。不得再以『缺少修改/命令能力』等待；請依檔案大小與任務需求選擇 action。"
        ) if available else ""

    @staticmethod
    def _declared_next_tools(next_action: object) -> list[str]:
        """Extract explicit canonical tool names from Progress.next_action.

        This is intentionally lexical rather than inferential.  Runtime may
        enforce an exact tool name the model wrote, but it must not translate
        arbitrary prose into new execution authority.
        """
        text = str(next_action or "")
        if not text:
            return []
        declared = []
        for tool in sorted(SUPPORTED_ACTION_TOOLS, key=len, reverse=True):
            if tool in {"report_progress", "final_response"}:
                continue
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(tool)}(?![A-Za-z0-9_])"
            if re.search(pattern, text, flags=re.IGNORECASE):
                declared.append(tool)
        return sorted(set(declared))

    @classmethod
    def _progress_action_consistency_diagnostic(
        cls, normalized_progress: Mapping[str, Any], operational: list[dict],
    ) -> dict | None:
        """Require explicitly declared next tools to be emitted in this turn.

        The gate checks action presence only.  Tool execution and result
        verification remain owned by the later Runtime lifecycle.
        """
        if str(normalized_progress.get("decision", "") or "").upper() != "CONTINUE":
            return None
        declared = cls._declared_next_tools(normalized_progress.get("next_action", ""))
        if not declared:
            return None
        actual = sorted({
            str(action.get("tool", "") or "")
            for action in operational
            if str(action.get("tool", "") or "") not in {"", "report_progress", "final_response"}
        })
        if any(tool in actual for tool in declared):
            return None
        reason = "declared_next_action_missing" if not actual else "declared_next_action_mismatch"
        return {
            "marker": "[WEBAGENT_PROGRESS_ACTION_REJECTED]",
            "reason": reason,
            "detail": (
                "decision=CONTINUE;declared_next_tools="
                + json.dumps(declared, ensure_ascii=False, separators=(",", ":"))
                + ";actual_operational_tools="
                + json.dumps(actual, ensure_ascii=False, separators=(",", ":"))
            ),
            "suggestion": (
                "Progress.next_action 已明確宣告工具；同一輪必須輸出其中一個完整 canonical action。"
                "若 prerequisite 尚不足，請先把 next_action 改成真正要執行的 prerequisite tool 並輸出該 action。"
                "這一層只驗證 action 是否送出，不代表 action 已成功，也不得提前宣告結果。"
            ),
            "declared_next_tools": declared,
            "actual_operational_tools": actual,
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
                nested = call.get("progress")
                progress = nested if isinstance(nested, dict) else call
                semantic_calls.append({
                    "tool": tool,
                    "progress_shape": "nested" if isinstance(nested, dict) else "flat",
                    "current_step": progress.get("current_step"),
                    "total_steps": progress.get("total_steps"),
                    "decision": str(progress.get("decision", "") or "").upper(),
                    "outcome": str(progress.get("outcome", "") or "").upper(),
                    "matched_condition": str(progress.get("matched_condition", "") or ""),
                    "matched_condition_id": str(
                        progress.get("matched_condition_id", "") or ""
                    ),
                    "completion_contract": progress.get("completion_contract", {}),
                    "evidence_refs": sorted(str(item) for item in list(progress.get("evidence_refs") or [])),
                    "steps": [
                        {
                            "step": item.get("step"),
                            "status": str(item.get("status", "") or "").upper(),
                        }
                        for item in list(progress.get("steps") or [])
                        if isinstance(item, dict)
                    ],
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
                    {
                        "reason": item.get("reason", ""),
                        "detail": item.get("detail", ""),
                        "condition_class": item.get("condition_class", ""),
                        "actual_condition": item.get("actual_condition", ""),
                        "allowed_conditions": list(item.get("allowed_conditions", []) or []),
                    }
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
        latest = result_refs[-1]
        verification = self._effective_verification_status(
            [latest],
            matched_condition=str(normalized_progress.get("matched_condition", "") or ""),
        )
        if (
            normalized_progress.get("decision") == "COMPLETE"
            and normalized_progress.get("outcome") == "SUCCESS"
            and verification in {"FAIL", "UNVERIFIED", "SPEC_INVALID"}
        ):
            return False
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
        terminal_verdict = self._terminal_evidence_verdict(
            result_refs, matched_condition=matched,
        )
        if outcome == "SUCCESS":
            evidence = [self.action_result_ledger[item] for item in result_refs]
            if not any(
                str(item.get("execution_status", "") or "").upper() == "SUCCEEDED"
                for item in evidence
            ):
                return False
            if terminal_verdict != "PASS":
                return False
        elif outcome == "FAILED" and terminal_verdict != "FAIL":
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

    def _preserve_progress_repair_actions(
        self, operational: list[dict], expected: dict,
    ) -> list[str]:
        """Hold schema-valid non-terminal actions while Progress is repaired.

        The actions are not admitted or executed here.  They are replayed into
        the same logical decision only after a repaired Progress payload passes
        every normal admission and state-machine gate.
        """
        candidates = [
            dict(action) for action in operational
            if str(action.get("tool", "") or "") != "final_response"
        ]
        if not candidates or len(candidates) != len(operational):
            self.pending_progress_repair_actions = []
            return []
        try:
            context = V8RequestContext(
                self.run_id,
                getattr(self, "task_id", "") or self.run_id,
                getattr(self, "task_epoch", "") or self.run_id,
                getattr(self, "intent_digest", ""),
            )
        except ProtocolV8Error:
            self.pending_progress_repair_actions = []
            return []
        seen: set[str] = set()
        for index, action in enumerate(candidates, 1):
            tool_name = str(action.get("tool", "") or "")
            if tool_name not in SUPPORTED_ACTION_TOOLS:
                self.pending_progress_repair_actions = []
                return []
            envelope_valid, _ = validate_tool_envelope(action, block_index=index)
            if not envelope_valid:
                self.pending_progress_repair_actions = []
                return []
            try:
                record = v8_admit_action(action, context)
            except ProtocolV8Error:
                self.pending_progress_repair_actions = []
                return []
            action_id = str(record.get("action_id", "") or "")
            if not action_id or action_id in seen:
                self.pending_progress_repair_actions = []
                return []
            seen.add(action_id)
        self.pending_progress_repair_actions = candidates
        action_ids = [str(action.get("action_id", "") or "") for action in candidates]
        self._emit(
            "progress_repair_actions_preserved",
            request_id=self.run_id,
            round=self.turn_id,
            action_ids=action_ids,
        )
        return action_ids

    def _restore_progress_repair_actions(self, calls: list[dict]) -> list[dict]:
        pending = [dict(action) for action in self.pending_progress_repair_actions]
        if not pending or not calls or calls[-1].get("tool") != "turn_commit":
            return calls
        submitted = [
            action for action in calls[:-1]
            if action.get("tool") != "report_progress"
        ]
        if submitted:
            return calls
        restored = [*calls[:-1], *pending]
        commit = dict(calls[-1])
        commit["action_count"] = len(restored)
        restored.append(commit)
        self._emit(
            "progress_repair_actions_restored",
            request_id=self.run_id,
            round=self.turn_id,
            action_ids=[str(action.get("action_id", "") or "") for action in pending],
        )
        return restored

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
        normalized_progress_action, wrapper_diagnostic = self._normalize_report_progress_envelope(
            progress_actions[0], self.progress_ledger
        )
        if wrapper_diagnostic:
            return [], [wrapper_diagnostic]
        progress_index = actions.index(progress_actions[0])
        actions[progress_index] = normalized_progress_action
        progress_actions = [normalized_progress_action]
        operational = [action for action in actions if action.get("tool") != "report_progress"]
        for action in operational:
            if str(action.get("tool", "") or "") != "query_project":
                continue
            queries = list(action.get("queries") or [])
            invalid_indexes = [
                index
                for index, query in enumerate(queries, 1)
                if not isinstance(query, Mapping)
                or not str(query.get("operation", "") or "").strip()
            ]
            if invalid_indexes:
                return [], [{
                    "marker": "[WEBAGENT_QUERY_PROJECT_REJECTED]",
                    "reason": "query_project_structured_queries_required",
                    "detail": (
                        "queries[] must contain explicit operation objects; invalid_indexes="
                        + json.dumps(invalid_indexes, separators=(",", ":"))
                    ),
                    "suggestion": PROJECT_EVIDENCE_ACTION_CONTRACT,
                }]
        pending_edit_recovery = dict(self.pending_result_recovery or {})
        if (
            pending_edit_recovery.get("active")
            and pending_edit_recovery.get("blocked_tool") == "apply_edit_plan"
            and not pending_edit_recovery.get("prerequisite_satisfied")
        ):
            if any(
                str(action.get("tool", "") or "") == "apply_edit_plan"
                for action in operational
            ):
                return [], [{
                    "marker": "[WEBAGENT_EDIT_PLAN_PREREQUISITE_REQUIRED]",
                    "reason": "edit_plan_prerequisite_unsatisfied",
                    "detail": (
                        "apply_edit_plan is blocked until structured read_range evidence is complete; "
                        "missing_paths="
                        + json.dumps(
                            pending_edit_recovery.get("missing_content_paths", []),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    ),
                    "suggestion": render_result_evidence_to_action_guidance(
                        pending_edit_recovery
                    ),
                }]
            submitted_queries = [
                action for action in operational
                if str(action.get("tool", "") or "") == "query_project"
            ]
            expected_queries = [
                action for action in list(pending_edit_recovery.get("next_actions") or [])
                if isinstance(action, dict)
                and str(action.get("tool", "") or "") == "query_project"
            ]
            if submitted_queries and expected_queries:
                def query_signature(action: dict) -> tuple:
                    return tuple(
                        (
                            str(query.get("operation", "") or "").lower(),
                            str(query.get("path", "") or "").replace("\\", "/").lstrip("/"),
                            int(query.get("start_line", 1) or 1),
                            int(query.get("end_line", 240) or 240),
                        )
                        for query in list(action.get("queries") or [])
                        if isinstance(query, dict)
                    )
                allowed = {query_signature(action) for action in expected_queries}
                if not any(query_signature(action) in allowed for action in submitted_queries):
                    return [], [{
                        "marker": "[WEBAGENT_EDIT_PLAN_PREREQUISITE_REQUIRED]",
                        "reason": "edit_plan_prerequisite_action_mismatch",
                        "detail": "query_project must use the exact Runtime-issued read_range path and range",
                        "suggestion": render_result_evidence_to_action_guidance(
                            pending_edit_recovery
                        ),
                    }]
        pending_plan_recovery = dict(self.pending_result_recovery or {})
        repair_actions = [
            action for action in operational
            if str(action.get("tool", "") or "") == "repair_task_plan"
        ]
        plan_repair_active = (
            pending_plan_recovery.get("active")
            and str(pending_plan_recovery.get("reason", "")).startswith("task_plan_")
            and str(pending_plan_recovery.get("required_action", "") or "")
            == "repair_task_plan"
            and not pending_plan_recovery.get("prerequisite_satisfied")
        )
        if repair_actions and not plan_repair_active:
            return [], [{
                "marker": "[WEBAGENT_TASK_PLAN_REPAIR_REJECTED]",
                "reason": "task_plan_repair_without_runtime_latch",
                "detail": "repair_task_plan is allowed only while Runtime PLAN_REPAIR_REQUIRED is active",
                "suggestion": "Use propose_task_plan for a new canonical TASK_PLAN_V1 plan.",
            }]
        if (
            plan_repair_active
        ):
            operational_tools = [str(action.get("tool", "") or "") for action in operational]
            forbidden = set(pending_plan_recovery.get("forbidden_actions") or [])
            attempted_forbidden = sorted(forbidden.intersection(operational_tools))
            if attempted_forbidden:
                return [], [{
                    "marker": "[WEBAGENT_TASK_PLAN_REPAIR_REQUIRED]",
                    "reason": "task_plan_repair_latch_active",
                    "detail": (
                        "task-plan repair prerequisite is active; forbidden_tools="
                        + json.dumps(attempted_forbidden, separators=(",", ":"))
                    ),
                    "suggestion": render_result_evidence_to_action_guidance(
                        pending_plan_recovery
                    ),
                }]
            expected_tool = str(pending_plan_recovery.get("required_action", "") or "")
            if expected_tool and operational_tools != [expected_tool]:
                return [], [{
                    "marker": "[WEBAGENT_TASK_PLAN_REPAIR_REQUIRED]",
                    "reason": "task_plan_repair_action_required",
                    "detail": (
                        f"expected exactly one {expected_tool}; actual="
                        + json.dumps(operational_tools, separators=(",", ":"))
                    ),
                    "suggestion": render_result_evidence_to_action_guidance(
                        pending_plan_recovery
                    ),
                }]
            expected_actions = [
                action for action in list(pending_plan_recovery.get("next_actions") or [])
                if isinstance(action, dict)
                and str(action.get("tool", "") or "") == expected_tool
            ]
            if expected_actions:
                expected_action = dict(expected_actions[0])
                submitted_action = dict(operational[0])
                expected_action.pop("action_id", None)
                submitted_action.pop("action_id", None)
                if submitted_action != expected_action:
                    return [], [{
                        "marker": "[WEBAGENT_TASK_PLAN_REPAIR_REQUIRED]",
                        "reason": "task_plan_repair_payload_mismatch",
                        "detail": "repair_task_plan must preserve the exact Runtime-issued canonical plan",
                        "suggestion": render_result_evidence_to_action_guidance(
                            pending_plan_recovery
                        ),
                    }]
        if getattr(self.tools, "_bootstrap_install_root", None) is None:
            evidence_route = build_evidence_to_action_route(
                operational, self.authorized_paths,
            )
            if evidence_route.get("active"):
                return [], [{
                    "marker": "[WEBAGENT_EVIDENCE_ROUTE_REQUIRED]",
                    "reason": "web_planner_project_read_requires_query_project",
                    "detail": (
                        "read_file would upload project source instead of returning bounded "
                        "Runtime evidence; routed_files="
                        + json.dumps(
                            evidence_route.get("routed_files", []),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    ),
                    "suggestion": render_evidence_to_action_guidance(evidence_route),
                }]
            for action in operational:
                if str(action.get("tool", "") or "") != "run_command":
                    continue
                verification_detail = self._run_command_verification_detail(action)
                if verification_detail:
                    return [], [{
                        "marker": "[WEBAGENT_VERIFICATION_REQUIRED]",
                        "reason": "run_command_verification_contract_required",
                        "detail": verification_detail,
                        "suggestion": (
                            "保留同一 operation、command 與決策。verify 必須是 non-empty object 或 object list，"
                            "不得使用自然語言字串。可直接使用："
                            '"verify":[{"action":"run_command","command":"驗證用 PowerShell 指令",'
                            '"expect_exit_code":0,"expect_contains":"可選關鍵字"}]。'
                            "Runtime 必須在執行前取得完成條件，避免工具成功後才追討證據。"
                        ),
                    }]
                shell_detail = self._run_command_shell_mismatch_detail(action)
                if shell_detail:
                    return [], [{
                        "marker": "[WEBAGENT_SHELL_MISMATCH]",
                        "reason": "run_command_shell_syntax_mismatch",
                        "detail": shell_detail,
                        "suggestion": (
                            "run_command 固定由 Windows PowerShell 5.1 執行。不得使用 CMD 的 cd /d、"
                            "裸露 && 或 ||。Git 請改用 git -C 'E:\\path\\to\\repo' <args>；"
                            "一般目錄切換請使用 Set-Location -LiteralPath 'E:\\path'，多指令以 ; 分隔。"
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
        state_diagnostic = self._normalize_action_loop_decision(
            progress_actions[0], operational,
        )
        if state_diagnostic:
            return [], [state_diagnostic]
        try:
            normalized_progress = validate_model_progress(progress_actions[0], self.progress_ledger)
        except TaskProgressError as exc:
            preserved_action_ids = self._preserve_progress_repair_actions(
                operational, expected,
            )
            diagnostic = {
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "invalid_progress_payload",
                "detail": str(exc),
                "suggestion": "保留決策，只修正 report_progress 的階段、Base、steps 或文字欄位。",
            }
            repair_context = dict(getattr(exc, "repair_context", {}) or {})
            if repair_context:
                diagnostic.update(repair_context)
            if preserved_action_ids:
                diagnostic["preserved_action_ids"] = preserved_action_ids
            return [], [diagnostic]
        decision = normalized_progress["decision"]
        outcome = normalized_progress["outcome"]
        success_conditions = list(
            dict(normalized_progress.get("completion_contract") or {}).get("success") or []
        )
        success_condition_ids = {
            item["condition_id"]
            for item in completion_condition_choices(
                normalized_progress.get("completion_contract") or {}, "success",
            )
        }
        allowed_expected_failure_conditions = {
            str(item).strip() for item in success_conditions if str(item).strip()
        } | success_condition_ids
        for action in operational:
            if action.get("expected_failure") is None:
                continue
            declared_condition = str(action.get("condition_id", "") or "").strip()
            success_criteria = str(action.get("success_criteria", "") or "").strip()
            if (
                declared_condition not in allowed_expected_failure_conditions
                and success_criteria not in allowed_expected_failure_conditions
            ):
                return [], [{
                    "marker": "[WEBAGENT_EXPECTED_FAILURE_REJECTED]",
                    "reason": "expected_failure_not_bound_to_success_contract",
                    "detail": json.dumps({
                        "action_id": str(action.get("action_id", "") or ""),
                        "condition_id": declared_condition,
                        "success_criteria": success_criteria,
                        "allowed_success_conditions": success_conditions,
                        "allowed_success_condition_ids": sorted(success_condition_ids),
                    }, ensure_ascii=False, separators=(",", ":")),
                    "suggestion": (
                        "expected_failure 必須在執行前綁定 completion_contract.success 中的精確條件文字，"
                        "或其 Runtime condition_id；不得用事後新增或不相干條件包裝一般失敗。"
                    ),
                }]
        action_consistency = self._progress_action_consistency_diagnostic(
            normalized_progress, operational,
        )
        if action_consistency:
            return [], [action_consistency]
        if decision == "CONTINUE" and float(normalized_progress["current_step"]) >= float(normalized_progress["total_steps"]):
            evidence_actions = [
                item for item in operational
                if item.get("tool") not in {"final_response", "report_progress"}
            ]
            if not evidence_actions:
                return [], [{
                    "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                    "reason": "final_step_continue_requires_action",
                    "detail": "current_step=total_steps may continue only while an evidence-producing final-step action is present",
                    "suggestion": "保留最後一步 IN_PROGRESS，並在同輪輸出補驗證或修正 action；否則改為 COMPLETE。",
                }]
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
        for action in operational:
            verifies_action_id = str(action.get("verifies_action_id", "") or "").strip()
            if verifies_action_id and verifies_action_id not in self.action_result_ledger:
                return [], [{
                    "marker": "[WEBAGENT_VERIFICATION_LINK_REJECTED]",
                    "reason": "verifies_action_id_unknown",
                    "detail": f"verifies_action_id={verifies_action_id}",
                    "suggestion": "verifies_action_id 必須引用 [RUNTIME_EVIDENCE] 中同一 request 已完成的 action_id。",
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
                "suggestion": (
                    "二選一：若仍需執行，移除 final_response 並輸出至少一個明確 action；"
                    "若任務已完成，將 current_step 設為 total_steps、所有 steps[].status 設為 "
                    "COMPLETED，並使用 decision=COMPLETE 與 SUCCESS/FAILED/PARTIAL outcome。"
                ),
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
        verification_status = self._effective_verification_status(
            evidence_refs,
            matched_condition=str(normalized_progress.get("matched_condition", "") or ""),
        )
        terminal_evidence_verdict = self._terminal_evidence_verdict(
            evidence_refs,
            matched_condition=str(normalized_progress.get("matched_condition", "") or ""),
        )
        if terminal_evidence_verdict in {"PASS", "FAIL"}:
            self.pending_verification_requirement = {}
        if (
            self.pending_verification_requirement
            and verification_status in {"UNVERIFIED", "SPEC_INVALID"}
            and decision == "CONTINUE"
            and not self._has_structured_verification_action(operational)
        ):
            return [], [self._verification_requirement_diagnostic(
                normalized_progress,
                reason="verification_evidence_action_required",
            )]
        if decision == "COMPLETE" and outcome == "SUCCESS" and terminal_evidence_verdict == "UNVERIFIED":
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
                "verification_status": verification_status,
            }
            self.pending_verification_requirement = {
                "missing_condition": (
                    str(normalized_progress.get("matched_condition", "") or "").strip()
                    or "取得支持 terminal SUCCESS 的 request-scoped PASS evidence"
                ),
                "condition_id": self._canonical_condition_id(
                    str(normalized_progress.get("matched_condition", "") or "").strip()
                    or "取得支持 terminal SUCCESS 的 request-scoped PASS evidence"
                ),
                "current_verification_status": terminal_evidence_verdict,
                "required_action": "run_command",
                "required_fields": ["operation", "command", "success_criteria", "verify"],
                "supported_verify_actions": ["run_command", "aggregate_verification"],
                "supported_verify_checks": ["file_exists", "file_contains", "expect_regex"],
                "evidence_refs": list(normalized_progress.get("evidence_refs") or []),
            }
            return [], [self._verification_requirement_diagnostic(
                normalized_progress,
                reason="terminal_success_verification_missing",
            )]
        if decision == "COMPLETE" and outcome == "SUCCESS" and terminal_evidence_verdict == "FAIL":
            return [], [{
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "success_contradicts_runtime_verification",
                "detail": f"model_outcome=SUCCESS;verification={terminal_evidence_verdict}",
                "suggestion": "依目前 FAIL evidence 回覆 FAILED/PARTIAL，或以 verifies_action_id 綁定原 action，執行修正驗證後再判斷。",
            }]
        if decision == "COMPLETE" and outcome == "FAILED" and terminal_evidence_verdict != "FAIL":
            return [], [{
                "marker": "[WEBAGENT_PROGRESS_REJECTED]",
                "reason": "failure_not_supported_by_runtime_evidence",
                "detail": f"model_outcome=FAILED;runtime_evidence={terminal_evidence_verdict}",
                "suggestion": "FAILED 必須引用 execution_status=FAILED 或 verification_status=FAIL 的 action evidence；否則繼續取得客觀結果。",
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
            envelope_valid, envelope_diagnostic = validate_tool_envelope(
                action,
                block_index=index,
            )
            if not envelope_valid:
                diagnostic = dict(envelope_diagnostic or {})
                diagnostics.append({
                    "marker": diagnostic.get("marker", "[WEBAGENT_V8_REJECTED]"),
                    "reason": diagnostic.get("reason", "invalid_tool_envelope"),
                    "detail": (
                        f"action_index={index};action_id={action.get('action_id', '')};"
                        f"{diagnostic.get('detail', '')}"
                    ),
                    "suggestion": diagnostic.get(
                        "suggestion",
                        "依 canonical tool schema 重建 action。",
                    ),
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
        self.pending_progress_repair_actions = []
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
        project_root_candidates = [
            str(item).strip() for item in self.authorized_paths if str(item).strip()
        ]
        for entry in self.action_ledger.values():
            previous_action = entry.get("action")
            if not isinstance(previous_action, dict):
                continue
            for field in ("project_root", "workspace"):
                candidate = str(previous_action.get(field, "") or "").strip()
                if candidate:
                    project_root_candidates.append(candidate)
        project_root_hint = max(
            project_root_candidates,
            key=lambda item: len(Path(item).parts),
            default="",
        )
        recovery_context = build_result_evidence_to_action_route(
            action,
            raw_result,
            project_root_hint=project_root_hint,
            prior_context=self.pending_result_recovery,
        )
        if recovery_context.get("active"):
            self.pending_result_recovery = dict(recovery_context)
            if str(recovery_context.get("reason", "")).startswith("task_plan_"):
                self._emit(
                    "task_plan_repair_latch_updated",
                    request_id=self.run_id,
                    round=self.turn_id,
                    state=recovery_context.get("runtime_state", "BLOCKED_ON_PREREQUISITE"),
                    reason=recovery_context.get("reason", ""),
                    blocking_reason=recovery_context.get("blocking_reason", ""),
                    required_action=recovery_context.get("required_action", ""),
                    forbidden_actions=recovery_context.get("forbidden_actions", []),
                    diagnostic_signature=recovery_context.get("diagnostic_signature", ""),
                    same_diagnostic_count=recovery_context.get("same_diagnostic_count", 0),
                )
        elif str(action.get("tool", "") or "") in {
            "validate_edit_plan", "apply_edit_plan",
            "propose_task_plan", "repair_task_plan", "propose_task_plan_file",
        }:
            had_task_plan_latch = str(
                self.pending_result_recovery.get("reason", "")
            ).startswith("task_plan_")
            self.pending_result_recovery = {}
            if had_task_plan_latch:
                self._emit(
                    "task_plan_repair_latch_released",
                    request_id=self.run_id,
                    round=self.turn_id,
                    state="PLAN_FROZEN",
                )
        result = prepare_tool_result(action, raw_result, self.tools)
        evidence = self._classify_action_evidence(action, result)
        self.action_ledger[action_id] = {
            "signature": signature,
            "result": result,
            "action": dict(action),
            "recovery_context": recovery_context,
            **evidence,
        }
        self.execution_state = evidence["execution_status"]
        return str(result)

    def _result_recovery_guidance(self, actions: list[dict]) -> str:
        guidance: list[str] = []
        rendered_contexts: set[str] = set()
        for action in actions:
            entry = self.action_ledger.get(
                str(action.get("action_id", "") or ""), {}
            )
            rendered = render_result_evidence_to_action_guidance(
                entry.get("recovery_context") if isinstance(entry, dict) else None
            )
            if rendered:
                guidance.append(rendered)
                rendered_contexts.add(rendered)
        pending = render_result_evidence_to_action_guidance(
            self.pending_result_recovery
        )
        if pending and pending not in rendered_contexts:
            guidance.append(pending)
        return "\n".join(guidance)

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
        """Run one request and always release its browser ownership on failure."""
        try:
            return self._run_impl(
                request,
                request_id=request_id,
                initial_attachments=initial_attachments,
                source_tag=source_tag,
                task_id=task_id,
                task_epoch=task_epoch,
                skill_context=skill_context,
            )
        except BaseException:
            from agent_core.request_ownership import release_active_request

            try:
                release_active_request(str(request_id or self.run_id or ""))
            except Exception as release_error:
                # Cleanup must never replace the original protocol failure.
                self._emit(
                    "request_ownership_release_failed",
                    request_id=str(request_id or self.run_id or ""),
                    error=f"{type(release_error).__name__}: {release_error}",
                )
            raise

    def _run_impl(
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
        self.protocol_state = "ACTIVE"
        self.action_loop_state = build_action_loop_state(
            transport_state=self.protocol_state,
        )
        self.progress_ledger = initialize_progress(
            self.task_id,
            request_id=self.run_id,
            goal=request,
            root=self.progress_root,
        )
        self.turn_id = 0
        self.terminal_outcome = "UNKNOWN"
        self.execution_state = "NOT_STARTED"
        self.terminal_candidate = {}
        last_no_action_decision_signature: str | None = None
        last_dead_end_recovery_signature: str | None = None
        last_rejected_exchange_signature: str | None = None
        self.pending_result_ack_id = ""
        self.pending_web_ack_id = ""
        self.seen_web_ack_ids.clear()
        self.accepted_ack_ids.clear()
        self.sent_attachment_paths.clear()
        self.action_ledger.clear()
        self.action_result_ledger.clear()
        self.condition_result_ledger.clear()
        self.v8_admitted_actions.clear()
        self.pending_progress_repair_actions.clear()
        self.active_stage_manifest = None
        self.pending_result_recovery = {}
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
        prompt += "\n" + render_initial_planner_toolkit()
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
            action_id_seed = f"{expected.get('run_id', '')}:{expected.get('turn_id', '')}"
            calls, v8_parse_diagnostics = parse_v9_tool_transport(
                response,
                action_id_seed=action_id_seed,
            )
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
                pending_recovery = self._result_recovery_guidance([])
                if pending_recovery:
                    prompt += "\n" + pending_recovery
                continue

            calls = self._restore_progress_repair_actions(calls)
            actions, ack_diagnostics = self._accept_ack(calls, expected)
            if ack_diagnostics:
                self.protocol_state = "REJECTED"
                if self.pending_progress_repair_actions and not any(
                    item.get("reason") == "invalid_progress_payload"
                    for item in ack_diagnostics
                ):
                    invalidated_ids = [
                        str(action.get("action_id", "") or "")
                        for action in self.pending_progress_repair_actions
                    ]
                    self.pending_progress_repair_actions = []
                    self._emit(
                        "progress_repair_actions_invalidated",
                        request_id=self.run_id,
                        round=self.turn_id,
                        action_ids=invalidated_ids,
                        reason="non_progress_admission_rejection",
                    )
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
                    diagnostic = dict(ack_diagnostics[0] if ack_diagnostics else {})
                    diagnostic_reason = str(diagnostic.get("reason", "unknown") or "unknown")
                    diagnostic_detail = str(diagnostic.get("detail", "") or "")
                    reason = (
                        "semantic_stagnation:repeated_progress_contract_rejection:"
                        + diagnostic_reason
                    )
                    set_runtime_state(self.task_id, "PAUSED", reason=reason, root=self.progress_root)
                    self._emit(
                        "protocol_loop_paused",
                        request_id=self.run_id,
                        round=self.turn_id,
                        reason=reason,
                        diagnostic_reason=diagnostic_reason,
                        diagnostic_detail=diagnostic_detail,
                    )
                    raise RuntimeError(
                        "WebAgent 連續回傳相同且不符合完成契約（Action Loop）的決策，Runtime 已暫停重複問答；"
                        f"reason={diagnostic_reason}; detail={diagnostic_detail}"
                    )
                last_rejected_exchange_signature = rejection_signature
                repair_note = ""
                if any(item.get("reason") == "unexpected_field" for item in ack_diagnostics):
                    repair_note = (
                        "\n這不是 FIELD_REPAIR，而是 ACTION_REPLAN：原 action 含有未定義或放錯層級的欄位。"
                        "請使用新的 action_id，依 tool 的 canonical schema 重建完整 action；"
                        "不得保留 diagnostic 指出的欄位。query_project 的 operation/path/symbol "
                        "只能放在 queries[] 物件內。"
                    )
                if any(item.get("reason") == "MISSING_OR_INVALID_FIELD" for item in ack_diagnostics):
                    repair_note = (
                        "\n這是 FIELD_REPAIR：保留原 tool、action_id 與所有已提供欄位，只補 diagnostic 指定欄位。"
                    )
                progress_details = "\n".join(
                    str(item.get("detail", "") or "")
                    for item in ack_diagnostics
                    if item.get("reason") == "invalid_progress_payload"
                )
                if progress_details:
                    repair_note += (
                        "\n這是 PROGRESS_REPAIR：steps[].status 合法值只有 PENDING、IN_PROGRESS、COMPLETED。"
                        "若 decision=COMPLETE，current_step 必須等於 total_steps，而且所有 steps[].status "
                        "都必須是 COMPLETED。不得使用 COMPLETE 作為 step status。"
                        "Progress 欄位的 canonical 位置是 report_progress 最外層；不要再包一層 progress。"
                    )
                condition_repairs = [
                    item for item in ack_diagnostics
                    if item.get("reason") == "invalid_progress_payload"
                    and item.get("condition_class")
                ]
                for item in condition_repairs:
                    condition_choices = list(item.get("condition_choices", []) or [])
                    repair_note += (
                        "\n這是 MATCHED_CONDITION_REPAIR：目前值="
                        + json.dumps(str(item.get("actual_condition", "") or ""), ensure_ascii=False)
                        + "；condition_class="
                        + json.dumps(str(item.get("condition_class", "") or ""), ensure_ascii=False)
                        + "；請從下列候選選擇唯一的 condition_id，並回填 matched_condition_id："
                        + json.dumps(condition_choices, ensure_ascii=False)
                        + "。condition_id 是契約身分；matched_condition 文字可保留原說法。"
                    )
                preserved_action_ids = sorted({
                    str(action_id)
                    for item in ack_diagnostics
                    for action_id in list(item.get("preserved_action_ids", []) or [])
                    if str(action_id)
                })
                if preserved_action_ids:
                    repair_note += (
                        "\nRuntime 已暫存同輪合法 action："
                        + json.dumps(preserved_action_ids, ensure_ascii=False)
                        + "。本次只修正 report_progress，不要重送或改寫 action；"
                        "Progress 通過後 Runtime 會把原 action 接回同一邏輯回合。"
                    )
                if any(
                    item.get("reason") == "continue_decision_forbids_final_response"
                    for item in ack_diagnostics
                ):
                    repair_note += (
                        "\n這是 TERMINAL_DECISION_REPAIR，必須二選一："
                        "(A) 尚未完成：保留 CONTINUE/PENDING、移除 final_response，並輸出至少一個明確 action；"
                        "(B) 已完成：current_step=total_steps、所有 steps[].status=COMPLETED，"
                        "decision=COMPLETE、outcome=SUCCESS/FAILED/PARTIAL，並保留 final_response。"
                    )
                if any(
                    item.get("reason") in {
                        "declared_next_action_missing", "declared_next_action_mismatch",
                    }
                    for item in ack_diagnostics
                ):
                    repair_note += (
                        "\n這是 PROGRESS_TO_ACTION_REPAIR：只修正 action emission。"
                        "next_action 宣告的 canonical tool 必須在同一輪以完整 action 出現；"
                        "不得聲稱它已執行或預測其結果。"
                    )
                prompt = (
                    "[WEBAGENT_ACK_REJECTED]\n"
                    + format_tool_parse_diagnostics(ack_diagnostics)
                    + "\n修正 compact v9 response 後重送同一決策；不得重做尚未執行的 action。"
                    + repair_note
                )
                pending_recovery = self._result_recovery_guidance([])
                if pending_recovery:
                    prompt += "\n" + pending_recovery
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
                verification_status = self._effective_verification_status(
                    getattr(self.progress_ledger, "evidence_refs", []) or [],
                    matched_condition=str(getattr(self.progress_ledger, "matched_condition", "") or ""),
                )
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
                pending_recovery = dict(self.pending_result_recovery or {})
                recovery_actions = [
                    item for item in list(pending_recovery.get("next_actions") or [])
                    if isinstance(item, dict) and str(item.get("tool", "") or "")
                ]
                if pending_recovery.get("active") and not pending_recovery.get(
                    "prerequisite_satisfied"
                ):
                    recovery_signature = json.dumps(
                        {
                            "reason": pending_recovery.get("reason", ""),
                            "next_actions": recovery_actions,
                            "terminal_if_no_action": pending_recovery.get("terminal_if_no_action", ""),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if recovery_actions and recovery_signature != last_dead_end_recovery_signature:
                        last_dead_end_recovery_signature = recovery_signature
                        last_no_action_decision_signature = None
                        prompt = (
                            "[WEBAGENT_DEAD_END_RECOVERY_REQUIRED]\n"
                            "目前 Progress 宣告 CONTINUE，但沒有 operational action。Runtime 已有可執行的"
                            " prerequisite route；下一輪必須輸出 route.next_actions 中的一個完整 canonical "
                            "action，不得只回 Progress 或『等待』。\n"
                            + self._result_recovery_guidance([])
                            + "\n"
                            + format_prompt_context(self.progress_ledger)
                            + "\n[/WEBAGENT_DEAD_END_RECOVERY_REQUIRED]"
                        )
                        self._emit(
                            "dead_end_recovery_requested",
                            request_id=self.run_id,
                            round=self.turn_id,
                            recovery_reason=pending_recovery.get("reason", ""),
                            available_actions=[item.get("tool", "") for item in recovery_actions],
                        )
                        continue
                    reason = (
                        "semantic_stagnation:recovery_action_not_emitted"
                        if recovery_actions
                        else "semantic_stagnation:recovery_route_has_no_action"
                    )
                    detail = str(
                        pending_recovery.get("terminal_if_no_action", "")
                        or pending_recovery.get("reason", "")
                        or "pending prerequisite has no executable route"
                    )
                    set_runtime_state(
                        self.task_id, "PAUSED", reason=f"{reason}:{detail}", root=self.progress_root
                    )
                    self._emit(
                        "protocol_loop_paused",
                        request_id=self.run_id,
                        round=self.turn_id,
                        reason=reason,
                        detail=detail,
                        recovery_context=pending_recovery,
                    )
                    raise RuntimeError(
                        "WebAgent 未執行 Runtime 指定的 prerequisite action；Runtime 已暫停。detail="
                        + detail
                    )
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
                        "declared_next_tools": self._declared_next_tools(
                            self.progress_ledger.next_action
                        ),
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
                last_dead_end_recovery_signature = None

            capability_guidance = self._progress_capability_guidance(progress_action)
            progress_result = (
                "[TASK_PROGRESS_RECORDED] "
                f"step={self.progress_ledger.current_step}/{self.progress_ledger.total_steps}"
            )
            if capability_guidance:
                progress_result += "\n" + capability_guidance
            results = [{
                "action_id": progress_action.get("action_id", ""),
                "tool": "report_progress",
                "result": progress_result,
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
                    ledger_entry = self._record_action_result_evidence(action, ledger_entry)
                    if str(
                        ledger_entry.get("effective_verification_status", "UNKNOWN") or "UNKNOWN"
                    ).upper() in {"PASS", "FAIL"}:
                        self.pending_verification_requirement = {}
                    self._emit(
                        "action_evidence_recorded",
                        request_id=self.run_id,
                        round=self.turn_id,
                        action_id=action.get("action_id", ""),
                        **action_evidence,
                        effective_verification_status=ledger_entry.get("effective_verification_status", "UNKNOWN"),
                        effective_verification_id=ledger_entry.get("effective_verification_id", ""),
                        verifies_action_id=ledger_entry.get("verifies_action_id", ""),
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
            state = self._refresh_action_loop_state()
            self._emit(
                "action_loop_state_updated",
                request_id=self.run_id,
                round=self.turn_id,
                state_id=state.get("state_id", ""),
                phase=state.get("phase", ""),
                execution_status=state.get("execution_status", ""),
                verification_status=state.get("verification_status", ""),
                evidence_state=state.get("evidence_state", ""),
                required_transition=state.get("required_transition", ""),
                allowed_next_actions=state.get("allowed_next_actions", []),
                blocked_actions=state.get("blocked_actions", []),
                missing_evidence=state.get("missing_evidence", []),
            )
            terminal_recovery = dict(self.pending_result_recovery or {})
            if terminal_recovery.get("pause_immediately"):
                detail = str(
                    terminal_recovery.get("terminal_if_no_action", "")
                    or terminal_recovery.get("reason", "")
                )
                reason = "semantic_stagnation:task_plan_repair_stalled"
                set_runtime_state(
                    self.task_id, "PAUSED", reason=f"{reason}:{detail}",
                    root=self.progress_root,
                )
                self._emit(
                    "protocol_loop_paused",
                    request_id=self.run_id,
                    round=self.turn_id,
                    reason=reason,
                    detail=detail,
                    recovery_context=terminal_recovery,
                )
                raise RuntimeError(
                    "WebAgent task plan 修復仍回傳相同 validator 診斷；Runtime 已暫停。detail="
                    + detail
                )
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
            recovery_guidance = self._result_recovery_guidance(actions)
            if recovery_guidance:
                prompt += "\n" + recovery_guidance
        raise RuntimeError(f"WebAgent 超過最大 protocol turns: {self.max_turns}")
