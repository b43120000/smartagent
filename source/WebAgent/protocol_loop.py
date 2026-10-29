#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Independent WebAgent protocol/result/ACK loop."""
from __future__ import annotations
from agent_core import path_integrity_trace as PIT

import hashlib
import json
import os
import re
import time
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
from agent_core.write_budget import validate_round_write_budget, write_budget_contract
from agent_core.runtime_environment import (
    ENVIRONMENT_LEDGER_SCHEMA,
    planner_environment_snapshot,
)

from .protocol import (
    SUPPORTED_ACTION_TOOLS,
    render_action_response_constraints,
    render_initial_planner_toolkit,
)
from .tool_context import WebAgentToolContext


PlannerCall = Callable[[str, dict, list[str]], str]
EventSink = Callable[[str], None]
WEBAGENT_STAGED_PROTOCOL = str(os.environ.get("SMARTAGENT_STAGED_PROTOCOL", "on")).strip().lower()

_WINDOWS_PATH_START_RE = re.compile(r"(?<![A-Za-z0-9])(?P<drive>[A-Za-z]:[\\/])")
_PATH_SENTENCE_BOUNDARY_RE = re.compile(r"[，；。]|[,;](?=\s|$)|[\r\n]")
_PATH_PROSE_BOUNDARY_RE = re.compile(
    # An unquoted path cannot consume a following natural-language predicate
    # or task instruction. Keep explicit quotes intact in _authorized_path_end;
    # never guess a shortened path from a genuine path containing spaces.
    r"\s+(?=(?:然後|接著|接下來|之後|完成後|接續|繼續|你先|請|並且|同時|最後"
    r"|再(?:去|執行|看|讀|修改|處理)|是否(?:不存在|存在|為)"
    r"|\s*的\s*(?=(?:(?:事前\s+)?file\.exists|結構化(?:狀態|查詢)|查詢結果|狀態|驗證結果)(?=\s|$|[,，；。;]))"
    r"|內(?=\s*[,，；。;]|\s+(?:使用|透過|執行|查詢|讀取|建立|檢查|確認|請))"
    r"|驗證以下(?:我指定的)?(?:原始)?\s*Predicate(?=\s*[：:])"
    r"|(?:測試)?範圍內(?:透過|使用|執行)|(?:此|該)?(?:測試)?範圍內透過"
    r"|進行\s+staging(?=\s|$|[,，；。;])))"
)
_PATH_TASK_SUFFIX_RE = re.compile(r"\s+(?:第一層內容|建立\s+[^\\/\s]+\.[A-Za-z0-9]{1,10}|建立\s+[^\\/\s]+\s+(?:測試)?資料夾)$")
_PATH_ATTACHED_PROSE_BOUNDARY_RE = re.compile(
    r"(?=(?:下(?:的|面)|裡面|里面|內(?:有|的)|有多少|有幾個|共有|的檔案))"
)
_PATH_QUOTE_PAIRS = {
    '"': '"', "'": "'", "`": "`",
    "「": "」", "『": "』",
}
_SAVED_PLAN_OPEN = "[SMARTAGENT_EXECUTE_SAVED_PLAN]"
_SAVED_PLAN_CLOSE = "[/SMARTAGENT_EXECUTE_SAVED_PLAN]"
_ORIGINAL_REQUEST_OPEN = "[ORIGINAL_PLANNING_REQUEST]"
_ORIGINAL_REQUEST_CLOSE = "[/ORIGINAL_PLANNING_REQUEST]"


def _hard_authorized_path_end(text: str, content_start: int) -> int:
    """Return the next boundary that cannot be part of the current path span."""
    end = len(text)
    newline = re.search(r"[\r\n]", text[content_start:])
    if newline:
        end = content_start + newline.start()
    next_path = _WINDOWS_PATH_START_RE.search(text, content_start)
    if next_path and next_path.start() < end:
        end = next_path.start()
    # Explicit neighbouring protocol fields are not part of an unquoted path.
    # Arbitrary prose/colon suffixes remain fail-closed (including disguised ADS).
    field_start = content_start
    opener = text[content_start - 4] if content_start >= 4 else ''
    if opener in _PATH_QUOTE_PAIRS:
        close = text.find(_PATH_QUOTE_PAIRS[opener], content_start, end)
        field_start = close + 1 if close >= 0 else end
    fields = re.search(r'[，,；;]\s*(?:snapshot_id|project_handle|queries|max_bytes|max_files|strategy)\s*=', text[field_start:end])
    if fields:
        end = field_start + fields.start()
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
    # Only recognize terminal task phrases, not words inside path components.
    # Preserve an actual full path with spaces; quoted paths returned above.
    span = text[start:end].rstrip()
    task_suffix = _PATH_TASK_SUFFIX_RE.search(span)
    # "在 <directory> 建立 <basename>" is an instruction. In "建立
    # <new filename>", the same words can be part of a not-yet-existing name.
    if task_suffix and task_suffix.group().lstrip().startswith("建立"):
        if not re.search(r"在\s*$", text[:start]):
            task_suffix = None
    if task_suffix:
        try:
            full_path_exists = Path(span).exists()
        except (OSError, ValueError):
            full_path_exists = False
        if not full_path_exists:
            end = start + task_suffix.start()
    return end


def extract_authorized_paths(request: str) -> list[str]:
    text = str(request or "")
    paths: list[str] = []
    consumed_until = -1
    for match in _WINDOWS_PATH_START_RE.finditer(text):
        if match.start() < consumed_until:
            continue
        # A complete JSON string value has its own lexical boundary. Do not
        # mistake a neighbouring field's colon for an ADS suffix. Decode only
        # the explicit string (never plan prose); real/escaped ADS remains in
        # the candidate for canonical path security to reject.
        quote = match.start() - 1
        if quote >= 0 and text[quote] == '"' and re.search(r':\s*$', text[:quote]):
            try:
                value, length = json.JSONDecoder().raw_decode(text[quote:])
            except (ValueError, json.JSONDecodeError):
                pass
            else:
                tail = text[quote + length:]
                if isinstance(value, str) and re.match(r'\s*(?:,\s*"[^"\r\n]+"\s*:|[}\]])', tail):
                    consumed_until = quote + length
                    if value and value not in paths:
                        paths.append(value)
                    continue
        hard_end = _hard_authorized_path_end(text, match.end())
        hidden_colon = text.find(":", match.end(), hard_end)
        # Do not let prose/quote parsing erase a possible ADS suffix.  Preserve
        # the colon so canonical path security rejects the candidate.
        parsed_end = _authorized_path_end(text, match.start(), match.end())
        # Keep the full ADS-looking candidate when it falls inside the normal
        # lexical span (e.g. file.txt:secret), so downstream authorization can
        # reject the original evidence without truncating the stream name.
        # If prose parsing ends before a hidden colon, retain at least that
        # colon: descriptions must never erase an ADS warning and grant scope.
        end = max(parsed_end, hidden_colon + 1) if hidden_colon >= 0 else parsed_end
        candidate = text[match.start():end].strip().rstrip(" \t.,;，；。")
        consumed_until = max(end, match.end())
        if candidate and candidate not in paths:
            paths.append(candidate)
    return paths


def resolve_authorized_paths(
    request: str,
    *,
    trusted_authorized_paths: list[str] | None = None,
) -> list[str]:
    """Resolve request scope without treating saved-plan prose as authority.

    New saved plans carry a ledger-backed path list. The tagged source fallback
    exists for older plan records only; accepted-plan and progress text must
    never expand filesystem authority.
    """
    if trusted_authorized_paths is not None:
        return list(
            dict.fromkeys(
                str(item).strip()
                for item in trusted_authorized_paths
                if str(item).strip()
            )
        )

    text = str(request or "")
    if _SAVED_PLAN_OPEN not in text and _SAVED_PLAN_CLOSE not in text:
        return extract_authorized_paths(text)
    if text.count(_SAVED_PLAN_OPEN) != 1 or text.count(_SAVED_PLAN_CLOSE) != 1:
        raise ValueError("saved_plan_authorization_source_invalid")
    if text.count(_ORIGINAL_REQUEST_OPEN) != 1 or text.count(_ORIGINAL_REQUEST_CLOSE) != 1:
        raise ValueError("saved_plan_authorization_source_invalid")
    start = text.find(_ORIGINAL_REQUEST_OPEN) + len(_ORIGINAL_REQUEST_OPEN)
    end = text.find(_ORIGINAL_REQUEST_CLOSE, start)
    if end < start:
        raise ValueError("saved_plan_authorization_source_invalid")
    return extract_authorized_paths(text[start:end])


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
        continuation_store=None,
        continuation_owner: str = "",
        fresh_context_adapter=None,
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
        self.continuation_store = continuation_store
        self.continuation_owner = continuation_owner
        self.continuation_source = None
        self.continuation_mode = ""
        self.terminal_outcome = "UNKNOWN"
        self.terminal_acceptance: dict = {}
        self._answer_only_admitted = False
        self._tri_one_origin = "WEB_DIRECT"
        self._tri_one_has_attached_context = False
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
        # A pre-execution envelope rejection is protocol evidence, never a Tool result.
        self._protocol_spec_rejection: dict[str, object] = {}
        # Native pre-execution bound-cwd rejection: not a ToolResult or terminal fact.
        self._pending_bound_cwd_repair: dict[str, object] = {}
        self.condition_result_ledger: dict[str, dict] = {}
        self.v8_admitted_actions: dict[str, dict] = {}
        self.pending_progress_repair_actions: list[dict] = []
        self.pending_action_repair: dict = {}
        self.active_stage_manifest: dict | None = None
        self.image_delivery_plan: dict = {}
        self.authorized_paths: list[str] = []
        self.pending_verification_requirement: dict = {}
        self.pending_result_recovery: dict = {}
        self.resolved_delete_target = ''
        self.deferred_progress_repair = {}
        self.read_only_progress_deferred = False
        self.last_deferred_read_signature = None
        self.execution_state = "NOT_STARTED"
        self.protocol_state = "IDLE"
        self.terminal_candidate: dict = {}
        self.request_context = None
        self.action_loop_state = build_action_loop_state()
        from agent_core.bounded_repair import BoundedRepairBudget
        self._bounded_repair = BoundedRepairBudget()
        from agent_core.bounded_repair import SemanticStagnationGuard
        self._semantic_stagnation = SemanticStagnationGuard()
        self._stagnation_pending = None
        self._gar = None
        self._gar_fresh_adapter = fresh_context_adapter
        self._gar_fresh_binding = None

    def _gar_l4_activate(self, snapshot):
        """Switch only after Runtime and browser both verify a distinct context.

        No new browser session is created by the Protocol Loop. The original
        request, Task/epoch, Action Ledger, Progress and Outbox remain intact.
        """
        from agent_core.task_progress import set_runtime_state
        adapter = self._gar_fresh_adapter
        if adapter is None:
            reason = self._gar.refuse_fresh_context(
                "GAR_FRESH_CONTEXT_UNAVAILABLE"
            ).reason
            set_runtime_state(self.task_id, "PAUSED", reason=reason,
                              root=self.progress_root)
            self._gar_persist()
            raise RuntimeError("GAR safely paused: " + reason)
        try:
            binding = adapter.prepare(
                request_id=self.run_id, task_id=self.task_id,
                task_epoch=self.task_epoch, owner=self._tri_one_origin,
            )
        except Exception as exc:
            # A failed/ambiguous bootstrap may have submitted a prompt: never
            # retry in-place or treat that submit as a completed task action.
            reason = self._gar.refuse_fresh_context(
                "GAR_L4_BOOTSTRAP_UNVERIFIED"
            ).reason
            self._gar_persist()
            set_runtime_state(self.task_id, "PAUSED", reason=reason,
                              root=self.progress_root)
            self._emit("gar_fresh_context_failed", request_id=self.run_id,
                       round=self.turn_id, reason=reason,
                       exception_type=type(exc).__name__)
            raise RuntimeError("GAR safely paused: " + reason) from exc
        verdict = self._gar.activate_fresh_context(
            proof=binding.proof, snapshot=snapshot,
        )
        if verdict.status != "ACTIVATED":
            binding.close()
            set_runtime_state(self.task_id, "PAUSED", reason=verdict.reason,
                              root=self.progress_root)
            self._gar_persist()
            raise RuntimeError("GAR safely paused: " + verdict.reason)
        # The new Planner has its own verified bootstrap/session. All old ACKs
        # remain in seen_web_ack_ids and in events; clear ONLY the previous-ACK
        # transport pointer after recording the explicit fence transition.
        previous_ack = self.pending_web_ack_id
        self._gar_fresh_binding = binding
        def fenced_fresh_planner(prompt, expected, attachments):
            if not self._gar.fence_old_planner(
                context_id=binding.proof.fresh_conversation_id
            ):
                raise RuntimeError("GAR_L4_OLD_PLANNER_FENCED")
            return binding.planner(prompt, expected, attachments)
        self.planner = fenced_fresh_planner
        self.pending_web_ack_id = ""
        self.pending_result_ack_id = ""
        self._gar_persist()
        self._emit(
            "gar_fresh_planner_activated",
            request_id=self.run_id, round=self.turn_id,
            previous_ack_digest=hashlib.sha256(
                str(previous_ack).encode("utf-8")).hexdigest(),
            **self._gar.diagnostic(),
        )
        from agent_core.task_progress import format_prompt_context
        return (
            verdict.prompt + "\n[LAST_ACCEPTED_RUNTIME_PROGRESS]\n"
            + format_prompt_context(self.progress_ledger)
            + "\n[/LAST_ACCEPTED_RUNTIME_PROGRESS]"
        )

    def _gar_persist(self):
        if self._gar is None:
            return
        try:
            base = self.progress_root if self.progress_root is not None else self.workspace
            checkpoint = self._gar.persist_snapshot(self._gar_snapshot(), root=base)
            self._emit("gar_checkpoint_persisted", request_id=self.run_id,
                       round=self.turn_id,
                       checkpoint_name=checkpoint.name,
                       **self._gar.diagnostic())
        except (OSError, ValueError, TypeError) as exc:
            # Reporting never rewrites a prior task result or authorizes retry.
            self._emit("gar_checkpoint_persist_failed", request_id=self.run_id,
                       round=self.turn_id, error_type=type(exc).__name__)

    def _gar_snapshot(self):
        if self._gar is None:
            return None
        ledger = self.progress_ledger
        progress = dict(getattr(ledger, "__dict__", {}) or {})
        pending_names = (
            "pending_result_recovery", "pending_verification_requirement",
            "pending_action_repair", "pending_progress_repair_actions",
            "deferred_action_repairs", "pending_command_field_repairs",
            "active_stage_manifest",
        )
        pending = {name: bool(getattr(self, name, None)) for name in pending_names}
        # An attempted tool in the Action Ledger without a corresponding
        # Runtime result is NOT a safely known outcome. A fresh Planner may
        # not bridge that gap by saying it completed.
        pending["unscoped_action_outcome"] = any(
            action_id not in self.action_result_ledger
            for action_id in self.action_ledger
        )
        pending["unknown_action_outcome"] = any(
            str(row.get("execution_status", "") or "").upper() in {
                "UNKNOWN", "OPEN", "PENDING", "RUNNING"
            } for row in self.action_ledger.values() if isinstance(row, dict)
        )
        return self._gar.snapshot(
            identity={"request_id": self.run_id, "task_id": self.task_id,
                      "task_epoch": self.task_epoch},
            verified_evidence=dict(self.action_result_ledger),
            blockers=list((self.action_loop_state or {}).get("blockers") or []),
            pending_operations=pending,
            progress=progress,
        )

    def _gar_rejection(self, diagnostics, *, fresh=True, repair_budget=None):
        if self._gar is None:
            return None
        snapshot = self._gar_snapshot()
        reason = str(next((item.get("reason") for item in diagnostics
                           if isinstance(item, dict) and item.get("reason")),
                          "PROTOCOL_REJECTED"))
        # Only Runtime-recorded evidence changes the generation, not new
        # Action IDs, prose, retries or merely executed=True.
        generation = json.dumps([
            [key, row.get("effective_verification_status"),
             row.get("evidence_status"), row.get("result_status")]
            for key, row in sorted(self.action_result_ledger.items())
            if isinstance(row, dict)
        ], ensure_ascii=False, sort_keys=True)
        decision = self._gar.planner_rejection(
            reason=reason, snapshot=snapshot, response_fresh=fresh,
            evidence_generation=hashlib.sha256(generation.encode("utf-8")).hexdigest(),
            local_repair_budget=repair_budget,
        )
        self._emit("gar_recovery_decision", request_id=self.run_id,
                   round=self.turn_id, **self._gar.diagnostic())
        self._gar_persist()
        if decision.status == "PREPARE":
            return self._gar_l4_activate(snapshot)
        if decision.status == "PAUSED":
            from agent_core.task_progress import set_runtime_state
            set_runtime_state(self.task_id, "PAUSED", reason=decision.reason,
                              root=self.progress_root)
            self._emit("gar_safe_pause", request_id=self.run_id,
                       round=self.turn_id, reason=decision.reason)
            raise RuntimeError("GAR safely paused: " + decision.reason)
        return decision.prompt if decision.status in {"REBASE", "REPLAN"} else None

    def _check_cancelled(self) -> None:
        if self.cancel_check is not None:
            self.cancel_check()

    def _admit_bounded_repair(self, diagnostics) -> dict:
        # Never treat an Action ID/prose change as progress. Only Runtime
        # records marked executed are considered a new evidence generation.
        evidence_generation = sum(
            1 for entry in self.action_result_ledger.values()
            if entry.get("executed") is True
        )
        budget = self._bounded_repair.observe(
            diagnostics, evidence_generation=evidence_generation
        )
        self._emit(
            "bounded_repair_observed",
            request_id=self.run_id,
            round=self.turn_id,
            **budget,
        )
        if budget["status"] == "EXHAUSTED" and self._gar is not None:
            # LOCAL_REPAIR_EXHAUSTED is a handoff, not a second termination
            # authority. GAR sees the same counters and enforces the hard
            # global cap; a Session switch never resets them.
            self._emit(
                "gar_local_repair_handoff",
                request_id=self.run_id, round=self.turn_id,
                rejected_turns_total=budget["rejected_turns_total"],
                rejected_turns_without_new_evidence=budget[
                    "rejected_turns_without_new_evidence"
                ],
                local_reason_codes=budget["reason_codes"],
            )
            return budget
        if budget["status"] == "EXHAUSTED":
            reason = "bounded_repair_exhausted_without_sufficient_new_evidence"
            set_runtime_state(
                self.task_id, "PAUSED", reason=reason, root=self.progress_root
            )
            self._emit(
                "protocol_loop_paused",
                request_id=self.run_id,
                round=self.turn_id,
                reason=reason,
                repair_budget=budget,
            )
            # PAUSED is not a completed task and creates no success receipt.
            raise RuntimeError(
                "SmartAgent 修復回合已達安全上限；保留既有證據並暫停。"
                + reason
            )
        return budget

    def _record_preexecution_predicate_rejection(self, calls, diagnostics) -> None:
        """Remember only a Runtime-verified, nonexecuted invalid fact Predicate.

        This records no filesystem fact, Tool result, or success evidence. An
        unrelated model statement or invalid action cannot create the blocker.
        """
        if (self.action_ledger or self.action_result_ledger or self.continuation_source
                or self.pending_result_recovery):
            return
        actions = [item for item in calls if item.get('tool') not in
                   {'report_progress', 'turn_commit'}]
        if len(actions) != 1 or actions[0].get('tool') != 'observe_fact':
            return
        action = actions[0]
        action_id = str(action.get('action_id', '') or '')
        predicate = action.get('predicate')
        if (not action_id or not isinstance(predicate, dict)
                or predicate.get('field') != 'file.exists'):
            return
        valid, diagnostic = validate_tool_envelope(action)
        reason = 'evidence_file_exists_boolean_required'
        if valid or not isinstance(diagnostic, dict) or diagnostic.get('reason') != reason:
            return
        if not any(item.get('reason') == reason for item in diagnostics):
            return
        self._protocol_spec_rejection = {
            'action_id': action_id, 'reason': reason,
            'request_id': self.run_id, 'task_epoch': self.task_epoch,
            'executed': False, 'verification_status': 'SPEC_INVALID',
        }

    def _safe_protocol_spec_rejection(self) -> dict:
        """Restrict protocol-blocked interruption to an action-free request."""
        record = dict(getattr(self, '_protocol_spec_rejection', {}) or {})
        if (not record or record.get('request_id') != self.run_id
                or record.get('task_epoch') != self.task_epoch
                or record.get('executed') is not False
                or self.action_ledger or self.action_result_ledger
                or self.continuation_source or self.pending_result_recovery):
            return {}
        return record

    def _restore_continuation(self):
        import copy
        from agent_core.task_continuation import ContinuationError
        source = self.continuation_source
        from agent_core.security_context import SecurityContext
        security = SecurityContext.from_agent(self.tools)
        for path in self.authorized_paths:
            security.require_read(path, require_absolute=True, forbid_glob=True)
        self.action_ledger = copy.deepcopy(source.get('actions', {}))
        self.action_result_ledger = copy.deepcopy(source.get('results', {}))
        # A committed cache preceding a result-envelope crash is still proof of
        # execution. Preserve it, but require a fresh verification for success.
        for action_id, cached in self.action_ledger.items():
            self.action_result_ledger.setdefault(action_id, {
                k: copy.deepcopy(cached[k]) for k in ('tool', 'operation', 'execution_status',
                'verification_status', 'executed', 'result_status', 'terminal_eligible') if k in cached})
        for action_id, prepared in source.get('prepared', {}).items():
            action = prepared.get('action', {})
            digest = action_signature(action)
            record = {'action_id':action_id, 'tool':action.get('tool'), 'action_digest':digest,
                      'execution_status':'UNKNOWN', 'verification_status':'UNKNOWN', 'executed':True,
                      'result_status':'UNKNOWN', 'terminal_eligible':False}
            self.action_result_ledger.setdefault(action_id, record)
            self.action_ledger.setdefault(action_id, {'signature':digest, 'action':dict(action),
                'result':json.dumps(record), 'execution_status':'UNKNOWN', 'verification_status':'UNKNOWN'})
        for action_id, record in self.action_result_ledger.items():
            if source.get('legacy') and (
                record.get('action_id') != action_id or record.get('request_id') != source['request_id'] or
                record.get('task_epoch') != source['epoch'] or
                not re.fullmatch(r'[0-9a-f]{64}', str(record.get('action_digest', ''))) or
                not isinstance(record.get('executed'), bool)):
                raise ContinuationError('legacy_action_provenance_invalid:' + action_id)
            if not record.get('tool') or not (record.get('action_digest') or self.action_ledger.get(action_id, {}).get('signature')):
                raise ContinuationError('continuation_action_record_incomplete:' + action_id)
            record.update(source_task_id=record.get('task_id', source['task_id']),
                          source_request_id=record.get('request_id', source['request_id']),
                          source_task_epoch=record.get('task_epoch', source['epoch']),
                          task_id=self.task_id, request_id=self.run_id, task_epoch=self.task_epoch,
                          action_id=action_id, evidence_status='STALE', continuation_import=True)
            from agent_core.task_continuation import unresolved_continuation_result
            if unresolved_continuation_result(record):
                record['evidence_status'] = 'ACTIVE'
            self.action_ledger.setdefault(action_id, {
                'signature': record['action_digest'], 'result': json.dumps(record, ensure_ascii=False),
                'action': {}, 'execution_status': record.get('execution_status', 'UNKNOWN')})
        # Provisional EXECUTING intents cannot disappear merely because the
        # worker died. The guard prevents replay until explicit reconciliation.
        if self.continuation_mode == 'RESUME' and not source.get('_reset_progress'):
            from agent_core.task_progress import TaskProgressLedger, _atomic_write, get_ledger_path
            saved = source.get('progress', {})
            if saved:
                progress = TaskProgressLedger.from_dict(saved)
                progress.task_id, progress.request_id, progress.goal = self.task_id, self.run_id, self.task_request
                progress.runtime_state, progress.decision, progress.outcome = 'PROCESSING', 'CONTINUE', 'PENDING'
                progress.interruption_reason = ''
                self.progress_ledger = progress
                _atomic_write(get_ledger_path(self.task_id, self.progress_root), progress.to_dict())
            for key, value in source.get('pending', {}).items():
                if key in {'pending_result_recovery', 'deferred_action_repairs', 'pending_verification_requirement'}:
                    setattr(self, key, copy.deepcopy(value))
            # Pending field repairs are bound to old request/epoch. Keep the
            # original action, but explicitly rebind them through this link.
            for key, value in source.get('pending', {}).get('pending_command_field_repairs', {}).items():
                rebound = copy.deepcopy(value)
                rebound.update(request_id=self.run_id, task_epoch=self.task_epoch)
                self.pending_command_field_repairs[key] = rebound
        self._emit('task_continuation_restored', request_id=self.run_id, task_id=self.task_id,
                   source_task_id=source['task_id'], mode=self.continuation_mode,
                   imported_action_ids=list(self.action_result_ledger))

    def _continuation_guard_source(self):
        source = dict(self.continuation_source or {})
        source['original_prepared'] = source.get('prepared', {})
        source['results'] = {key: value for key, value in self.action_result_ledger.items()
                             if value.get('continuation_import')}
        source['prepared'] = {key: value for key, value in source.get('prepared', {}).items()
            if (self.action_result_ledger.get(key) or {}).get('pending_resolution') != 'SCOPED_VERIFICATION_OBTAINED'}
        return source

    def _emit(self, event: str, **fields) -> None:
        if getattr(self, '_action_admission_probe', False):
            return
        if event == "tool_completed":
            PIT.record("15_runtime_tool_result", fields.get("result", ""), evidence_status="RUNTIME_OBSERVATION", extra={"action_id": fields.get("action_id"), "round": fields.get("round")})
        elif event == "runtime_terminal_accepted":
            PIT.record("16_terminal_receipt", fields, evidence_status="RUNTIME_OBSERVATION")
        if self.event_sink is not None:
            self.event_sink(event, **fields)
        if (self.continuation_store and self.progress_ledger and
                event in {'task_progress_updated', 'ack_accepted', 'protocol_rejected'}):
            self.continuation_store.snapshot(self)

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
                    "completion_contract": {
                        key: list(value) for key, value in dict(getattr(ledger, 'completion_contract', {}) or {}).items()
                    },
                    "decision": "CONTINUE",
                    "outcome": "PENDING",
                    "matched_condition": next(iter((getattr(ledger, 'completion_contract', {}) or {}).get('in_progress', [])), ''),
                    "evidence_refs": ["RUNTIME_STATUS"],
                    "decision_reason": "Runtime 已確認本輪 fresh image，須先執行授權的 PNG 保存，再依實際結果更新進度。",
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
                # Once any download actually ran, do not synthesize a new
                # attempt just because a failed artifact is still mounted.
                "artifact_auto_delivery_allowed": not any(
                    (entry.get('action') or {}).get('tool') == 'download_artifact'
                    for entry in self.action_ledger.values()
                ),
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
            "context_request": dict(getattr(ledger, 'context_request', {}) or {}),
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
            "latest_runtime_context": latest_context[-16000:] + "\n" + self._write_continuation_context(),
            "authorized_paths": list(self.authorized_paths),
            "context_capabilities": self._context_capabilities(),
            'request_context_epoch':self.task_epoch if self.request_context else '',
        }

    def _context_capabilities(self) -> dict:
        from agent_core.context_acquisition import context_capabilities
        results = {
            action_id: {**entry, 'result': (self.action_ledger.get(action_id) or {}).get('result', '')}
            for action_id, entry in self.action_result_ledger.items()
        }
        caps = context_capabilities(self.authorized_paths, SUPPORTED_ACTION_TOOLS, results)
        # No explicit target does not remove existing read permission. Do not
        # widen discovery to any parent or unrelated configured workspace.
        caps['discovery_paths'] = list(self.authorized_paths) or [str(self.workspace)]
        caps['target_path_confirmed'] = bool(self.authorized_paths)
        spec = self._safe_protocol_spec_rejection()
        if spec:
            caps['objective_blockers'].append({
                'action_id': spec['action_id'],
                'reason': 'PREDICATE_SPEC_INVALID',
                'executed': False,
                'evidence_ref': 'PROTOCOL_SPEC_REJECTION',
            })
        return caps

    def _write_continuation_context(self) -> str:
        manager = getattr(self.tools, "_chunked_write_manager", None)
        sessions = manager.open_sessions(request_id=self.run_id, run_id=self.run_id) if manager else []
        return "[RUNTIME_WRITE_CONTINUATION]\n" + json.dumps({
            "budget": write_budget_contract(), "open_transactions": sessions,
            "rule": "Continue only remaining content; OPEN is not a completed file. Do not replay accepted chunks.",
        }, ensure_ascii=False, sort_keys=True) + "\n[/RUNTIME_WRITE_CONTINUATION]"

    def _bound_cwd_command_contract(self, command: str = 'Start-Sleep -Seconds 4') -> dict:
        """Runtime-owned, no-execution scope preview for path-free commands.

        Explicit file fixtures remain under their own request authorization.
        The already-bound execution workspace is not a second fixture grant.
        """
        from agent_core.tools import _path_independent_command_must_use_bound_workspace
        if not _path_independent_command_must_use_bound_workspace(command):
            return {}
        requested = {
            'tool': 'run_command', 'command': command,
            'working_directory': str(self.workspace),
        }
        try:
            preview = self.tools.preview_tool_scope(requested)
        except (OSError, RuntimeError, ValueError) as exc:
            preview = {'allowed': False, 'error': type(exc).__name__ + ':' + str(exc)}
        return {
            'schema': 'SMARTAGENT_BOUND_COMMAND_WORKSPACE_V1',
            'command': command,
            'bound_workspace': str(self.workspace),
            'bound_workspace_admitted': bool(preview.get('allowed')),
            'bound_workspace_error': str(preview.get('error', '') or ''),
            'separate_original_file_scope': list(self.authorized_paths),
            'scope_rule': (
                'A literal bounded path-free wait or exact cwd-only query must run '
                'in the existing bound_workspace; a request-mentioned fixture '
                'is a file/data reference, not an implicit command cwd grant. '
                'No command or fixture was accessed by this preview.'
            ),
            'not_an_action_or_completed_evidence': True,
        }

    def _bound_cwd_command_guidance(self, command: str = 'Start-Sleep -Seconds 4') -> str:
        contract = self._bound_cwd_command_contract(command)
        return (
            '[RUNTIME_BOUND_COMMAND_WORKSPACE]\n'
            + json.dumps(contract, ensure_ascii=False, separators=(',', ':'))
            + '\n[/RUNTIME_BOUND_COMMAND_WORKSPACE]'
        ) if contract else ''

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
            + ('\n' + self._bound_cwd_command_guidance() if ('run_command' in str(getattr(self, 'task_request', '') or '').lower() and ( '等待' in str(getattr(self, 'task_request', '') or '') or 'sleep' in str(getattr(self, 'task_request', '') or '').lower() )) else '')
            + ('\n[REQUEST_CONTEXT_OBJECTS]\n'+json.dumps(self.request_context.catalog(),ensure_ascii=False)
               +'\n[/REQUEST_CONTEXT_OBJECTS]' if self.request_context else '')
            + "\n"
            + render_action_response_constraints()
            + "\n" + self._write_continuation_context()
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
        if self._safe_protocol_spec_rejection():
            refs.add('PROTOCOL_SPEC_REJECTION')
        return refs

    def _progress_repair_diagnostic(self, payload, error):
        from agent_core.progress_recovery import repair_diagnostic
        refs = {'REQUEST_ACCEPTED', 'RUNTIME_STATUS'}
        refs.update(str(action_id) for action_id, record in self.action_result_ledger.items()
                    if record.get('evidence_status', 'ACTIVE') == 'ACTIVE'
                    and record.get('request_id', self.run_id) == self.run_id
                    and record.get('task_epoch', self.task_epoch) == self.task_epoch)
        return repair_diagnostic(payload, self.progress_ledger, error, refs)

    def _runtime_evidence_context(self) -> str:
        evidence = []
        for action_id, item in self.action_result_ledger.items():
            evidence.append({
                "action_id": str(action_id),
                'evidence_status':item.get('evidence_status','ACTIVE'),
                'produced_round':item.get('produced_round'),
                'snapshot_id':item.get('snapshot_id',''),
                'superseded_by_action_id':item.get('superseded_by_action_id',''),
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
                "delivery_status": item.get("delivery_status", ""),
                "delivery_receipts": item.get("delivery_receipts", []),
            })
        all_refs = self._runtime_evidence_refs()
        if len(all_refs) > 24:
            recent_ids = {str(item.get("action_id", "")) for item in evidence[-16:]}
            progress_cited = set(getattr(self.progress_ledger, "evidence_refs", []) or [])
            filtered_refs = sorted({"REQUEST_ACCEPTED", "RUNTIME_STATUS"}
                                   | ({"PROTOCOL_SPEC_REJECTION"} if self._safe_protocol_spec_rejection() else set())
                                   | recent_ids | progress_cited)
        else:
            filtered_refs = sorted(all_refs)
        payload = {
            "available_refs": filtered_refs,
            "verification_status": self._effective_verification_status(),
            "legacy_last_verification_status": self.tools.last_verification_status,
            "execution_state": self.execution_state,
            "protocol_state": self.protocol_state,
            "action_evidence": evidence[-16:],
            "condition_evidence": list(self.condition_result_ledger.values())[-16:],
            "protocol_admission_evidence": (
                [self._safe_protocol_spec_rejection()]
                if self._safe_protocol_spec_rejection() else []
            ),
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
                    'evidence_status':recorded_evidence.get('evidence_status','ACTIVE'),
                    'produced_round':recorded_evidence.get('produced_round'),
                    'terminal_eligible':recorded_evidence.get('terminal_eligible',True),
                    'pending_resolution':recorded_evidence.get('pending_resolution',''),
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
            admission_records=list(getattr(self, 'deferred_action_repairs', {}).values()),
            available_tools=sorted(set(SUPPORTED_ACTION_TOOLS) | {'final_response'}),
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
        phases = []
        for item in actions:
            try:
                phases.append(phase_for_action(item))
            except ValueError:
                # Unknown tool names belong to schema repair, not a fatal
                # state-machine classifier exception before admission.
                continue
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
        if transition in {"PREREQUISITE_REQUIRED", "VERIFY_REQUIRED", "REPLAN_REQUIRED", "WAIT_RUNTIME"}:
            # Progress-only replies are admitted here so the existing bounded
            # dead-end/verification recovery can issue one complete prompt and
            # then pause.  Do not turn a missing action into repeated field-level
            # ACK repair.  When an action is present it must follow the state.
            # An honest interruption is decided by the objective blocker/user
            # input gate below, not forced into another doomed prerequisite.
            terminal_proposal = str(progress.get('decision', '')).upper() in {'COMPLETE','INTERRUPT'} and actual_tools == ['final_response']
            # Observation cannot resolve a mutation by itself, but it may gather
            # independent facts. Dedicated prerequisite checks remain below.
            if actual_tools and not terminal_proposal and any(
                item.get('tool') not in allowed and not self._independent_action(item) for item in operational
            ):
                return {
                    "marker": "[ACTION_LOOP_STATE_REJECTED]",
                    "reason": "required_transition_action_missing",
                    "detail": json.dumps({
                        "required_transition": transition,
                        "allowed_next_actions": sorted(allowed),
                        "actual_tools": actual_tools,
                        "blockers": state.get('blockers', []),
                    }, ensure_ascii=False, separators=(",", ":")),
                    "suggestion": "依 Runtime 狀態一次輸出一個允許的 prerequisite/verification action。",
                }
        if transition == 'WAIT_RUNTIME' and any(not self._independent_action(item) and item.get('tool') == 'run_command' for item in operational):
            return {'marker':'[ACTION_LOOP_STATE_REJECTED]', 'reason':'pending_action_requires_observation',
                'detail':'WAIT_RUNTIME permits inspection/verification, not another mutating command',
                'suggestion':'先取得 pending action 的客觀退出／交付／部分結果；不得盲目重送或開始另一個修改。'}
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

    def _independent_action(self, action):
        from agent_core.action_dependencies import is_observation
        if is_observation(action):
            return True
        # Opaque commands/bundles/plans cannot prove disjoint side effects.
        if action.get('tool') not in {'write_file','begin_file_write','delete_path'}:
            return False
        paths = self._action_resource_paths(action)
        blockers = list((self.action_loop_state or {}).get('blockers') or [])
        if not paths or not blockers:
            return False
        def overlap(left, right):
            a,b = Path(left).resolve(),Path(right).resolve()
            return a == b or a in b.parents or b in a.parents
        for blocker in blockers:
            ref = blocker.get('blocking_action_id','')
            previous = (self.action_ledger.get(ref) or {}).get('action') or (
                getattr(self,'deferred_action_repairs',{}).get(ref) or {}).get('action')
            if not previous:
                return False
            prior_paths = self._action_resource_paths(previous)
            if not prior_paths or any(overlap(a,b) for a in paths for b in prior_paths):
                return False
        return True

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
    def _valid_expected_failure_contract(action: dict) -> bool:
        """Negative BUILD/TEST assertions are already structured verification.

        Reuse canonical validation; a truthy dict alone must not exempt an
        ordinary mutation or a malformed/zero-exit expectation.
        """
        from agent_core.smartagent_protocol import validate_tool_envelope
        from agent_core.command_operation import normalize_command_operation
        try: operation = normalize_command_operation(action.get('operation'))
        except ValueError: return False
        if operation not in {'BUILD','TEST'} or not action.get('expected_failure'):
            return False
        valid, _ = validate_tool_envelope(action)
        return valid

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
        if (not checks or not all(isinstance(item, dict) and item for item in checks)) and not WebAgentProtocolLoop._valid_expected_failure_contract(action):
            missing.append("verify")
        return missing

    @staticmethod
    def _run_command_verification_detail(action: dict) -> str:
        """Describe a malformed verification contract without calling it missing."""
        from agent_core.command_operation import normalize_command_operation
        try:
            operation = normalize_command_operation(action.get('operation'))
        except (ValueError, TypeError) as exc:
            return 'invalid=operation:' + str(exc)
        observation = operation in {'INSPECT','GIT_INSPECT'}
        negative_assertion = WebAgentProtocolLoop._valid_expected_failure_contract(action)
        issues: list[str] = []
        if not observation and not str(action.get("success_criteria", "") or "").strip():
            issues.append("missing=success_criteria")
        verify = action.get("verify")
        if verify is None or verify == []:
            if not observation and not negative_assertion: issues.append("missing=verify")
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
        structured_text = text
        for marker in ("[CHUNKED_WRITE_SUCCESS]", "[CHUNKED_WRITE_FAILED]"):
            if text.startswith(marker):
                structured_text = text[len(marker):].strip()
                break
        try:
            structured = json.loads(structured_text)
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
        elif text.startswith("[CHUNKED_WRITE_FAILED]") or re.search(
            r"(?m)^\[WEBAGENT_ATTACHMENT_ERROR\]", text,
        ):
            execution = "FAILED"
        elif tool == "query_project":
            query_payload = structured if isinstance(structured, dict) else {}
            if str(query_payload.get("status", "") or "").upper() in {"REJECTED", "ERROR", "FAILED"}:
                execution = "FAILED"
        elif structured_status in {"REJECTED", "ERROR", "FAILED"} and tool not in {
            "aggregate_verification", "execute_frozen_plan",
        }:
            execution = "FAILED"
        if isinstance(structured, dict) and structured.get("execution_status") == "FAILED":
            execution = "FAILED"
        evidence_state, _missing = classify_evidence_state(
            action, text, execution, verification, expectation,
        )
        from agent_core.tool_result_contract import decode_result
        tool_outcome = decode_result(result)
        if tool_outcome and (tool_outcome.get('tool', tool) != tool or
            (action.get('action_id') and tool_outcome.get('action_id') and action['action_id'] != tool_outcome['action_id'])):
            tool_outcome = {**tool_outcome, 'status':'UNKNOWN', 'executed':False,
                'reason':'RESULT_IDENTITY_MISMATCH', 'data':{}}
        if tool_outcome:
            execution = {'SUCCEEDED':'SUCCEEDED', 'FAILED':'FAILED', 'REJECTED':'FAILED', 'UNKNOWN':'UNKNOWN',
                'QUEUED':'QUEUED', 'RUNNING':'RUNNING', 'PARTIAL':'PARTIAL'}[tool_outcome['status']]
            observed = tool_outcome.get('data') or {}
            if (tool=='aggregate_verification' and tool_outcome.get('executed') is True
                and observed.get('checks_completed') is True and observed.get('execution_status')=='SUCCEEDED'):
                # A completed checker may return a negative result. That is a
                # verification failure, not failure to execute the checker.
                execution='SUCCEEDED'
            from agent_core.tool_result_contract import completed_unknown_fact_observation
            completed_unknown_fact = (tool == 'observe_fact' and
                completed_unknown_fact_observation(tool_outcome, action.get('action_id', '')))
            admitted_verification = {'PASS','FAIL','UNVERIFIED','SPEC_INVALID'}
            if completed_unknown_fact:
                # A finished canonical probe can prove the observation is
                # indeterminate, not that a file is absent. Store that UNKNOWN
                # explicitly without admitting UNKNOWN claims from other tools.
                admitted_verification.add('UNKNOWN')
            if isinstance(observed, dict) and observed.get('verification_status') in admitted_verification:
                verification = observed['verification_status']
                expectation = observed.get('expectation_status', '')
            elif execution != 'SUCCEEDED':
                verification = 'UNKNOWN'
                expectation = ''
            evidence_state, _missing = classify_evidence_state(action, text, execution, verification, expectation)
        elif tool == 'delete_path':
            execution = 'UNKNOWN'
            evidence_state = 'MISSING'
        operation = str(action.get("operation", "") or "").upper()
        terminal_eligible = execution == "SUCCEEDED"
        if tool_outcome and tool_outcome.get('status') in {'QUEUED','RUNNING','PARTIAL','UNKNOWN','REJECTED'}:
            terminal_eligible = False
        if tool == "run_command" and execution in {'SUCCEEDED','FAILED'}:
            try:
                terminal_eligible = (
                    expectation == "PASS"
                    if expectation
                    else operation_terminal_eligible(operation, verification)
                )
            except ValueError:
                terminal_eligible = False
        if tool_outcome:
            from agent_core.tool_result_contract import outcome_terminal_eligible
            terminal_eligible = outcome_terminal_eligible(tool, tool_outcome, action.get('action_id',''))
        return {
            "tool": tool,
            "operation": operation,
            "execution_status": execution,
            "verification_status": verification,
            "expectation_status": expectation,
            "evidence_state": evidence_state,
            "terminal_eligible": terminal_eligible,
            'result_status': tool_outcome['status'] if tool_outcome else ('UNKNOWN' if tool == 'delete_path' else execution),
            # Some scoped inspection tools still return the legacy textual
            # rejection marker rather than a structured result envelope.
            # Preserve that denial reason for the bounded semantic guard;
            # this never changes authorization, executed or PASS/FAIL.
            'result_reason': (tool_outcome or {}).get('reason', '')
                or ('TOOL_SCOPE_REJECTED' if '[TOOL_SCOPE_REJECTED]' in text else ''),
            'executed': (tool_outcome or {}).get('executed', execution == 'SUCCEEDED'),
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
            if entry.get('request_id') and entry['request_id'] != self.run_id:
                continue
            if entry.get('task_epoch') and getattr(self,'task_epoch','') and entry['task_epoch'] != self.task_epoch:
                continue
            if entry.get('evidence_status') == 'STALE':
                continue
            successor = self.action_result_ledger.get(str(entry.get('superseded_by_action_id','')))
            if successor is not None and entry.get('evidence_status') == 'SUPERSEDED' and successor.get('evidence_status','ACTIVE') == 'ACTIVE':
                resolved[str(entry['superseded_by_action_id'])] = successor
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
            # A referenced UNKNOWN observation is authoritative uncertainty.
            # An unrelated earlier last_verification_status=PASS must never
            # mask its lack of confirmable evidence.
            if entries or refs:
                return "UNKNOWN"
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
        if condition_entry and bool(condition_entry.get('terminal_eligible',True)) and str(condition_entry.get("action_id", "") or "") in refs and (
            self.action_result_ledger.get(str(condition_entry.get('action_id',''))) or {}).get('evidence_status','ACTIVE') == 'ACTIVE':
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
            if execution in {'FAILED','REJECTED'} and expectation != "PASS":
                return "FAIL"
            if verification in {"FAIL", "SPEC_INVALID"}:
                return "FAIL"
            scoped_postcondition_pass = (entry.get('pending_resolution') == 'SCOPED_VERIFICATION_OBTAINED'
                                         and verification == 'PASS' and execution in {'UNKNOWN','PARTIAL'})
            if not bool(entry.get("effective_terminal_eligible", entry.get("terminal_eligible", True))) and not scoped_postcondition_pass:
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

    def _action_resource_paths(self, action: dict) -> list[str]:
        """Evidence impact only; these paths never grant authorization."""
        values = [action.get('path'), action.get('output_path')]
        if action.get('tool') == 'find_file':
            values.append(action.get('root') or self.workspace)
        if action.get('tool') in {'list_directory','read_file'} and not action.get('path'):
            values.append(self.workspace)
        if isinstance(action.get('paths'),list):
            values.extend(action['paths'])
        if action.get('tool') in {'project_sync','inspect_project_scope','inspect_project_working_set','inspect_semantic_map'}:
            values.append(action.get('project_root') or action.get('workspace') or self.workspace)
        if action.get('tool') == 'query_project':
            root = Path(action.get('project_root') or self.workspace)
            if not any(isinstance(row,dict) and row.get('path') for row in action.get('queries',[])):
                values.append(root)
            values.extend(root / str(row['path']) for row in action.get('queries',[]) if isinstance(row,dict) and row.get('path'))
        if action.get('tool') == 'commit_file_write':
            for item in self.action_ledger.values():
                original = item.get('action') or {}
                if original.get('tool') == 'begin_file_write' and original.get('write_id') == action.get('write_id'):
                    values.append(original.get('path'))
        plan = action.get('plan') or {}
        if isinstance(plan,dict):
            values.extend(item.get('path') if isinstance(item,dict) else item for item in plan.get('files_to_modify',[]) if isinstance(item,(dict,str)))
        checks = action.get('verify') or []
        checks = [checks] if isinstance(checks,dict) else checks
        if isinstance(checks,list):
            values.extend(item.get('path') for item in checks if isinstance(item,dict))
        paths = []
        from agent_core.path_security import canonicalize_path
        resource_base = self.workspace
        if action.get('tool') in {'validate_edit_plan','apply_edit_plan'}:
            resource_base = action.get('workspace') or self.workspace
        elif action.get('tool') == 'run_command':
            resource_base = action.get('working_directory') or self.workspace
        try:resource_base = canonicalize_path(resource_base,base=self.workspace)
        except (TypeError,ValueError,OSError):return []
        for value in values:
            if value:
                try:
                    paths.append(str(canonicalize_path(value,base=resource_base)))
                except (TypeError,ValueError,OSError):
                    continue
        return list(dict.fromkeys(paths))

    def _reconcile_read_attachment_delivery(self, attachments, started_at, expected):
        """Refresh staged reads/uploads from trusted transport records, without replay."""
        if not attachments:
            return
        from agent_core.paths import source_root
        from agent_core.tool_result_contract import decode_result, attach_runtime_result
        from agent_core.web_attachment_delivery import confirmed_delivery
        for action_id, cached in self.action_ledger.items():
            action = cached.get('action') or {}
            entry = self.action_result_ledger.get(action_id) or {}
            outcome = decode_result(cached.get('result', '')) or {}
            tool = action.get('tool')
            if (tool not in {'read_file', 'upload_file', 'upload_files'}
                    or outcome.get('status') not in {'QUEUED', 'PARTIAL'}
                    or entry.get('request_id') != self.run_id
                    or entry.get('task_epoch') != expected.get('task_epoch')):
                continue
            try:
                receipts = confirmed_delivery(source_root(), request_id=self.run_id,
                    task_epoch=expected.get('task_epoch'), paths=attachments,
                    started_at=started_at, artifacts=(outcome.get('data') or {}).get('artifacts', []))
            except (OSError, ValueError, TypeError) as exc:
                self._emit('attachment_delivery_receipt_unavailable', request_id=self.run_id,
                           action_id=action_id, error=f'{type(exc).__name__}: {exc}')
                continue
            if not receipts:
                continue
            partial = bool(outcome['data'].get('errors')) or outcome.get('status') == 'PARTIAL'
            data = {**outcome['data'], 'delivery_status':'PARTIAL_DELIVERY' if partial else 'DELIVERED_TO_CONVERSATION',
                    'delivery_receipts':receipts, 'verification_status':'PASS',
                    'verification_scope':'TRANSPORT_ONLY', 'content_read':False}
            if partial:
                data.update(verification_status='UNVERIFIED', terminal_eligible=False)
            updated = attach_runtime_result(cached['result'], 'PARTIAL' if partial else 'SUCCEEDED',
                'ATTACHMENT_PARTIAL_DELIVERY_CONFIRMED' if partial else
                'READ_ATTACHMENT_DELIVERY_CONFIRMED' if tool == 'read_file' else 'ATTACHMENT_DELIVERY_CONFIRMED',
                executed=True, requested_path=outcome.get('requested_path',''),
                resolved_path=outcome.get('resolved_path'), data=data, tool=tool,
                request_id=self.run_id, task_id=self.task_id, action_id=action_id)
            # Keep the staging result for audit; current result reflects delivery.
            cached.setdefault('transport_history', []).append(outcome)
            cached['result'] = updated
            facts = self._classify_action_evidence(action, updated)
            cached.update(facts)
            entry.update(facts, effective_verification_status='UNVERIFIED' if partial else 'PASS',
                effective_terminal_eligible=not partial, delivery_status=data['delivery_status'],
                delivery_receipts=receipts, last_verified_round=self.turn_id)
            self._emit('attachment_delivery_confirmed', request_id=self.run_id,
                action_id=action_id, receipts=receipts, content_read=False,
                verification_scope='TRANSPORT_ONLY')
        self._refresh_action_loop_state()
        if self.continuation_store:
            self.continuation_store.snapshot(self)

    def _reconcile_committed_write_repairs(self, action: dict, producer: dict) -> None:
        """Retire only proven equivalent, never-executed single-chunk repairs.

        A new action ID may finish a previously blocked transaction. Retaining
        its old chunk/commit then asks the planner to replay a closed write.
        Do not infer equivalence for repeated or multi-chunk content.
        """
        if (action.get('tool') != 'commit_file_write' or producer.get('status') != 'SUCCEEDED'
                or producer.get('executed') is not True):
            return
        cached = self.action_ledger.get(action.get('action_id'), {})
        try:
            receipt = json.loads(cached['result'].split('] ', 1)[1])
        except (KeyError, ValueError, TypeError):
            return
        if (receipt.get('state') != 'COMMITTED' or receipt.get('received_chunks') != 1
                or receipt.get('write_id') != action.get('write_id')):
            return
        write_id = action.get('write_id')
        ignored = {'action_id', 'depends_on', 'replaces_action_id', 'field_refs'}
        def payload(value):
            result = {k:v for k,v in value.items() if k not in ignored}
            if result.get('tool') == 'write_file_chunk':
                result.setdefault('content_encoding', 'utf-8')
            return result
        pending = getattr(self, 'deferred_action_repairs', {})
        for old_id, repair in list(pending.items()):
            old = repair.get('action') or {}
            if (old_id in self.action_ledger or repair.get('executed') is not False
                    or old.get('write_id') != write_id
                    or old.get('tool') not in {'write_file_chunk','commit_file_write'}):
                continue
            matches = []
            for new_id, record in self.action_ledger.items():
                actual = record.get('action') or {}
                evidence = self.action_result_ledger.get(new_id) or {}
                if (new_id != old_id and payload(actual) == payload(old)
                        and evidence.get('request_id') == self.run_id
                        and evidence.get('execution_status') == 'SUCCEEDED'
                        and evidence.get('executed') is True):
                    matches.append(new_id)
            if len(matches) != 1 or sum(payload((r.get('action') or {})) == payload(old) for r in pending.values()) != 1:
                continue
            if old.get('tool') == 'write_file_chunk':
                if old.get('content_encoding','utf-8') != 'utf-8' or not isinstance(old.get('content'),str):
                    continue
                data = old['content'].encode('utf-8')
                if receipt.get('received_bytes') != len(data) or receipt.get('output_sha256') != hashlib.sha256(data).hexdigest():
                    continue
            proof = {**repair, 'status':'SUPERSEDED', 'superseded_by_action_id':matches[0],
                     'commit_action_id':action['action_id'], 'reason':'COMMITTED_EQUIVALENT_ACTION_PROVEN'}
            self.action_ledger[action['action_id']].setdefault('resolved_admission_repairs', []).append(proof)
            pending.pop(old_id)
            self.current_action_repairs = [r for r in getattr(self,'current_action_repairs',[]) if r.get('action_id') != old_id]
            self._emit('action_repair_superseded', request_id=self.run_id, round=self.turn_id, **proof)

    def _record_action_result_evidence(self, action: dict, ledger_entry: dict) -> dict:
        """Append verification history and apply an explicit scoped supersession."""
        action_id = str(action.get("action_id", "") or "")
        existing = self.action_result_ledger.get(action_id)
        if existing is not None:
            # Exactly-once cache replay is not a new execution/evidence event.
            # Preserve its original round and newer effective verification.
            self._emit('action_evidence_cache_reused', request_id=self.run_id, round=self.turn_id, action_id=action_id)
            return dict(existing)
        ledger_entry.update(request_id=self.run_id, task_id=getattr(self,'task_id',''),
                            task_epoch=getattr(self,'task_epoch',''), produced_round=self.turn_id,
                            evidence_status='ACTIVE', resource_paths=self._action_resource_paths(action))
        from agent_core.tool_result_contract import decode_result
        producer = decode_result(str((self.action_ledger.get(action_id) or {}).get('result',''))) or {}
        ledger_entry['snapshot_id'] = (producer.get('data') or {}).get('snapshot_id','')
        changed = action.get('tool') in {'write_file','delete_path','apply_edit_plan','web_edit_file','commit_file_write','execute_artifact_bundle','execute_frozen_plan'} or (
            action.get('tool') == 'run_command' and action.get('operation') in {'MUTATE','GIT_MUTATE','BUILD','GENERAL'})
        if action.get('tool') == 'run_command' and action.get('operation') in {'INSPECT','GIT_INSPECT'}:
            from agent_core.action_dependencies import command_is_proven_observation
            changed = not command_is_proven_observation(action)
        may_have_changed = ledger_entry.get('execution_status') in {'SUCCEEDED','PARTIAL','UNKNOWN'} or (
            ledger_entry.get('execution_status') == 'FAILED' and ledger_entry.get('executed') is True)
        if changed and may_have_changed:
            touched = [Path(value) for value in ledger_entry['resource_paths']]
            if not touched:
                touched = [self.workspace]
            for old_id, old in self.action_result_ledger.items():
                if old_id == action_id:
                    continue
                affected = any(a == b or a in b.parents or b in a.parents
                    for a in touched for b in (Path(value) for value in old.get('resource_paths',[])))
                if affected:
                    old.update(evidence_status='STALE', stale_by_action_id=action_id)
                    self._emit('action_evidence_staled', request_id=self.run_id, round=self.turn_id,
                               action_id=old_id, changed_by_action_id=action_id)
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
        from agent_core.tool_result_contract import completed_unknown_fact_observation
        completed_unknown_fact = (action.get('tool') == 'observe_fact'
            and completed_unknown_fact_observation(producer, action_id)
            and bool(condition_id) and action.get('predicate') is not None)
        if completed_unknown_fact:
            # Only Runtime-owned, actually attempted UNKNOWN observations are
            # condition observations. A missing/rejected action is not one.
            observed_status = "UNKNOWN"
            expectation_status = "UNKNOWN"
        status = expectation_status if expectation_status in {"PASS", "FAIL", "SPEC_INVALID"} else observed_status
        fact_data = (producer.get('data') or {}) if action.get('tool') == 'observe_fact' else {}
        ledger_entry['observation_status'] = str(fact_data.get('observation_status', '') or '')
        ledger_entry['comparison'] = str(fact_data.get('comparison', '') or '')
        ledger_entry['execution_finality'] = str(fact_data.get('execution_finality', '') or '')
        ledger_entry['expectation_status'] = expectation_status
        ledger_entry["condition_id"] = condition_id
        ledger_entry["verifies_action_id"] = verifies_action_id
        ledger_entry["effective_verification_status"] = status
        ledger_entry["effective_expectation_status"] = expectation_status
        ledger_entry.setdefault("verification_history", [])
        if status in {"PASS", "FAIL", "UNVERIFIED", "SPEC_INVALID"} or completed_unknown_fact:
            verification_id = "VER-" + uuid.uuid4().hex[:12].upper()
            prior_id = str(target.get("effective_verification_id", "") or "") if target else ""
            record = {
                "verification_id": verification_id,
                "action_id": action_id,
                "status": status,
                "observed_verification_status": observed_status,
                "expectation_status": expectation_status,
                "observation_status": ledger_entry["observation_status"],
                "comparison": ledger_entry["comparison"],
                "execution_finality": ledger_entry["execution_finality"],
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
                target['effective_expectation_status'] = expectation_status
                if target.get('tool') == 'run_command':
                    target['effective_terminal_eligible'] = (
                        target.get('execution_status') == 'SUCCEEDED' and
                        operation_terminal_eligible(target.get('operation', 'GENERAL'), status))
                else:
                    # delete/write/etc. have no command operation enum. A
                    # linked verifier updates their postcondition, not their
                    # historical execution identity.
                    target['effective_terminal_eligible'] = target.get('execution_status') == 'SUCCEEDED' and status == 'PASS'
                target['evidence_status'] = 'ACTIVE'
                target['last_verified_round'] = self.turn_id
                if target.get('execution_status') in {'UNKNOWN','PARTIAL'} and status in {'PASS','FAIL'}:
                    # Historical execution remains UNKNOWN/PARTIAL. A linked
                    # current verifier resolves the pending postcondition only.
                    target['pending_resolution'] = 'SCOPED_VERIFICATION_OBTAINED'
        self.action_result_ledger[action_id] = ledger_entry
        if (action.get('tool') == 'repair_task_plan'
                and ledger_entry.get('result_status') == 'SUCCEEDED'
                and ledger_entry.get('result_reason') == 'PLAN_FROZEN'
                and ledger_entry.get('executed') is True):
            # Preserve the rejected receipt, but resolve only the exact Runtime
            # repair prerequisite it produced. Unrelated FAILs remain active.
            submitted = {key:value for key,value in action.items() if key != 'action_id'}
            for prior_id, prior in self.action_result_ledger.items():
                original = self.action_ledger.get(prior_id) or {}
                recovery = original.get('recovery_context') or {}
                remedies = recovery.get('next_actions') or []
                if (prior_id != action_id and prior.get('executed') is False
                        and prior.get('result_status') == 'REJECTED'
                        and recovery.get('active') is True
                        and recovery.get('required_action') == 'repair_task_plan'
                        and any({k:v for k,v in remedy.items() if k != 'action_id'} == submitted
                                for remedy in remedies if isinstance(remedy,dict))):
                    prior['evidence_status'] = 'SUPERSEDED'
                    prior['superseded_by_action_id'] = action_id
        if action.get('tool') in {'commit_file_write','abort_file_write'} and ledger_entry.get('execution_status') == 'SUCCEEDED':
            for prior_id, prior in self.action_result_ledger.items():
                prior_action = (self.action_ledger.get(prior_id) or {}).get('action') or {}
                if prior_id != action_id and prior_action.get('tool') in {'begin_file_write','write_file_chunk'} and prior_action.get('write_id') == action.get('write_id'):
                    prior['evidence_status'] = 'SUPERSEDED'
                    prior['superseded_by_action_id'] = action_id
            requirement = self.pending_verification_requirement or {}
            if requirement.get('required_action') == 'commit_file_write' and requirement.get('write_id') == action.get('write_id'):
                self.pending_verification_requirement = {}
        if condition_id:
            self.condition_result_ledger[condition_id] = {
                "condition_id": condition_id,
                "action_id": action_id,
                "status": status,
                "verification_status": status,
                "observed_verification_status": observed_status,
                "expectation_status": expectation_status,
                "observation_status": ledger_entry["observation_status"],
                "comparison": ledger_entry["comparison"],
                "execution_finality": ledger_entry["execution_finality"],
                "effective_verification_status": status,
                "execution_status": str(
                    ledger_entry.get("execution_status", "UNKNOWN") or "UNKNOWN"
                ).upper(),
                "terminal_eligible": bool(ledger_entry.get("terminal_eligible", True)),
                "verification_id": str(ledger_entry.get("verification_id", "") or ""),
                "round": self.turn_id,
            }
        self._reconcile_committed_write_repairs(action, producer)
        return ledger_entry

    def _terminal_recovery_requirement(self, progress: dict, verdict: str) -> dict:
        """Select a remedy from objective referenced outcomes, never prose."""
        refs = list(progress.get('evidence_refs') or [])
        condition = str(progress.get('matched_condition','')).strip() or '取得支持 terminal SUCCESS 的 request-scoped evidence'
        requirement = {'missing_condition':condition,'condition_id':self._canonical_condition_id(condition),
            'current_verification_status':verdict,'evidence_refs':refs,
            'required_action':'run_command','required_fields':['operation','command','success_criteria','verify'],
            'supported_verify_actions':['run_command','aggregate_verification'],
            'supported_verify_checks':['file_exists','file_contains','expect_regex'], 'next_actions':[]}
        from agent_core.tool_result_contract import decode_result
        for ref in refs:
            entry = self.action_result_ledger.get(str(ref)) or {}
            if not entry:
                continue
            action_entry = self.action_ledger.get(str(ref)) or {}
            action = action_entry.get('action') or {}
            outcome = decode_result(str(action_entry.get('result',''))) or {}
            status = str(entry.get('execution_status','UNKNOWN')).upper()
            if entry.get('evidence_status') == 'SUPERSEDED':
                continue
            if entry.get('evidence_status') == 'STALE' or (entry.get('request_id') and entry['request_id'] != self.run_id):
                read_action = action.get('tool') in {'query_project','read_file','list_directory','inspect_directory','inspect_project_scope','project_sync'}
                requirement.update(reason_code='STALE_EVIDENCE',blocking_action_id=ref,
                    missing_condition='刷新受影響的證據，不能沿用過期或其他 request 的結果',
                    required_action=action.get('tool','') if read_action else 'run_command',
                    supported_verify_actions=[action.get('tool','')] if read_action else ['run_command','aggregate_verification'],
                    next_actions=[{k:v for k,v in action.items() if k != 'action_id'}] if read_action
                                 else [{'tool':'run_command','operation':'VERIFY','verifies_action_id':ref}])
                break
            if action.get('tool') in {'begin_file_write','write_file_chunk'}:
                write_id = action.get('write_id','')
                requirement.update(reason_code='OPEN_WRITE_TRANSACTION',blocking_action_id=ref,write_id=write_id,
                    missing_condition='取得相同 write_id 的實際 commit 結果；收到 chunk 不代表目的檔案已保存',
                    required_action='commit_file_write',required_fields=['write_id'],supported_verify_actions=['commit_file_write'],
                    next_actions=[{'tool':'commit_file_write','write_id':write_id}])
                break
            from agent_core.tool_result_contract import completed_unknown_fact_observation
            if completed_unknown_fact_observation(outcome, ref):
                # This read has completed. An indeterminate fact is not an
                # unfinished command, and MUST NOT demand a substitute tool.
                requirement.update(reason_code='FACT_OBSERVATION_INDETERMINATE',
                    blocking_action_id=ref, owner='runtime',
                    missing_condition='已完成 observe_fact，但檔案存在性無法確認',
                    required_action='', required_fields=[], supported_verify_actions=[],
                    supported_verify_checks=[], next_actions=[],
                    release_condition='NEW_CONFIRMABLE_FACT_OR_OBJECTIVE_INTERRUPTION')
                break
            if status in {'QUEUED','RUNNING','PARTIAL','UNKNOWN'}:
                requirement.update(reason_code=status+'_NOT_COMPLETE',blocking_action_id=ref,
                    missing_condition='等待實際交付確認' if status=='QUEUED' else '確認實際執行狀態；禁止盲目重送原 action',
                    owner='transport' if status=='QUEUED' else 'runtime',
                    resolved_path=outcome.get('resolved_path'),
                    process_id=(outcome.get('data') or {}).get('process_id'))
                if status=='QUEUED':
                    requirement.update(required_action='',required_fields=[],supported_verify_actions=[],
                        supported_verify_checks=[],release_condition='DELIVERY_CONFIRMED')
                break
            if status=='SUCCEEDED' and not entry.get('terminal_eligible',True):
                requirement.update(reason_code='POSTCONDITION_REQUIRED',blocking_action_id=ref,
                    next_actions=[{'tool':'run_command','operation':'VERIFY','verifies_action_id':ref}])
                break
        # Terminal recovery verifies a task condition, not merely an action.
        # Give the planner the complete binding rather than asking for a
        # linked verifier which cannot populate condition_result_ledger.
        if requirement.get('reason_code') in {None,'POSTCONDITION_REQUIRED'}:
            requirement['required_fields'].append('condition_id')
            if not requirement['next_actions']:
                requirement['next_actions']=[{'tool':'run_command','operation':'VERIFY'}]
            for remedy in requirement['next_actions']:
                if remedy.get('tool')=='run_command' and remedy.get('operation')=='VERIFY':
                    remedy['condition_id']=requirement['condition_id']
        return requirement

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
        if requirement.get('reason_code') == 'FACT_OBSERVATION_INDETERMINATE':
            return {
                "marker": "[WEBAGENT_VERIFICATION_REQUIRED]",
                "reason": reason, "detail": detail,
                "suggestion": (
                    "observe_fact 已完成，但 Observation/Predicate 是 UNKNOWN，不能宣告 SUCCESS。"
                    "若還有合法觀測方法，可提出不同的 Action；若原始任務禁止替代工具，"
                    "可使用 decision=INTERRUPT、outcome=UNKNOWN、context_request.kind=BLOCKED，"
                    "引用 blocking_action_id 和該 observe_fact evidence_ref，禁止盲目重送。"
                ),
            }
        return {
            "marker": "[WEBAGENT_VERIFICATION_REQUIRED]",
            "reason": reason,
            "detail": detail,
            "suggestion": (
                "保留既有進度，依 reason_code、missing_condition 與 next_actions 一次補齊必要 action。"
                "不要重做已執行操作。queued/running 不是完成；只報仍在等待或依客觀阻塞中斷。"
                "下一輪使用 decision=CONTINUE，並輸出 Runtime 列出的 canonical prerequisite/verification action。"
                "使用 run_command 時必須同時包含 operation、command、描述精確成功條件的 success_criteria，以及非空 verify；"
                "補驗證的 operation 固定為 VERIFY；若有原 action，使用 verifies_action_id；"
                "若 next_actions 同時列出 condition_id，必須連同原 action 關聯一起回覆，才能綁定任務驗收條件；"
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

    def _accepted_terminal_receipt(self, *, runtime_state: str) -> dict:
        """Create an auditable receipt only after a terminal turn is admitted.

        The receipt attests admission, NOT that a failed/unknown predicate passed.
        """
        progress = self.progress_ledger
        refs = [str(item) for item in list(progress.evidence_refs or [])]
        # Audit the unredacted source IDs; the sanitized report may reuse
        # harmless aliases. Never turn report aliases into admission evidence.
        from agent_core.evidence_identity_audit import audit_terminal_references
        identity_audit = audit_terminal_references(
            refs, self.action_result_ledger, self.run_id, self.task_epoch,
            protocol_spec_rejection=self._safe_protocol_spec_rejection(),
        )
        self._emit("terminal_evidence_identity_audit",
            request_id=self.run_id, task_id=self.task_id, round=self.turn_id,
            **identity_audit)
        # A terminal admission must never override a SUSPECT reference audit.
        # Rejection is not an Observation; refuse UNKNOWN/foreign evidence
        # instead of issuing an ACCEPTED receipt that masks identity failure.
        if identity_audit["identity_status"] != "SCOPED":
            raise RuntimeError(
                "terminal_reference_identity_not_scoped:"
                + str(identity_audit["identity_reason"])
            )
        payload = {
            "schema": "SMARTAGENT_TERMINAL_ACCEPTANCE_V1",
            "admission_status": "ACCEPTED",
            "request_id": self.run_id,
            "task_id": self.task_id,
            "task_epoch": self.task_epoch,
            "round": self.turn_id,
            "ack_id": self.accepted_ack_ids[-1] if self.accepted_ack_ids else "",
            "decision": progress.decision,
            "task_outcome": progress.outcome,
            "runtime_state": runtime_state,
            "matched_condition_id": progress.matched_condition_id,
            "evidence_refs": refs,
            "evidence_verdict": self._terminal_evidence_verdict(
                refs, matched_condition=progress.matched_condition),
            "verification_status": self._effective_verification_status(
                refs, matched_condition=progress.matched_condition),
        }
        # Include the distinct answer-only admission type in the receipt ID.
        # An evidence-free text reply cannot collide with an ordinary receipt.
        if self._answer_only_admitted:
            payload["completion_kind"] = "ANSWER_ONLY"
            payload["admission_reason"] = "ANSWER_ONLY_RESPONSE_ADMITTED"
        signed = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        payload["receipt_id"] = "TERM-" + hashlib.sha256(signed.encode('utf-8')).hexdigest()[:20].upper()
        self.terminal_acceptance = payload
        self._emit("runtime_terminal_accepted", **payload)
        return payload

    @staticmethod
    def _read_only_unexecuted_repairs(repairs) -> bool:
        """Only optional, unexecuted reads may be left behind on blocked terminal."""
        from agent_core.action_dependencies import OBSERVATION_TOOLS
        for item in repairs:
            if not isinstance(item, dict) or item.get('executed') is True:
                return False
            action = item.get('action') if isinstance(item.get('action'), dict) else item
            if str(action.get('tool','')) not in OBSERVATION_TOOLS:
                return False
        return True

    def _failed_predeclared_fact(self, progress: dict) -> bool:
        """Evidence-backed FAILED exception; cannot use an unrelated error."""
        if (str(progress.get('decision','')).upper() != 'COMPLETE'
                or str(progress.get('outcome','')).upper() != 'FAILED'):
            return False
        contract = dict(getattr(self.progress_ledger, 'completion_contract', {}) or {})
        supplied = progress.get('completion_contract')
        if isinstance(supplied, dict):
            contract['success'] = list(contract.get('success') or []) + list(supplied.get('success') or [])
        allowed = set(str(x) for x in contract.get('success') or [])
        allowed.update(item['condition_id'] for item in completion_condition_choices(
            contract, 'success'))
        refs = [str(ref) for ref in list(progress.get('evidence_refs') or [])]
        if not refs or self._terminal_evidence_verdict(refs) != 'FAIL':
            return False
        return any((entry.get('tool') == 'observe_fact'
            and entry.get('evidence_status','ACTIVE') == 'ACTIVE'
            and str(entry.get('request_id',self.run_id)) == self.run_id
            and str(entry.get('task_epoch',self.task_epoch)) == self.task_epoch
            and str(entry.get('expectation_status','')).upper() == 'FAIL'
            and str(entry.get('condition_id','')) in allowed)
            for ref in refs for entry in [self.action_result_ledger.get(ref) or {}])

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
        from copy import deepcopy
        pending = getattr(self, 'pending_command_field_repairs', {})
        merged = []
        for call in calls:
            ref = call.get('action_id')
            saved = pending.get(ref) if isinstance(ref,str) else None
            if saved:
                if (saved['request_id'] != self.run_id or saved['task_epoch'] != getattr(self,'task_epoch','')
                    or ref in self.action_ledger or ref in self.action_result_ledger):
                    return [], [{'reason':'command_repair_target_not_pending','detail':ref}]
                original = saved['action']
                for name,value in call.items():
                    if name not in ({'verify','success_criteria'} | set(saved.get('required_fields', []))) and value != original.get(name):
                        return [], [{'reason':'command_repair_identity_changed','detail':name,
                            'suggestion':'命令、operation、目錄或依賴改變時，使用新 action_id 與 replaces_action_id；不得沿用舊驗證。'}]
                materialized = {**deepcopy(original), **deepcopy(call)}
                merged.append(materialized)
                self._emit('command_field_repair_merged',request_id=self.run_id,round=self.turn_id,
                    action_id=ref,patched_fields=sorted(set(call)-{'tool','action_id'}))
            else:
                merged.append(call)
        admitted, diagnostics = self._accept_action_batch(merged, expected)
        for action in admitted:
            ref = action.get('action_id')
            pending.pop(ref,None)
            pending.pop(action.get('replaces_action_id'),None)
        deferred_diagnostics = [item for repair in getattr(self,'current_action_repairs',[])
            for item in repair.get('diagnostics',[])]
        repair_sources = diagnostics + deferred_diagnostics
        repair_diagnostic = any(item.get('reason') in {'run_command_verification_contract_required','wrong_field_type','run_command_operation_invalid'}
            for item in repair_sources)
        if repair_diagnostic:
            for action in merged:
                ref = action.get('action_id')
                if action.get('tool') != 'run_command' or not isinstance(ref,str) or ref in self.action_ledger:
                    continue
                if not isinstance(action.get('command'),str) or not action['command'].strip():
                    continue
                fields = []
                try:
                    detail = self._run_command_verification_detail(action)
                except (ValueError,TypeError):
                    continue
                for field in ('operation','verify','success_criteria'):
                    if field in detail or (field in action and not isinstance(action[field], (list,dict) if field=='verify' else str)):
                        fields.append(field)
                if not fields: continue
                if len(pending) >= 32 and ref not in pending: continue
                saved = {'request_id':self.run_id,'task_epoch':getattr(self,'task_epoch',''),
                    'action':deepcopy(action),'required_fields':fields}
                pending[ref] = saved
                self.deferred_action_repairs[ref] = {'action_id':ref,'tool':'run_command',
                    'status':'REPAIR_REQUIRED','executed':False,'action':deepcopy(action),'diagnostics':repair_sources}
                for diagnostic in repair_sources:
                    if diagnostic.get('reason') in {'run_command_verification_contract_required','wrong_field_type','run_command_operation_invalid'}:
                        diagnostic.setdefault('command_field_repairs',[]).append({
                            'action_id':ref,'tool':'run_command','required_fields':fields,
                            'reply_example':{'tool':'run_command','action_id':ref,**{field:
                                ([{'action':'run_command','command':'<read-only verification command>','expect_exit_code':0}]
                                 if field=='verify' else ('INSPECT|MUTATE|BUILD|TEST|VERIFY|GIT_INSPECT|GIT_MUTATE|PROCESS|TRANSFER|GENERAL' if field=='operation' else '<describe the expected observable result>')) for field in fields}},
                            'rule':'Only patch these fields. Runtime restores the pending action and revalidates it; it has NOT executed.'})
        self.pending_command_field_repairs = pending
        return admitted, diagnostics

    def _probe_action_admission(self, calls, expected, *, single=False):
        from copy import deepcopy
        names = ('pending_result_recovery', 'pending_verification_requirement',
                 'pending_progress_repair_actions', 'pending_action_repair',
                 'read_only_progress_deferred', 'deferred_progress_repair', 'active_stage_manifest')
        saved = {name: deepcopy(getattr(self, name, None)) for name in names}
        self._action_admission_probe = True
        self._single_action_probe = single
        try:
            return self._accept_v8_ack(deepcopy(calls), expected)
        finally:
            self._action_admission_probe = False
            self._single_action_probe = False
            for name, value in saved.items():
                setattr(self, name, value)

    def _accept_action_batch(self, calls, expected):
        """Keep transport/Progress global; partition operational admission only."""
        from copy import deepcopy
        from agent_core.action_dependencies import dependency_diagnostic
        from agent_core.smartagent_protocol import normalize_tool_numeric_fields
        calls = [normalize_tool_numeric_fields(item) for item in calls]
        resolution_errors = {}
        for index,item in enumerate(calls):
            if 'field_refs' not in item:
                continue
            from agent_core.request_context import RequestContextError
            try:
                if not self.request_context: raise RequestContextError('request_context_unavailable')
                if item.get('tool') in {'report_progress','final_response','turn_commit'}:
                    raise RequestContextError('request_context_control_reference_forbidden')
                calls[index]=self.request_context.expand(item)
                self._emit('action_context_refs_expanded', request_id=self.run_id,round=self.turn_id,
                    action_id=item.get('action_id',''),field_refs=item['field_refs'])
            except RequestContextError as exc:
                resolution_errors[item.get('action_id','')]={'reason':str(exc),'detail':'field_refs',
                    'suggestion':'選擇本 request 的正確 field/ref_id/version，或重答該欄位的 literal；不會自動填入舊值。'}
        if not hasattr(self, 'deferred_action_repairs'):
            self.deferred_action_repairs = {}
        self.current_action_repairs = []
        # No partition of missing/invalid commits, duplicate IDs, terminal mixes
        # or ambiguous Progress. Every projected batch derives from this commit.
        if not calls or calls[-1].get('tool') != 'turn_commit':
            return self._accept_v8_ack(calls, expected)
        try:
            v8_validate_model_commit(calls[-1], len(calls)-1)
        except ProtocolV8Error:
            return self._accept_v8_ack(calls, expected)
        body = calls[:-1]
        ids = [item.get('action_id') for item in body]
        if any(not isinstance(ref, str) or not ref.strip() for ref in ids) or len(set(ids)) != len(ids):
            return [], [{'marker':'[WEBAGENT_V8_REJECTED]', 'reason':'invalid_or_duplicate_action_id',
                         'detail':'Action identity must be unique before partial admission', 'suggestion':'修正 action ID，不假設任何操作已執行。'}]
        progress = [item for item in body if item.get('tool') == 'report_progress']
        operational = [item for item in body if item.get('tool') != 'report_progress']
        if self.deferred_action_repairs and len(progress) == 1 and progress[0].get('decision') == 'COMPLETE':
            return [], [{'marker':'[WEBAGENT_ACTION_REPAIR_REQUIRED]', 'reason':'unresolved_action_repairs',
                'detail':json.dumps(list(self.deferred_action_repairs.values()),ensure_ascii=False),
                'suggestion':'只補未執行的 action；成功操作不要重跑。依賴失敗使用新前置 action ID，再更新依賴。'}]
        if any(item.get('tool') == 'final_response' for item in operational):
            return self._accept_v8_ack(calls, expected)
        if len(progress) != 1 or progress[0].get('decision') != 'CONTINUE' or not operational:
            return self._accept_v8_ack(calls, expected)
        try:
            normalized = validate_model_progress(progress[0], self.progress_ledger)
        except TaskProgressError:
            return self._accept_v8_ack(calls, expected)
        # Exclusive-action restrictions must run before a generic Progress.next_action
        # mismatch, or the model receives misleading advice to repair an
        # unauthorized filler tool instead of removing it.
        from agent_core.strict_observation_guard import strict_observation_action_diagnostic
        strict_diagnostic = strict_observation_action_diagnostic(
            getattr(self, "task_request", ""), operational,
        )
        if strict_diagnostic:
            return [], [strict_diagnostic]
        consistency = self._progress_action_consistency_diagnostic(normalized, operational)
        if consistency:
            return [], [consistency]
        dependency_errors = {}; earlier = set()
        for action in operational:
            issue = resolution_errors.get(action['action_id']) or dependency_diagnostic(action, earlier, self.action_result_ledger)
            cached = self.action_ledger.get(action['action_id'])
            if cached:
                try:
                    same = cached.get('signature') == action_signature(action)
                except ProtocolV8Error:
                    same = False
                if not same:
                    issue = {'reason':'action_id_payload_mismatch', 'detail':action['action_id']}
            replaced = action.get('replaces_action_id')
            held_replacement = (self.deferred_action_repairs.get(action['action_id']) or {}).get('action',{}).get('replaces_action_id')
            if replaced and (replaced not in self.deferred_action_repairs and held_replacement != replaced or replaced in self.action_ledger):
                issue = {'reason':'action_replacement_not_deferred', 'detail':str(replaced)}
            if issue: dependency_errors[action['action_id']] = issue
            earlier.add(action['action_id'])
        # Fast path preserves the full existing admission, including all native
        # tool prerequisites and round budget. Dependencies are checked at use.
        original_budget = validate_round_write_budget(body)
        if original_budget:
            return [], [original_budget]
        envelopes_valid = all(validate_tool_envelope(item)[0] for item in operational)
        if not dependency_errors and envelopes_valid:
            admitted, diagnostics = self._probe_action_admission(calls, expected)
            if admitted and not diagnostics:
                result = self._accept_v8_ack(calls, expected)
                for action in operational:
                    self.deferred_action_repairs.pop(action['action_id'], None)
                    self.deferred_action_repairs.pop(action.get('replaces_action_id'), None)
                return result
        accepted = []; repairs = []
        global_reasons = {'invalid_progress_payload', 'runtime_evidence_ref_unknown',
            'progress_count_invalid', 'round_write_budget_exceeded', 'final_response_not_exclusive',
            'continue_decision_forbids_final_response', 'invalid_progress_envelope',
            'pending_actions_not_preserved'}
        for action in operational:
            issue = dependency_errors.get(action['action_id'])
            if issue:
                diagnostics = [issue]
            else:
                # Probe only after envelope validity, so malformed query/plan
                # types cannot crash other independent actions.
                valid, issue = validate_tool_envelope(action)
                if not valid:
                    diagnostics = [issue]
                else:
                    _, diagnostics = self._probe_action_admission([
                        deepcopy(progress[0]), deepcopy(action), {'tool':'turn_commit','action_count':2}
                    ], expected, single=True)
            if any(item.get('reason') in global_reasons for item in diagnostics):
                return self._accept_v8_ack(calls, expected)
            if diagnostics:
                repairs.append({'action_id':action['action_id'], 'tool':action.get('tool'),
                    'status':'REPAIR_REQUIRED', 'executed':False, 'action':deepcopy(action), 'diagnostics':diagnostics})
            else:
                accepted.append(action)
        if not accepted:
            if dependency_errors:
                return [], [{'marker':'[WEBAGENT_ACTION_DEPENDENCY_REJECTED]', **issue,
                             'action_id':ref} for ref, issue in dependency_errors.items()]
            return self._accept_v8_ack(calls, expected)
        # Revalidate all accepted actions together (including the original round
        # budget and Progress). Projection is never executable authority itself.
        selected = [deepcopy(progress[0]), *deepcopy(accepted)]
        self._single_action_probe = True
        try:
            admitted, diagnostics = self._accept_v8_ack([
                *selected, {'tool':'turn_commit','action_count':len(selected)}], expected)
        finally:
            self._single_action_probe = False
        if diagnostics: return admitted, diagnostics
        for action in accepted:
            self.deferred_action_repairs.pop(action['action_id'], None)
            self.deferred_action_repairs.pop(action.get('replaces_action_id'), None)
        self.current_action_repairs = repairs
        for repair in repairs:
            if repair['action_id'] not in self.action_ledger:
                self.deferred_action_repairs[repair['action_id']] = repair
            else:
                repair['status'] = 'REJECTED_REPLAY'
        self._emit('action_batch_partitioned', request_id=self.run_id, round=self.turn_id,
            original_action_count=len(body),
            admitted_action_ids=[a['action_id'] for a in accepted], deferred_actions=repairs)
        return admitted, []

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
        from agent_core.smartagent_protocol import normalize_tool_numeric_fields
        normalized = [normalize_tool_numeric_fields(item) for item in calls]
        numeric_changes = [index for index, (old,new) in enumerate(zip(calls,normalized),1) if old != new]
        if self.request_context:
            from agent_core.request_context import RequestContextError
            for index,item in enumerate(normalized):
                if 'field_refs' in item and item.get('tool') not in {'report_progress','final_response','turn_commit'}:
                    try:
                        expanded=self.request_context.expand(item)
                        self._emit('action_context_refs_expanded',request_id=self.run_id,round=self.turn_id,
                                   action_id=item.get('action_id',''),field_refs=item['field_refs'])
                        normalized[index]=expanded
                    except RequestContextError:
                        pass  # Normal per-action admission returns the precise diagnostic.
        if numeric_changes:
            self._emit('action_numeric_fields_normalized', request_id=self.run_id, round=self.turn_id, block_indexes=numeric_changes)
        calls = normalized
        pending = [dict(action) for action in self.pending_progress_repair_actions]
        if not pending or not calls or calls[-1].get("tool") != "turn_commit":
            return calls
        submitted = [
            action for action in calls[:-1]
            if action.get("tool") != "report_progress"
        ]
        pending_by_id = {str(item.get('action_id', '')): item for item in pending}
        if any(str(item.get('action_id', '')) not in pending_by_id
               or semantic_action_signature(item) != semantic_action_signature(pending_by_id[str(item.get('action_id', ''))])
               for item in submitted):
            return calls
        # A partial repair reply may echo only one of several held actions.
        # Restore the entire original order without executing any action here.
        restored = [item for item in calls[:-1] if item.get('tool') == 'report_progress'] + pending
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

    def _preserve_action_repair_batch(
        self,
        actions: list[dict],
        invalid_action_indexes: set[int],
    ) -> dict:
        """Hold valid operational actions while invalid siblings are repaired.

        Nothing stored here is admitted or executable authority.  The complete
        reconstructed batch still passes through every normal progress,
        state-machine, envelope, and v8 admission check before execution.
        """
        slots: list[dict | None] = []
        invalid_slots: list[int] = []
        preserved_ids: list[str] = []
        for action_index, action in enumerate(actions, 1):
            tool_name = str(action.get("tool", "") or "")
            if tool_name == "report_progress":
                continue
            # Terminal replies are deliberately never mixed into action-batch
            # repair.  Their exclusivity and evidence gates must be evaluated
            # from one fresh, complete response.
            if tool_name == "final_response":
                self.pending_action_repair = {}
                return {}
            slot_index = len(slots) + 1
            if action_index in invalid_action_indexes:
                slots.append(None)
                invalid_slots.append(slot_index)
                continue
            slots.append(dict(action))
            preserved_ids.append(str(action.get("action_id", "") or ""))

        if not preserved_ids or not invalid_slots:
            self.pending_action_repair = {}
            return {}
        state = {
            "source_round": self.turn_id,
            "slots": slots,
            "invalid_slots": invalid_slots,
            "preserved_action_ids": preserved_ids,
        }
        self.pending_action_repair = state
        self._emit(
            "action_repair_batch_preserved",
            request_id=self.run_id,
            round=self.turn_id,
            preserved_action_ids=preserved_ids,
            repair_action_indexes=invalid_slots,
        )
        return state

    def _restore_action_repair_batch(
        self, calls: list[dict],
    ) -> tuple[list[dict], dict | None]:
        pending = dict(self.pending_action_repair or {})
        slots = list(pending.get("slots") or [])
        invalid_slots = [int(item) for item in list(pending.get("invalid_slots") or [])]
        if not slots or not invalid_slots:
            return calls, None
        if not calls or calls[-1].get("tool") != "turn_commit":
            return calls, None

        progress_actions = [
            dict(action) for action in calls[:-1]
            if action.get("tool") == "report_progress"
        ]
        submitted = [
            dict(action) for action in calls[:-1]
            if action.get("tool") != "report_progress"
        ]
        preserved_ids = set(pending.get("preserved_action_ids") or [])
        submitted_ids = {
            str(action.get("action_id", "") or "") for action in submitted
        }
        # A full retransmission remains safe and compatible.  Distinguish it by
        # the presence of at least one preserved action_id; this avoids
        # confusing a multi-action replacement sequence with a full batch.
        if len(submitted) == len(slots) and preserved_ids.intersection(submitted_ids):
            self._emit(
                "action_repair_full_batch_resubmitted",
                request_id=self.run_id,
                round=self.turn_id,
                action_count=len(submitted),
            )
            return calls, None
        replacement_count_valid = (
            bool(submitted)
            if len(invalid_slots) == 1
            else len(submitted) == len(invalid_slots)
        )
        if len(progress_actions) != 1 or not replacement_count_valid:
            return calls, {
                "marker": "[WEBAGENT_ACTION_REPAIR_REJECTED]",
                "reason": "action_repair_count_mismatch",
                "detail": (
                    f"expected_progress=1;actual_progress={len(progress_actions)};"
                    f"expected_repaired_actions="
                    f"{'one_or_more' if len(invalid_slots) == 1 else len(invalid_slots)};"
                    f"actual_repaired_actions={len(submitted)}"
                ),
                "suggestion": (
                    "只輸出一個 report_progress，並依 repair_action_indexes 順序輸出完整修正版 action。"
                    "若只有一個失敗位置，可用多個 action 組成 canonical replacement sequence；"
                    "不要重送 preserved action。"
                ),
                "repair_scope": "ACTION_BATCH",
                "preserved_action_ids": list(pending.get("preserved_action_ids") or []),
                "repair_action_indexes": invalid_slots,
            }

        repaired_iter = iter(submitted)
        merged: list[dict] = []
        for slot in slots:
            if slot is not None:
                merged.append(dict(slot))
                continue
            if len(invalid_slots) == 1:
                merged.extend(dict(action) for action in repaired_iter)
            else:
                merged.append(dict(next(repaired_iter)))
        restored = [progress_actions[0], *merged]
        commit = dict(calls[-1])
        commit["action_count"] = len(restored)
        restored.append(commit)
        self._emit(
            "action_repair_batch_restored",
            request_id=self.run_id,
            round=self.turn_id,
            preserved_action_ids=list(pending.get("preserved_action_ids") or []),
            repair_action_indexes=invalid_slots,
        )
        return restored, None

    def _accept_v8_ack(self, calls: list[dict], expected: dict) -> tuple[list[dict], list[dict]]:
        from agent_core.smartagent_protocol import normalize_tool_numeric_fields
        original_calls = calls
        calls = [normalize_tool_numeric_fields(item) for item in calls]
        changes = [index for index, (old, new) in enumerate(zip(original_calls, calls), 1) if old != new]
        if changes:
            self._emit('action_numeric_fields_normalized', request_id=self.run_id, round=self.turn_id, block_indexes=changes)
        self.active_stage_manifest = None
        self.read_only_progress_deferred = False
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
        held = {str(item.get('action_id', '')): item for item in self.pending_progress_repair_actions
                if str(item.get('action_id', '')) not in self.action_ledger}
        submitted = [item for item in actions if item.get('tool') != 'report_progress']
        if (held and str(progress_actions[0].get('decision', '')).upper() != 'INTERRUPT'
                and not (self._failed_predeclared_fact(progress_actions[0])
                         and self._read_only_unexecuted_repairs(held.values()))):
            by_id = {str(item.get('action_id', '')): item for item in submitted}
            if set(by_id) != set(held) or any(semantic_action_signature(by_id[key]) != semantic_action_signature(value) for key, value in held.items()):
                return [], [{'marker':'[WEBAGENT_ACTION_REPAIR_REJECTED]',
                    'reason':'pending_actions_not_preserved', 'detail':json.dumps({'pending_action_ids':list(held)}, separators=(',', ':')),
                    'suggestion':'只補 Progress；Runtime 保留原本尚未執行的 action。不要換 action、提前結案或重做已執行操作。'}]
        budget_diagnostic = validate_round_write_budget(actions)
        if budget_diagnostic:
            self._emit("write_budget_rejected", request_id=self.run_id, round=self.turn_id,
                       diagnostic=budget_diagnostic)
            return [], [budget_diagnostic]
        normalized_progress_action, wrapper_diagnostic = self._normalize_report_progress_envelope(
            progress_actions[0], self.progress_ledger
        )
        if wrapper_diagnostic:
            return [], [wrapper_diagnostic]
        from agent_core.progress_reply_contract import materialize_progress
        try:
            normalized_progress_action = materialize_progress(normalized_progress_action, self.progress_ledger)
            inherited = [field for field in ('base_evaluation','total_steps','current_step','steps','completion_contract')
                         if field not in progress_actions[0] and field in normalized_progress_action]
            normalized_fields = [field for field in normalized_progress_action if field in progress_actions[0]
                                 and (type(normalized_progress_action[field]) is not type(progress_actions[0][field])
                                      or normalized_progress_action[field] != progress_actions[0][field])]
            if inherited or normalized_fields:
                self._emit('progress_fields_materialized', request_id=self.run_id, round=self.turn_id,
                           inherited_fields=inherited, normalized_fields=normalized_fields)
        except ValueError as exc:
            from agent_core.progress_recovery import repair_diagnostic, read_only_progress_fallback
            diagnostic = self._progress_repair_diagnostic(normalized_progress_action, exc)
            reads = [a for a in actions if a.get('tool') != 'report_progress']
            fallback = read_only_progress_fallback(normalized_progress_action, self.progress_ledger,
                                                  reads, exc, self._runtime_evidence_refs())
            if fallback is not None:
                if any(not self.tools.preview_tool_scope(a).get('allowed', True) for a in reads):
                    return [], [diagnostic]
                revised = [fallback if a.get('tool') == 'report_progress' else a for a in actions]
                admitted, errors = self._accept_v8_ack([*revised, calls[-1]], expected)
                if admitted and not errors:
                    self.read_only_progress_deferred = True
                    self.deferred_progress_repair = diagnostic
                    self._emit('progress_repair_deferred_read_admitted', request_id=self.run_id,
                               round=self.turn_id, diagnostic=diagnostic,
                               action_ids=[a.get('action_id') for a in reads])
                return admitted, errors
            return [], [diagnostic]
        progress_index = actions.index(progress_actions[0])
        actions[progress_index] = normalized_progress_action
        progress_actions = [normalized_progress_action]
        operational = [action for action in actions if action.get("tool") != "report_progress"]
        from agent_core.strict_observation_guard import strict_observation_action_diagnostic
        strict_diagnostic = strict_observation_action_diagnostic(
            getattr(self, "task_request", ""), operational,
        )
        if strict_diagnostic:
            return [], [strict_diagnostic]
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
                    rows = []
                    for query in list(action.get('queries') or []):
                        if not isinstance(query, dict):
                            return ()
                        start, end = query.get('start_line',1), query.get('end_line',240)
                        if type(start) is not int or type(end) is not int or start < 1 or end < start:
                            return ()
                        rows.append((str(query.get('operation','')).lower(), str(query.get('path','')).replace('\\','/'), start, min(end,start+239)))
                    return tuple(rows)
                def covers(actual: dict, expected: dict) -> bool:
                    actual_root = str(Path(actual.get('project_root') or actual.get('workspace') or self.workspace).resolve()).casefold()
                    expected_root = str(Path(expected.get('project_root') or expected.get('workspace') or self.workspace).resolve()).casefold()
                    wanted, provided = query_signature(expected), query_signature(actual)
                    return bool(wanted and provided and actual_root == expected_root and all(
                        any(row[:2] == need[:2] and row[2] <= need[2] and row[3] >= need[3] for row in provided)
                        for need in wanted))
                if not all(any(covers(actual, wanted) for actual in submitted_queries) for wanted in expected_queries):
                    return [], [{
                        "marker": "[WEBAGENT_EDIT_PLAN_PREREQUISITE_REQUIRED]",
                        "reason": "edit_plan_prerequisite_action_mismatch",
                        "detail": "query_project must cover every Runtime-issued read_range path/range in the same project root",
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
        from agent_core.action_dependencies import is_observation
        if plan_repair_active and not (operational and all(is_observation(item) for item in operational)):
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
                cwd_scope = self.tools.preview_tool_scope(action)
                if not cwd_scope.get('allowed',True):
                    from agent_core.tools import _path_independent_command_must_use_bound_workspace
                    command = str(action.get('command', '') or '')
                    scope_error = str(cwd_scope.get('error', '') or '')
                    eligible = (
                        scope_error.startswith('path_independent_command_cwd_outside_bound_workspace:')
                        and _path_independent_command_must_use_bound_workspace(command)
                    )
                    alternative = self._bound_cwd_command_contract(command) if eligible else {}
                    if eligible and alternative.get('bound_workspace_admitted'):
                        self._pending_bound_cwd_repair = {
                            'action_id': str(action.get('action_id', '') or ''),
                            'command': command,
                            'attempted_cwd': str(action.get('working_directory', '') or ''),
                            'bound_workspace': str(self.workspace),
                            'reason': scope_error,
                            'request_id': self.run_id,
                        }
                        suggestion = (
                            'Runtime confirms this path-independent command is admitted at the '
                            'already-bound working_directory=' + str(self.workspace)
                            + '. Retry with the SAME command and explicitly this bound cwd '
                            'in a new canonical action; this is not access to or rebinding '
                            'of the historical file fixture. Do not execute at the rejected '
                            'directory. Original file-target permissions remain unchanged. '
                            'Pre-execution rejection is NOT_STARTED, not completed evidence.'
                        )
                    else:
                        suggestion = '提供本 request 授權範圍內的絕對目錄 working_directory。'
                    return [], [{'reason':'run_command_working_directory_rejected',
                        'detail':scope_error, 'suggestion':suggestion}]
                if (self._pending_bound_cwd_repair.get('request_id') == self.run_id
                        and self._pending_bound_cwd_repair.get('command') == str(action.get('command', '') or '')
                        and cwd_scope.get('resolved_paths') == [str(self.workspace)]):
                    self._pending_bound_cwd_repair = {}
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
            from agent_core.progress_recovery import repair_diagnostic, read_only_progress_fallback
            diagnostic = self._progress_repair_diagnostic(progress_actions[0], exc)
            fallback = read_only_progress_fallback(progress_actions[0], self.progress_ledger,
                                                  operational, exc, self._runtime_evidence_refs())
            if fallback is not None:
                if any(not self.tools.preview_tool_scope(a).get('allowed', True) for a in operational):
                    return [], [diagnostic]
                # Re-enter the entire normal admission path: scope, prerequisites,
                # envelope, tool params, commit and terminal guards are not skipped.
                repaired_calls = [fallback if item is progress_actions[0] else item for item in actions]
                admitted, errors = self._accept_v8_ack([*repaired_calls, calls[-1]], expected)
                if admitted and not errors:
                    self.read_only_progress_deferred = True
                    self.deferred_progress_repair = diagnostic
                    self._emit('progress_repair_deferred_read_admitted', request_id=self.run_id,
                               round=self.turn_id, diagnostic=diagnostic,
                               action_ids=[a.get('action_id') for a in operational])
                return admitted, errors
            preserved_action_ids = self._preserve_progress_repair_actions(
                operational, expected,
            )
            repair_context = dict(getattr(exc, "repair_context", {}) or {})
            if repair_context:
                diagnostic.update(repair_context)
            if preserved_action_ids:
                diagnostic["preserved_action_ids"] = preserved_action_ids
            return [], [diagnostic]
        decision = normalized_progress["decision"]
        outcome = normalized_progress["outcome"]
        if (decision == 'COMPLETE' and getattr(self,'deferred_action_repairs',{})
                and not (self._failed_predeclared_fact(normalized_progress)
                    and self._read_only_unexecuted_repairs(self.deferred_action_repairs.values()))):
            return [], [{'marker':'[WEBAGENT_ACTION_REPAIR_REQUIRED]', 'reason':'unresolved_action_repairs',
                'detail':json.dumps(list(self.deferred_action_repairs.values()),ensure_ascii=False),
                'suggestion':'終態正規化不會解除未執行操作；先補齊 deferred action，不能用 FAILED/PARTIAL 繞過。'}]
        if decision == "INTERRUPT":
            from agent_core.context_acquisition import context_interrupt_allowed
            capabilities = self._context_capabilities()
            request = normalized_progress.get('context_request') or {}
            spec = self._safe_protocol_spec_rejection()
            if (spec and request.get('kind') == 'BLOCKED'
                    and request.get('blocking_action_id') == spec['action_id']):
                if 'PROTOCOL_SPEC_REJECTION' not in normalized_progress.get('evidence_refs', []):
                    return [], [{
                        'marker': '[WEBAGENT_CONTEXT_ACQUISITION_REQUIRED]',
                        'reason': 'spec_invalid_protocol_evidence_ref_required',
                        'suggestion': '引用本 Request 的 PROTOCOL_SPEC_REJECTION；'
                                      '它只支持 INTERRUPT/UNKNOWN，不能支持 SUCCESS。',
                    }]
            objective_unknown = next((item for item in capabilities.get('objective_blockers', [])
                if item.get('reason') == 'FACT_OBSERVATION_INDETERMINATE'
                and item.get('action_id') == request.get('blocking_action_id')), None)
            if request.get('kind') == 'BLOCKED' and objective_unknown:
                # This exception is solely for the exact, completed read.
                # Never use UNKNOWN fact to conceal unresolved side effects.
                refs = set(normalized_progress.get('evidence_refs') or [])
                from agent_core.action_loop_state import MUTATION_TOOLS, DELIVERY_TOOLS
                from agent_core.tool_result_contract import decode_result
                unsafe = []
                for ref, entry in self.action_result_ledger.items():
                    if entry.get('evidence_status', 'ACTIVE') != 'ACTIVE':
                        continue
                    if entry.get('tool') not in MUTATION_TOOLS | DELIVERY_TOOLS:
                        continue
                    typed = decode_result(str((self.action_ledger.get(ref) or {}).get('result', ''))) or {}
                    if (str(entry.get('execution_status','UNKNOWN')).upper() not in {'SUCCEEDED','REJECTED'}
                            or entry.get('terminal_eligible') is False
                            or (typed.get('data') or {}).get('transaction_state') == 'OPEN'):
                        unsafe.append(ref)
                if (request.get('blocking_action_id') not in refs or unsafe
                        or not self._read_only_unexecuted_repairs(
                            getattr(self, 'deferred_action_repairs', {}).values())
                        or not self._read_only_unexecuted_repairs(
                            getattr(self, 'pending_progress_repair_actions', []))):
                    return [], [{
                        'marker':'[WEBAGENT_CONTEXT_ACQUISITION_REQUIRED]',
                        'reason':'objective_unknown_interrupt_not_safe_or_unbound',
                        'detail':json.dumps({'blocking_action_id':request.get('blocking_action_id'),
                            'evidence_refs':sorted(refs), 'unresolved_side_effects':unsafe},
                            ensure_ascii=False, separators=(',', ':')),
                        'suggestion':'必須引用同一 request 的 UNKNOWN observe_fact，且不得有未決副作用。',
                    }]
            repair = dict(getattr(self, '_pending_bound_cwd_repair', {}) or {})
            if (repair.get('request_id') == self.run_id
                    and request.get('kind') == 'BLOCKED'
                    and request.get('blocking_action_id') == repair.get('action_id')
                    and self._bound_cwd_command_contract(str(repair.get('command', ''))).get('bound_workspace_admitted')):
                # Do not turn an admission-only rejection into a fictitious ToolResult,
                # or force unrelated fixture reads that cannot alter command scope.
                return [], [{
                    'marker': '[WEBAGENT_BOUND_COMMAND_WORKSPACE_RECOVERY]',
                    'reason': 'path_independent_command_bound_workspace_already_available',
                    'detail': json.dumps(repair, ensure_ascii=False, separators=(',', ':')),
                    'suggestion': (
                        'A no-file-access command may use the existing bound workspace '
                        + str(self.workspace)
                        + ' without changing the C fixture data-scope permission. '
                        'Use CONTINUE and a fresh canonical run_command at exactly '
                        'this bound workspace, or request explicit USER_INPUT if the '
                        'original requirement genuinely requires C as the execution cwd. '
                        'Do not repeat directory reads as they cannot authorize cwd, '
                        'do not claim a rejected command was executed.'
                    ),
                }]
            if not context_interrupt_allowed(request, capabilities):
                self._emit('context_acquisition_required', request_id=self.run_id,
                           state='NOT_REQUESTED', context_request=request)
                return [], [{
                    'marker': '[WEBAGENT_CONTEXT_ACQUISITION_REQUIRED]',
                    'reason': 'missing_context_requires_runtime_read',
                    'detail': json.dumps(capabilities, ensure_ascii=False, separators=(',', ':')),
                    'suggestion': (
                        '尚未取得資料不代表無法取得資料。使用 CONTINUE，依原始需求與授權範圍選擇'
                        '一個精確讀取 action；先確認實際目錄／檔名，不猜檔名、不省略 path/root。'
                        '專案內容使用 query_project，禁止要求使用者上傳 Runtime 可讀取的 source。'
                        '若真正需要使用者選擇，提供 context_request={kind:USER_INPUT,detail:具體問題}；'
                        '若已有客觀拒絕，使用 BLOCKED 並引用 Runtime 列出的 blocking_action_id。'
                    ),
                }]
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
            if (action.get("expected_failure") is None and action.get('expected_verification') is None
                and action.get("expected_outcome") is None
                and action.get("predicate") is None):
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
                        "expected_failure／expected_verification／expected_outcome／predicate 必須在執行前綁定 completion_contract.success 中的精確條件文字，"
                        "或其 Runtime condition_id；不得用事後新增或不相干條件包裝一般失敗。"
                    ),
                }]
        from agent_core.strict_observation_guard import strict_observation_only_request
        strict_first_registration = (
            strict_observation_only_request(getattr(self, "task_request", ""))
            # Earlier REJECTED attempts are not successful condition
            # registration.  Only the first actually admitted ACK may register
            # these two success clauses without an operational Tool Action.
            and not getattr(self, "accepted_ack_ids", [])
            and not operational
            and not self.action_ledger
            and not self.action_result_ledger
            and not self.condition_result_ledger
            and not (getattr(self.progress_ledger, "completion_contract", {}) or {}).get("success")
            and normalized_progress.get("decision") == "CONTINUE"
            and normalized_progress.get("outcome") == "PENDING"
            and float(normalized_progress["current_step"]) < float(normalized_progress["total_steps"])
            and len((normalized_progress.get("completion_contract") or {}).get("success") or []) == 2
        )
        # One initial Progress-only condition-registration exchange, not a
        # reusable no-action loophole. Normal ACK, ledger and stagnation guards
        # still apply, and no Runtime Tool Action is dispatched on this turn.
        action_consistency = None if (
            getattr(self, '_single_action_probe', False) or strict_first_registration
        ) else self._progress_action_consistency_diagnostic(normalized_progress, operational)
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
            if self.continuation_source and action.get('action_id') not in self.action_ledger:
                from agent_core.task_continuation import guard_continuation_action, ContinuationError
                try:
                    guard_continuation_action(action, self._continuation_guard_source(), self.continuation_mode)
                except ContinuationError as exc:
                    return [], [{'marker':'[TASK_CONTINUATION_REJECTED]', 'reason':str(exc),
                                 'suggestion':'使用新的唯讀 action 核對；不要重做舊操作。'}]
            if self.continuation_source and decision == 'COMPLETE' and outcome == 'SUCCESS':
                from agent_core.task_continuation import unresolved_continuation_result
                unresolved = [key for key, value in self._continuation_guard_source()['results'].items()
                    if unresolved_continuation_result(value)]
                if unresolved:
                    return [], [{'marker':'[TASK_CONTINUATION_REJECTED]',
                        'reason':'continuation_execution_reconciliation_required', 'detail':','.join(unresolved),
                        'suggestion':'使用新 VERIFY 並以 verifies_action_id 關聯不確定的原操作，不能以無關讀取結案。'}]
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
        live_blockers = [item for item in self.action_loop_state.get('blockers',[]) if not item.get('satisfied')]
        if decision == 'COMPLETE' and outcome == 'SUCCESS' and live_blockers:
            return [], [{'marker':'[WEBAGENT_VERIFICATION_REQUIRED]', 'reason':'active_action_not_complete',
                'detail':json.dumps({'blockers':live_blockers},ensure_ascii=False,separators=(',',':')),
                'suggestion':'不能用後來的讀取 PASS 隱藏仍 OPEN／RUNNING／QUEUED 的操作。保留進度，完成 Runtime 提供的 commit/abort/狀態確認；不要重送原 action。'}]
        if decision == 'COMPLETE' and outcome == 'PARTIAL':
            facts = [entry for _,entry in self._resolve_effective_evidence_entries(evidence_refs)]
            objective_partial = any(item.get('execution_status') in {'FAILED','REJECTED','UNKNOWN','PARTIAL','QUEUED'} for item in facts)
            recoverable = any(item.get('reason') in {'OPEN_WRITE_TRANSACTION','VERIFY_REQUIRED','PREREQUISITE_REQUIRED'}
                              or item.get('release_kind') in {'FILE_COMMITTED','RUNTIME_PREREQUISITE_SATISFIED','SCOPED_VERIFICATION_PASS_OR_FAIL'}
                              or item.get('execution_status') in {'RUNNING','UNKNOWN','PARTIAL'}
                              for item in live_blockers)
            if not objective_partial or recoverable:
                return [], [{'marker':'[WEBAGENT_PROGRESS_REJECTED]', 'reason':'partial_not_supported_or_recoverable_work',
                    'detail':json.dumps({'objective_partial':objective_partial,'blockers':live_blockers},ensure_ascii=False,separators=(',',':')),
                    'suggestion':'PARTIAL 需引用客觀失敗／拒絕／未知結果，不能拿來跳過仍可 commit 的交易或尚未驗證的修改。先完成 prerequisite／verification；有客觀阻塞才中斷。'}]
        if terminal_evidence_verdict in {"PASS", "FAIL"}:
            self.pending_verification_requirement = {}
        if (
            self.pending_verification_requirement
            # UNKNOWN is not a completed verification: an outstanding
            # structured verification requirement also blocks progress-only
            # replies with no verifying Tool Action.
            and verification_status not in {"PASS", "FAIL"}
            and decision == "CONTINUE"
            and not (operational and all(is_observation(item) for item in operational))
            and not (self._has_structured_verification_action(operational)
                     if (self.pending_verification_requirement or {}).get('required_action') in {'run_command','aggregate_verification'}
                     else any(item.get('tool') == (self.pending_verification_requirement or {}).get('required_action')
                              and (not (self.pending_verification_requirement or {}).get('write_id')
                                   or item.get('write_id') == self.pending_verification_requirement['write_id'])
                              for item in operational))
        ):
            return [], [self._verification_requirement_diagnostic(
                normalized_progress,
                reason="verification_evidence_action_required",
            )]
        # Phase 5A: a separate, explicitly enabled answer-only admission lane.
        # No ActionResult or fabricated verification PASS is produced. Every
        # canonical V9/Progress/scope/identity gate before this point still runs.
        answer_only_admission = False
        if decision == "COMPLETE" and outcome == "SUCCESS" and terminal_evidence_verdict == "UNVERIFIED":
            from agent_core.tri_one_answer_only import enabled as answer_only_enabled, accept_terminal_candidate
            from agent_core.tri_one_task_result import enabled as task_result_enabled
            if task_result_enabled() and answer_only_enabled(source=getattr(self, "_tri_one_origin", "")):
                final_action = next((a for a in operational if a.get("tool") == "final_response"), {})
                answer_only_admission, answer_reason = accept_terminal_candidate(
                    self, normalized_progress, final_action,
                )
                self._emit("tri_one_answer_only_candidate",
                           request_id=self.run_id, round=self.turn_id,
                           eligible=answer_only_admission, reason=answer_reason)
        if decision == "COMPLETE" and outcome == "SUCCESS" and terminal_evidence_verdict == "UNVERIFIED" and not answer_only_admission:
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
            self.pending_verification_requirement = self._terminal_recovery_requirement(normalized_progress, terminal_evidence_verdict)
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
        if decision == 'COMPLETE' and outcome == 'FAILED':
            # A wrong factual expectation is only an admissible FAILED reason
            # if it was bound to a declared success condition before the probe.
            cited = [(ref, self.action_result_ledger.get(ref) or {})
                     for ref in evidence_refs]
            if any(entry.get('tool') == 'observe_fact' for _, entry in cited):
                supported = any(
                    entry.get('tool') == 'observe_fact'
                    and entry.get('evidence_status','ACTIVE') == 'ACTIVE'
                    and str(entry.get('expectation_status','')).upper() == 'FAIL'
                    and str(entry.get('condition_id','')) in allowed_expected_failure_conditions
                    for _, entry in cited)
                if not supported:
                    return [], [{'marker':'[WEBAGENT_PROGRESS_REJECTED]',
                        'reason':'failed_fact_unbound_to_declared_success_condition',
                        'detail':'Referenced failed fact does not match a predeclared success condition.',
                        'suggestion':'引用原始事前綁定的 observe_fact FAIL，不得用不相關工具失敗代替。'}]
        context = V8RequestContext(
            self.run_id,
            getattr(self, "task_id", "") or self.run_id,
            getattr(self, "task_epoch", "") or self.run_id,
            getattr(self, "intent_digest", ""),
        )
        admitted: dict[str, dict] = {}
        diagnostics: list[dict] = []
        invalid_action_indexes: set[int] = set()
        for index, action in enumerate(actions, 1):
            tool_name = str(action.get("tool", "") or "")
            if tool_name not in {"final_response", "report_progress"} and tool_name not in SUPPORTED_ACTION_TOOLS:
                invalid_action_indexes.add(index)
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
                invalid_action_indexes.add(index)
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
                invalid_action_indexes.add(index)
                diagnostics.append({
                    "marker": "[WEBAGENT_V8_REJECTED]",
                    "reason": exc.code,
                    "detail": f"action_index={index};action_id={action.get('action_id', '')};{exc.detail}",
                    "suggestion": "只補缺少的決策欄位，不要修改已提供欄位。",
                })
                continue
            action_id = record["action_id"]
            if action_id in admitted:
                invalid_action_indexes.add(index)
                diagnostics.append({
                    "marker": "[WEBAGENT_V8_REJECTED]",
                    "reason": "duplicate_action_id",
                    "detail": f"action_id={action_id}",
                    "suggestion": "同一回合每個 action_id 只能出現一次。",
                })
            else:
                admitted[action_id] = record
        if diagnostics:
            repair_state = self._preserve_action_repair_batch(
                actions, invalid_action_indexes,
            )
            if repair_state:
                for diagnostic in diagnostics:
                    diagnostic["repair_scope"] = "ACTION_BATCH"
                    diagnostic["preserved_action_ids"] = list(
                        repair_state.get("preserved_action_ids") or []
                    )
                    diagnostic["repair_action_indexes"] = list(
                        repair_state.get("invalid_slots") or []
                    )
                    diagnostic["repair_expected_count"] = len(
                        repair_state.get("invalid_slots") or []
                    )
            return [], diagnostics
        if getattr(self, '_action_admission_probe', False):
            return actions, []
        self._answer_only_admitted = answer_only_admission
        self.v8_admitted_actions = admitted
        self.tools._v8_admitted_actions = admitted
        self.protocol_state = "ACCEPTED"
        self.pending_progress_repair_actions = []
        self.pending_action_repair = {}
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
        if action.get('tool') == 'delete_path' and self.resolved_delete_target and str(action.get('path', '')).casefold() != self.resolved_delete_target.casefold():
            from agent_core.tool_result_contract import encode_result
            return encode_result('REJECTED', 'TARGET_MISMATCH', requested_path=action.get('path', ''), detail='Use only the exact target proven by Runtime discovery')
        action_id = str(action.get("action_id", "") or "")
        signature = action_signature(action)
        cached = self.action_ledger.get(action_id)
        if cached:
            if cached["signature"] != signature:
                raise RuntimeError(f"action_id payload mismatch: {action_id}")
            return str(cached["result"])
        if self.continuation_source:
            from agent_core.task_continuation import guard_continuation_action
            guard_continuation_action(action, self._continuation_guard_source(), self.continuation_mode)
        if self.continuation_store:
            self.continuation_store.prepare_action(self.task_id, action)
        try:
            raw_result = self.tools.execute(action)
        except Exception as exc:
            # The tool may have produced a side effect before raising. Preserve
            # UNKNOWN, never claim no execution or automatically retry mutation.
            from agent_core.tool_result_contract import encode_result
            raw_result = encode_result('UNKNOWN', 'TOOL_EXCEPTION_OUTCOME_UNKNOWN',
                tool=action.get('tool', ''), executed=True,
                detail=f'{type(exc).__name__}: {exc}')
        if action.get('tool') == 'delete_path' and self.resolved_delete_target:
            from agent_core.tool_result_contract import decode_result
            if (decode_result(raw_result) or {}).get('status') == 'SUCCEEDED':
                self.resolved_delete_target = ''
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
        from agent_core.tool_result_contract import delete_target_recovery, resolved_delete_target
        delete_recovery = delete_target_recovery(action, raw_result, self.authorized_paths or [self.workspace])
        if delete_recovery:
            recovery_context = delete_recovery
        elif self.pending_result_recovery.get('reason') == 'delete_target_requires_resolution':
            proof = self._classify_action_evidence(action, str(raw_result))
            target = resolved_delete_target(self.pending_result_recovery, action, raw_result)
            if target and proof['execution_status'] == 'SUCCEEDED' and proof['evidence_state'] == 'AVAILABLE':
                self.resolved_delete_target = target
                self.pending_result_recovery = {}
            elif not recovery_context.get('active'):
                recovery_context = dict(self.pending_result_recovery)
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
        evidence = self._classify_action_evidence(action, str(raw_result))
        result = prepare_tool_result(action, raw_result, self.tools)
        from agent_core.progress_recovery import read_fact_digest
        self.action_ledger[action_id] = {
            "signature": signature,
            "result": result,
            'raw_result_digest': hashlib.sha256(str(raw_result).encode('utf-8')).hexdigest(),
            'read_fact_digest': read_fact_digest(action, raw_result),
            "action": dict(action),
            "recovery_context": recovery_context,
            **evidence,
        }
        self.execution_state = evidence["execution_status"]
        stagnation_guard = getattr(self, "_semantic_stagnation", None)
        if stagnation_guard is not None:
            stagnation = stagnation_guard.observe(action, evidence)
            if stagnation["blocker_reason"]:
                self._emit("semantic_stagnation_observed",
                    request_id=self.run_id, round=self.turn_id, **stagnation)
            if stagnation["status"] == "EXHAUSTED":
                # Never reclassify an already attempted tool as UNKNOWN.
                self._stagnation_pending = stagnation
        if self.continuation_store:
            self.continuation_store.snapshot(self)
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
        trusted_authorized_paths: list[str] | None = None,
    ) -> str:
        """Run one request and always release its browser ownership on failure."""
        self.continuation_source = None
        self.continuation_mode = ""
        self._protocol_spec_rejection = {}
        registered = False
        self._gar_fresh_binding = None
        try:
            if self.continuation_store:
                from agent_core.task_continuation import parse_continuation
                request_id = request_id or ('WA-' + uuid.uuid4().hex[:16].upper())
                task_id = task_id or ('TASK-' + uuid.uuid4().hex[:20].upper())
                task_epoch = task_epoch or uuid.uuid4().hex
                self.run_id, self.task_id, self.task_epoch = request_id, task_id, task_epoch
                self.progress_ledger = None
                self.continuation_store.register(task_id=task_id, request_id=request_id,
                    epoch=task_epoch, goal=request, workspace=self.workspace, owner=self.continuation_owner)
                registered = True
                self.continuation_source, self.continuation_mode, request = self.continuation_store.prepare_request(
                    task_id, request=request, owner=self.continuation_owner, workspace=self.workspace)
                if self.continuation_source:
                    # Only trusted original user scope; never extract from journal prose.
                    trusted_authorized_paths = resolve_authorized_paths(self.continuation_source.get('effective_goal') or self.continuation_source['goal'],
                        trusted_authorized_paths=self.continuation_source.get('authorized_paths'))
                    if self.continuation_source.get('_revised_goal'):
                        # Only explicit new human requirements can extend scope.
                        # Tool SecurityContext still checks current workspace ACLs.
                        revised_paths = resolve_authorized_paths(request)
                        if revised_paths:
                            trusted_authorized_paths = revised_paths
                    # Telegram may have created a display-only placeholder
                    # before the session bootstrap. It is not the source task.
                    from agent_core.task_progress import read_progress, delete_progress
                    placeholder = read_progress(task_id, self.progress_root)
                    if placeholder and placeholder.total_steps == 0:
                        delete_progress(task_id, self.progress_root)
                self.continuation_store.start(task_id, source=self.continuation_source, mode=self.continuation_mode)
            result = self._run_impl(
                request,
                request_id=request_id,
                initial_attachments=initial_attachments,
                source_tag=source_tag,
                task_id=task_id,
                task_epoch=task_epoch,
                skill_context=skill_context,
                trusted_authorized_paths=trusted_authorized_paths,
            )
            if self.continuation_store:
                self.continuation_store.snapshot(self)
                self.continuation_store.finish(task_id, 'COMPLETED', result, outcome=self.terminal_outcome)
            return result
        except BaseException as exc:
            if registered:
                # Bootstrap/restore can fail before progress/cache initialization.
                cleanup_errors = []
                try:
                    if getattr(self, 'task_id', '') == task_id and self.progress_ledger:
                        self.continuation_store.snapshot(self)
                except Exception as persistence_error:
                    cleanup_errors.append(('continuation_snapshot_cleanup_failed', str(persistence_error)))
                try:
                    self.continuation_store.finish(task_id, 'INTERRUPTED', f'{type(exc).__name__}: {exc}')
                except Exception as persistence_error:
                    cleanup_errors.append(('continuation_finish_cleanup_failed', str(persistence_error)))
                for event, detail in cleanup_errors:
                    try:
                        if self.event_sink:
                            self.event_sink(event, request_id=request_id, error=detail)
                    except Exception:
                        pass  # diagnostics cannot replace the original failure
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
        finally:
            binding = self._gar_fresh_binding
            self._gar_fresh_binding = None
            if binding is not None:
                try:
                    binding.close()
                except Exception as exc:
                    self._emit("gar_fresh_context_cleanup_failed",
                               request_id=str(request_id or self.run_id or ""),
                               error_type=type(exc).__name__)

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
        trusted_authorized_paths: list[str] | None = None,
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
        PIT.begin(request, request_id=self.run_id, task_id=self.task_id)
        PIT.record("07_loop_input", request)
        self.intent_digest = digest_intent(request)
        self.task_request = request
        source_name = str(source_tag or "").upper()
        self._tri_one_origin = (
            "TELEGRAM" if source_name == "REMOTEAGENT_TELEGRAM"
            else "LOCAL_TEST" if source_name == "REMOTEAGENT_TELEGRAM_SIMULATION"
            else "WEB_COPILOT" if source_name == "REMOTEAGENT_WEBCOPILOT_V9"
            else "LOCAL_TEST" if source_name == "TRI_ONE_TEST_V1"
            else "WEB_DIRECT"
        )
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
        self.terminal_acceptance = {}
        self._answer_only_admitted = False
        # An already queued attachment or skill is an external dependency,
        # even if the original prose itself contains no file path.
        self._tri_one_has_attached_context = bool(initial_attachments or skill_context)
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
        self.pending_action_repair = {}
        self.deferred_action_repairs = {}
        self.pending_command_field_repairs = {}
        self.current_action_repairs = []
        self.active_stage_manifest = None
        self.pending_result_recovery = {}
        self.resolved_delete_target = ''
        self.deferred_progress_repair = {}
        self.read_only_progress_deferred = False
        self.last_deferred_read_signature = None
        self.tools.current_task_id = self.task_id
        self.tools.last_verification_status = None
        self.pending_verification_requirement = {}
        from agent_core.blocker_recovery import BlockerRecovery
        from agent_core.bounded_repair import BoundedRepairBudget
        self._blocker_recovery = BlockerRecovery()
        # Each request owns a fresh repair and no-progress budget.
        self._bounded_repair = BoundedRepairBudget()
        from agent_core.bounded_repair import SemanticStagnationGuard
        self._semantic_stagnation = SemanticStagnationGuard()
        self._stagnation_pending = None
        from agent_core.goal_anchored_recovery import (
            enabled as gar_enabled, RecoveryEngine,
        )
        # A continuation may present a fresh imperative such as "continue".
        # GAR must anchor to the trusted effective original Goal, not that
        # UI shorthand, and may not infer a broader scope from old model prose.
        continuation_goal = (
            (self.continuation_source or {}).get("effective_goal")
            or (self.continuation_source or {}).get("goal")
        ) if isinstance(self.continuation_source, dict) else ""
        gar_goal = str(continuation_goal or self.task_request)
        self._gar = (
            RecoveryEngine(
                request_id=self.run_id, task_id=self.task_id,
                task_epoch=self.task_epoch, original_request=gar_goal,
                owner=self._tri_one_origin,
            )
            if gar_enabled() else None
        )
        self._request_started_monotonic = time.monotonic()
        self.tools.current_task_epoch = self.task_epoch
        self.tools.current_intent_digest = self.intent_digest
        authorized = resolve_authorized_paths(
            request,
            trusted_authorized_paths=trusted_authorized_paths,
        )
        self.authorized_paths = list(authorized)
        self.tools.begin_run(self.run_id, request, authorized)
        if self.continuation_source:
            self._restore_continuation()
        from agent_core.request_context import RequestContextStore, RequestContextError
        context_base=self.progress_root if self.progress_root is not None else self.workspace
        self.request_context=RequestContextStore(Path(context_base)/'localdata'/'runtime'/'request_contexts',
                                                self.run_id,self.task_epoch,request)
        for path in authorized[:32]:
            self.request_context.remember('path',str(path),'USER_REQUEST')
            if Path(path).is_dir():
                self.request_context.remember('project_root',str(path),'USER_REQUEST')
        if authorized:
            self.request_context.remember('paths',[str(path) for path in authorized[:32]],'USER_REQUEST')
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
            if "[WEBAGENT_ATTACHMENT_ERROR]" in queued or 'WEB_FILE_TRANSFER_UNAVAILABLE' in queued:
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
        if PIT.context():
            start = prompt.index("[WEBAGENT_USER_REQUEST]\n") + len("[WEBAGENT_USER_REQUEST]\n")
            end = prompt.rindex("\n[/WEBAGENT_USER_REQUEST]")
            PIT.record("08_user_request_block", prompt[start:end])
        try:
            environment_snapshot = planner_environment_snapshot()
        except Exception as exc:
            environment_snapshot = {
                "schema": ENVIRONMENT_LEDGER_SCHEMA,
                "status": "PROBE_FAILED",
                "tools": {},
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
        prompt += "\n" + render_initial_planner_toolkit(environment_snapshot)
        from agent_core.prerequisite_resolution import render_prerequisites
        prompt += "\n" + render_prerequisites(
            request, self.authorized_paths, environment_snapshot
        )
        if self.image_delivery_plan:
            prompt += (
                "\n[WEBAGENT_IMAGE_DELIVERY_PLAN]\n"
                "Generate exactly one image and wait for the media UI to finish. Do not generate a second "
                "image and do not emit a phase-2 download envelope. After a request-scoped fresh image is "
                "stable, the software runtime prioritizes staging and local PNG delivery for image-only or "
                "CONTINUE Progress-only responses through the validated action path. Do not claim a download "
                "has run before its Runtime result. If your response includes other operational actions, keep "
                "them explicit; Runtime will preserve that response for normal admission. After a failed "
                "download, use the actual error to decide a safe next step; never regenerate or blindly repeat.\n"
                f"delivery={self.image_delivery_plan['delivery']}\n"
                "[/WEBAGENT_IMAGE_DELIVERY_PLAN]"
            )
        prompt += "\n" + lazy_context_sync_prompt(request, self.workspace)
        prompt += "\n" + format_prompt_context(self.progress_ledger)
        from agent_core.tri_one_answer_only import (
            enabled as _tri_one_text_enabled, classify_original_request,
        )
        from agent_core.tri_one_task_result import enabled as _tri_one_result_enabled
        if _tri_one_result_enabled() and _tri_one_text_enabled(source=self._tri_one_origin):
            text_candidate, _ = classify_original_request(
                request, attachments=self._tri_one_has_attached_context,
                authorized_paths=bool(authorized),
                image_or_artifact_request=bool(self.image_delivery_plan),
                continuation=bool(self.continuation_source),
            )
            if text_candidate:
                prompt += (
                    "\n[TRI_ONE_ANSWER_ONLY_CANDIDATE]\n"
                    "The original request may qualify for text-only completion. "
                    "If it needs no external information or side effects, emit ONE "
                    "canonical v9 turn: complete report_progress, final_response "
                    "and turn_commit. Set every completed step to COMPLETED and "
                    "cite no invented evidence. Runtime will decide eligibility; "
                    "any tool need requires the ordinary Action Loop.\n"
                    "[/TRI_ONE_ANSWER_ONLY_CANDIDATE]"
                )
        from agent_core.strict_observation_guard import strict_observation_only_request
        if strict_observation_only_request(self.task_request):
            prompt += (
                "\n[STRICT_OBSERVATION_ONLY_REQUEST]\n"
                "原始 Request 僅准許事前註冊成功條件與指定次數的 canonical observe_fact，禁止 read_file、"
                "inspect_project_ledger、其他補救／占位操作。第一輪先用 report_progress 登記兩個獨立"
                " completion_contract.success，並以 turn_commit(action_count=1) 結束本輪；"
                "本輪沒有 operational action，正式 condition_id 由 Runtime 接受 Progress 後發行。"
                "下一輪取得兩個正式 condition_id，再依序各使用一次 observe_fact。"
                "任何補救工具不得自行加入；若 Runtime 不允許條件登記，保留拒絕而非改動檔案。\n"
                "[/STRICT_OBSERVATION_ONLY_REQUEST]"
            )
        if self.continuation_source:
            from agent_core.task_continuation import continuation_prompt
            prompt += continuation_prompt(self.continuation_source, self.continuation_mode)
        self._emit(
            "context_sync_deferred",
            request_id=self.run_id,
            context_state="NOT_LOADED",
            selection_owner="WEBGPT",
        )

        for _ in range(self.max_turns):
            self._check_cancelled()
            from agent_core.bounded_repair import repair_deadline_expired
            if repair_deadline_expired(
                self._bounded_repair.rejected_turns,
                time.monotonic() - self._request_started_monotonic,
            ):
                reason = "bounded_repair:request_elapsed_soft_deadline"
                set_runtime_state(self.task_id, "PAUSED", reason=reason,
                                  root=self.progress_root)
                self._emit("protocol_loop_paused", request_id=self.run_id,
                    round=self.turn_id, reason=reason,
                    rejected_turns=self._bounded_repair.rejected_turns)
                # This checkpoint never kills an in-flight process or declares
                # the task terminal, and cannot create a fake success receipt.
                raise RuntimeError("Repair elapsed budget exhausted; task safely paused. " + reason)
            if self._stagnation_pending:
                reason = "semantic_stagnation:repeated_authorization_or_preflight_blocker"
                set_runtime_state(self.task_id, "PAUSED", reason=reason,
                                  root=self.progress_root)
                self._emit("protocol_loop_paused", request_id=self.run_id,
                    round=self.turn_id, reason=reason,
                    stagnation=self._stagnation_pending)
                raise RuntimeError("Repeated prerequisite/scope denial; paused without replay. " + reason)
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
            transport_started_at = time.time()
            try:
                response = self.planner(self._with_commit(prompt, expected), expected, attachments)
                PIT.record("11_planner_response", response, evidence_status="ACTION_VALUE", extra={"round": self.turn_id})
            except BaseException as exc:
                self.protocol_state = "INTERRUPTED"
                if self._gar is not None:
                    # L0 stale DOM/ACK is transport uncertainty, not a
                    # second Planner decision. Do NOT re-send or execute.
                    snapshot = self._gar_snapshot()
                    classification = self._gar.transport_failure(exc, snapshot)
                    self._gar_persist()
                    self._emit("gar_transport_integrity", request_id=self.run_id,
                               round=self.turn_id, **self._gar.diagnostic())
                    if classification.reason in {
                        "STALE_TRANSPORT_READBACK", "TRANSPORT_UNCERTAIN"
                    }:
                        set_runtime_state(self.task_id, "PAUSED",
                                          reason=classification.reason,
                                          root=self.progress_root)
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
            self._reconcile_read_attachment_delivery(attachments, transport_started_at, expected)
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
                if (rejection_signature == last_rejected_exchange_signature
                        and self._gar is None):
                    reason = "semantic_stagnation:repeated_protocol_rejection"
                    set_runtime_state(self.task_id, "PAUSED", reason=reason, root=self.progress_root)
                    raise RuntimeError(
                        "WebAgent 連續回傳相同的無效協議內容，Runtime 已暫停重複問答"
                    )
                last_rejected_exchange_signature = rejection_signature
                self._emit("protocol_rejected", request_id=self.run_id, round=self.turn_id, diagnostics=diagnostics)
                repair_budget = self._admit_bounded_repair(diagnostics)
                gar_prompt = self._gar_rejection(
                    diagnostics, repair_budget=repair_budget,
                )
                prompt = (
                    "[WEBAGENT_PROTOCOL_REJECTED]\n"
                    + format_tool_parse_diagnostics(diagnostics)
                    + "\n修正 transport/schema 後重送同一決策；不得假設 action 已執行。"
                )
                pending_recovery = self._result_recovery_guidance([])
                if pending_recovery:
                    prompt += "\n" + pending_recovery
                if gar_prompt:
                    prompt += "\n" + gar_prompt
                continue

            calls = self._restore_progress_repair_actions(calls)
            calls, action_repair_diagnostic = self._restore_action_repair_batch(calls)
            if action_repair_diagnostic:
                actions, ack_diagnostics = [], [action_repair_diagnostic]
            else:
                actions, ack_diagnostics = self._accept_ack(calls, expected)
            if ack_diagnostics:
                self._record_preexecution_predicate_rejection(calls, ack_diagnostics)
                self.protocol_state = "REJECTED"
                if self.pending_action_repair and not any(
                    item.get("repair_scope") == "ACTION_BATCH"
                    for item in ack_diagnostics
                ):
                    invalidated_ids = list(
                        self.pending_action_repair.get("preserved_action_ids") or []
                    )
                    self.pending_action_repair = {}
                    self._emit(
                        "action_repair_batch_invalidated",
                        request_id=self.run_id,
                        round=self.turn_id,
                        action_ids=invalidated_ids,
                        reason="non_action_batch_rejection",
                    )
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
                repair_budget = self._admit_bounded_repair(ack_diagnostics)
                from agent_core.task_progress import record_progress_repair
                progress_diagnostic = next((row for row in ack_diagnostics if row.get('reason')=='invalid_progress_payload' and row.get('field_issues')), None)
                if progress_diagnostic:
                    self.progress_ledger = record_progress_repair(self.task_id, progress_diagnostic,
                        round_id=self.turn_id, root=self.progress_root) or self.progress_ledger
                rejection_signature = self._rejected_exchange_signature(
                    calls, ack_diagnostics, response,
                )
                if progress_diagnostic:
                    from agent_core.progress_recovery import repair_signature
                    rejection_signature = repr(repair_signature(progress_diagnostic, len(self.action_result_ledger)))
                if (rejection_signature == last_rejected_exchange_signature
                        and self._gar is None):
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
                gar_prompt = self._gar_rejection(
                    ack_diagnostics, repair_budget=repair_budget,
                )
                repair_note = ""
                spec = self._safe_protocol_spec_rejection()
                if spec:
                    repair_note += (
                        "\\n[PROTOCOL_SPEC_REJECTION]\\n"
                        + json.dumps(spec, ensure_ascii=False, separators=(',', ':'))
                        + "\\n此非法 Predicate 已由 Runtime 在 Tool Envelope Admission 階段拒絕，"
                          "未執行觀測，不能宣稱 Predicate PASS 或 FAIL。"
                          "若原始使用者要求禁止修正 Predicate 或使用替代工具，"
                          "可在下一輪選擇 report_progress decision=INTERRUPT、outcome=UNKNOWN，"
                          "並用 context_request={kind:BLOCKED,detail:具體規格拒絕,"
                          "blocking_action_id:上述 action_id}，"
                          "evidence_refs 引用 PROTOCOL_SPEC_REJECTION；"
                          "Runtime 會重新檢查同 Request/Epoch、無已執行操作及待決副作用。"
                          "不要用 read_file 等替代操作繞過原始限制。"
                    )
                field_repairs = [repair for diagnostic in ack_diagnostics for repair in diagnostic.get('command_field_repairs',[])]
                if field_repairs:
                    repair_note += '\n[COMMAND_FIELD_REPAIR]\n' + json.dumps(field_repairs,ensure_ascii=False) + '\n[/COMMAND_FIELD_REPAIR]\n只回覆同一 tool/action_id 與 required_fields；不必重寫 command、operation、working_directory。仍需 report_progress 與 turn_commit。'
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
                    repair_note += '\n[PROGRESS_EVIDENCE_REPAIR]\n' + json.dumps([
                        {key: item[key] for key in ('field_issues', 'available_evidence_refs', 'evidence_reference_policy')
                         if key in item}
                        for item in ack_diagnostics if item.get('reason') == 'invalid_progress_payload'
                    ], ensure_ascii=False) + '\n[/PROGRESS_EVIDENCE_REPAIR]'
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
                action_batch_repairs = [
                    item for item in ack_diagnostics
                    if item.get("repair_scope") == "ACTION_BATCH"
                ]
                if action_batch_repairs:
                    repair_indexes = sorted({
                        int(index)
                        for item in action_batch_repairs
                        for index in list(item.get("repair_action_indexes", []) or [])
                    })
                    repair_note += (
                        "\n這是 ACTION_BATCH_REPAIR。Runtime 已暫存同輪合法 action："
                        + json.dumps(preserved_action_ids, ensure_ascii=False)
                        + "；只輸出一個 report_progress，並依下列原始 action 位置順序輸出"
                        "完整修正版 action："
                        + json.dumps(repair_indexes, separators=(",", ":"))
                        + "。若只有一個失敗位置且 canonical 修正需要多個 action（例如 "
                        "begin_file_write → write_file_chunk × N → commit_file_write），"
                        "可輸出完整 replacement sequence"
                        + "。不要重送 preserved action。Runtime 會依原順序合併並重新驗證整批；"
                        "合併完成前不會執行任何 action。"
                    )
                elif preserved_action_ids:
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
                if gar_prompt:
                    prompt += "\n" + gar_prompt
                continue
            last_rejected_exchange_signature = None

            progress_action = next(action for action in actions if action.get("tool") == "report_progress")
            from agent_core.task_progress import record_progress_repair
            if self.read_only_progress_deferred:
                self.progress_ledger = record_progress_repair(self.task_id, self.deferred_progress_repair,
                    round_id=self.turn_id, root=self.progress_root) or self.progress_ledger
            else:
                self.progress_ledger = record_model_progress(
                    self.task_id, progress_action, request_id=self.run_id, goal=request,
                    round_id=self.turn_id, root=self.progress_root,
                )
                if self.progress_ledger.repair_state.get('status') == 'REPAIR_REQUIRED':
                    self.progress_ledger = record_progress_repair(self.task_id, {},
                        round_id=self.turn_id, root=self.progress_root) or self.progress_ledger
                self.deferred_progress_repair = {}
                self.last_deferred_read_signature = None
            # Admission is request-scoped. Install only the accepted,
            # Runtime-recorded progress clauses (never raw Planner action
            # fields), after progress persistence but before any Tool Action.
            bind_completion = getattr(self.tools, "bind_runtime_completion_contract", None)
            if bind_completion is not None:
                bind_completion(
                    self.progress_ledger.completion_contract,
                    request_id=self.run_id, task_epoch=self.task_epoch,
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
                self.terminal_outcome = self.progress_ledger.outcome
                self.protocol_state = "INTERRUPTED"
                set_runtime_state(
                    self.task_id,
                    "INTERRUPTED",
                    reason=self.progress_ledger.decision_reason,
                    root=self.progress_root,
                )
                terminal_receipt = self._accepted_terminal_receipt(runtime_state="INTERRUPTED")
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
                raise RuntimeError("WebAgent 已接受中斷終態（模型決策中斷），receipt_id="
                    + terminal_receipt["receipt_id"] + "：" + content)

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
                self._accepted_terminal_receipt(runtime_state="COMPLETED")
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
            if self.read_only_progress_deferred:
                progress_result = '[TASK_PROGRESS_REPAIR_REQUIRED] 已保留最後有效 Progress；本輪只准入合法讀取，未接受錯誤更新。'
            if capability_guidance:
                progress_result += "\n" + capability_guidance
            results = [{
                "action_id": progress_action.get("action_id", ""),
                "tool": "report_progress",
                "result": progress_result,
            }]
            if self.continuation_store:
                self.continuation_store.pending_batch(self.task_id, actions)
            def execute_with_events(action: dict) -> str:
                self._check_cancelled()
                historical = self.action_result_ledger.get(str(action.get('action_id', '')), {})
                if historical.get('continuation_import'):
                    # Cached readback is historical, not a new execution or
                    # verification. Never turn it into fresh evidence.
                    cached_result = self._execute_action(action)
                    return '[TASK_CONTINUATION_CACHED_RESULT] 這是歷史回讀，沒有重新執行；原始驗證與最新有效驗證不同時，以後者為目前狀態。\n' + json.dumps({
                        'action_id':action.get('action_id'), 'source_request_id':historical.get('source_request_id'),
                        'effective_verification_status':historical.get('effective_verification_status', 'UNKNOWN'),
                        'historical_result':cached_result}, ensure_ascii=False)
                from agent_core.action_dependencies import unsatisfied_dependencies
                blocked = unsatisfied_dependencies(action, self.action_result_ledger, self.run_id, self.task_epoch)
                late_errors = []
                from agent_core.action_dependencies import is_observation
                # GAR cannot clear a previously executed/UNKNOWN mutation by
                # changing only the Action ID during a new plan. A legitimate
                # new operation requires a different payload and still passes
                # ordinary scope/admission/tool security.
                if (self._gar is not None
                        and (self._gar.rebases or self._gar.replans)
                        and not is_observation(action)
                        and str(action.get("action_id", "")) not in self.action_ledger):
                    from agent_core.goal_anchored_recovery import mutation_fingerprint
                    candidate = mutation_fingerprint(action)
                    for historical_id, entry in self.action_ledger.items():
                        old_action = entry.get("action")
                        if (isinstance(old_action, dict)
                                and historical_id != str(action.get("action_id", ""))
                                and not is_observation(old_action)
                                and mutation_fingerprint(old_action) == candidate
                                and entry.get("executed") is not False):
                            from agent_core.tool_result_contract import encode_result
                            self._emit("gar_mutation_replay_blocked",
                                       request_id=self.run_id, round=self.turn_id,
                                       action_id=action.get("action_id", ""),
                                       prior_action_id=historical_id,
                                       reason="GAR_POTENTIAL_MUTATION_REPLAY")
                            return encode_result(
                                "REJECTED", "GAR_POTENTIAL_MUTATION_REPLAY",
                                tool=action.get("tool", ""), executed=False,
                            )
                if not blocked and not is_observation(action) and str(action.get('action_id','')) not in self.action_ledger:
                    self._refresh_action_loop_state()
                    _,late_errors = self._probe_action_admission([
                        dict(progress_action), dict(action), {'tool':'turn_commit','action_count':2}
                    ], expected, single=True)
                if (blocked or late_errors) and str(action.get('action_id','')) not in self.action_ledger:
                    from agent_core.tool_result_contract import encode_result
                    repair = {'action_id':action['action_id'], 'tool':action.get('tool'),
                        'status':'BLOCKED_DEPENDENCY', 'executed':False, 'action':dict(action),
                        'diagnostics':late_errors or [{'reason':'action_dependency_unsatisfied', 'detail':','.join(blocked)}]}
                    self.deferred_action_repairs[action['action_id']] = repair
                    self.current_action_repairs.append(repair)
                    self._emit('action_dependency_blocked',request_id=self.run_id,round=self.turn_id,**repair)
                    return encode_result('REJECTED','ACTION_DEPENDENCY_UNSATISFIED',
                        tool=action.get('tool',''),executed=False,data={'depends_on':blocked})
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
                if self.read_only_progress_deferred:
                    self.progress_ledger = record_progress_repair(self.task_id, self.deferred_progress_repair,
                        round_id=self.turn_id, executed_action_ids=[str(action.get('action_id',''))],
                        root=self.progress_root) or self.progress_ledger
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
                    cached = self.action_ledger.get(str(action.get('action_id', ''))) or {}
                    action_evidence = ({key:cached[key] for key in ('tool','operation','execution_status','verification_status','expectation_status','evidence_state','terminal_eligible','result_status','result_reason','executed') if key in cached}
                                       if cached else self._classify_action_evidence(action, result))
                    ledger_entry = v8_build_result(
                        admitted, action_result_id, "COMMITTED", result,
                    )
                    ledger_entry.update(action_evidence)
                    ledger_entry = self._record_action_result_evidence(action, ledger_entry)
                    if self.continuation_store:
                        self.continuation_store.snapshot(self)
                    if self.request_context and ledger_entry.get('result_status') == 'SUCCEEDED' and ledger_entry.get('executed') is True:
                        from agent_core.typed_field_values import STABLE_FIELDS
                        from agent_core.request_context import RequestContextError
                        for field in STABLE_FIELDS.intersection(action):
                            try:
                                reference=self.request_context.remember(field,action[field],'EXECUTED_ACTION')
                                self._emit('request_context_input_recorded',request_id=self.run_id,round=self.turn_id,
                                           action_id=action['action_id'],field=field,reference=reference)
                            except RequestContextError as exc:
                                self._emit('request_context_input_not_cached',request_id=self.run_id,field=field,reason=str(exc))
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
            if self.current_action_repairs:
                results.append({'tool':'action_admission', 'action_id':'',
                    'result':json.dumps({'schema':'SMARTAGENT_ACTION_ADMISSION_V1',
                        'deferred_actions':self.current_action_repairs,
                        'instructions':'只補 deferred_actions；已執行的 action 不要重送。依賴失敗先修正前置條件，不代表整個任務失敗。'},ensure_ascii=False)})
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
            from agent_core.blocker_recovery import BlockerRecovery
            if not hasattr(self, "_blocker_recovery"):
                self._blocker_recovery = BlockerRecovery()
            blocker_state = self._blocker_recovery.observe(terminal_recovery, results)
            if blocker_state != "inactive":
                self._emit("blocker_recovery_updated", request_id=self.run_id,
                           round=self.turn_id, state=blocker_state,
                           missing_fields=terminal_recovery.get("missing_fields", []))
            if blocker_state == "stalled":
                detail = "invalid_edit_plan:no_new_source_evidence_or_resolved_fields"
                set_runtime_state(self.task_id, "PAUSED", reason=detail, root=self.progress_root)
                self._emit("protocol_loop_paused", request_id=self.run_id,
                           round=self.turn_id, reason=detail, recovery_context=terminal_recovery)
                from agent_core.request_ownership import release_active_request
                release_active_request(self.run_id)
                raise RuntimeError("WebAgent 計畫補救未解除相同阻塞；Runtime 已暫停。detail=" + detail)
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
            if self.read_only_progress_deferred:
                from agent_core.progress_recovery import repair_signature
                facts = tuple(sorted((str(action.get('tool', '')),
                    tuple(self.tools.preview_tool_scope(action).get('resolved_paths') or []),
                    (self.action_ledger.get(str(action.get('action_id', ''))) or {}).get('read_fact_digest', '')) for action in actions))
                signature = repair_signature(self.deferred_progress_repair, facts)
                if signature == self.last_deferred_read_signature:
                    set_runtime_state(self.task_id, 'PAUSED', reason='progress_repair_stalled_no_new_read_evidence', root=self.progress_root)
                    self._emit('progress_repair_stalled', request_id=self.run_id, round=self.turn_id,
                               diagnostic=self.deferred_progress_repair)
                    raise RuntimeError('WebAgent 重複讀取未取得新資料，且必要 Progress 欄位仍未修復；Runtime 已暫停')
                self.last_deferred_read_signature = signature
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
            if self.deferred_progress_repair:
                prompt += '\n[PROGRESS_FIELD_REPAIR]\n' + json.dumps(self.deferred_progress_repair, ensure_ascii=False, separators=(',', ':')) + '\n[/PROGRESS_FIELD_REPAIR]\n只修正這些欄位並選擇真正的下一步；上述 action 已有實際結果，禁止重做。'
            recovery_guidance = self._result_recovery_guidance(actions)
            if recovery_guidance:
                prompt += "\n" + recovery_guidance
            if blocker_state == "repair_required":
                prompt += self._blocker_recovery.guidance(terminal_recovery)
        raise RuntimeError(f"WebAgent 超過最大 protocol turns: {self.max_turns}")
