#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Independent WebAgent protocol/result/ACK loop."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Callable

from agent_core.smartagent_protocol import (
    analyze_tool_transport,
    format_tool_parse_diagnostics,
    validate_ack_turn,
)

from .tool_context import WebAgentToolContext


PlannerCall = Callable[[str, dict, list[str]], str]
EventSink = Callable[[str], None]


def extract_authorized_paths(request: str) -> list[str]:
    return [
        match.group(0).strip().rstrip(".,;，；。")
        for match in re.finditer(r"[A-Za-z]:[\\/][^\r\n]+", str(request or ""))
    ]


def local_commit_line(expected: dict) -> str:
    payload = {
        "run_id": expected["run_id"],
        "turn_id": expected["turn_id"],
        "local_nonce": expected["local_nonce"],
        "ack_result_id": expected["ack_result_id"],
        "ack_web_ack_id": expected["ack_web_ack_id"],
        "protocol_name": "web_agent_direct",
        "protocol_version": 1,
    }
    return "[WEBAGENT_LOCAL_COMMIT] " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def action_signature(action: dict) -> str:
    blob = json.dumps(action, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()


class WebAgentProtocolLoop:
    def __init__(
        self,
        workspace: str | Path,
        planner: PlannerCall,
        *,
        tool_context: WebAgentToolContext | None = None,
        max_turns: int = 100,
        event_sink: Callable[..., None] | None = None,
        display_name: str = "WebAgent",
    ):
        self.workspace = Path(workspace).expanduser().resolve()
        self.planner = planner
        self.tools = tool_context or WebAgentToolContext(self.workspace)
        self.max_turns = max(1, int(max_turns))
        self.event_sink = event_sink
        self.display_name = str(display_name or "WebAgent")
        self.run_id = ""
        self.turn_id = 0
        self.pending_result_ack_id = ""
        self.pending_web_ack_id = ""
        self.seen_web_ack_ids: set[str] = set()
        self.action_ledger: dict[str, dict] = {}

    def _emit(self, event: str, **fields) -> None:
        if self.event_sink is not None:
            self.event_sink(event, **fields)

    def _new_commit(self) -> dict:
        self.turn_id += 1
        self.tools._protocol_turn_seq = self.turn_id
        return {
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "local_nonce": uuid.uuid4().hex,
            "ack_result_id": self.pending_result_ack_id,
            "ack_web_ack_id": self.pending_web_ack_id,
        }

    @staticmethod
    def _with_commit(prompt: str, expected: dict) -> str:
        trace = (
            "[WEBAGENT_REQUEST_TRACE]\n"
            f"request_id={expected['run_id']}\n"
            f"round={expected['turn_id']}\n"
            "attempt=1\n"
            f"previous_ack_id={expected['ack_web_ack_id']}\n"
            "[/WEBAGENT_REQUEST_TRACE]"
        )
        return (
            str(prompt).rstrip()
            + "\n\n"
            + trace
            + "\n[WEBAGENT_ACK_REQUIRED]\n"
            + "只輸出 smartagent_tool blocks；最後一個 block 必須是 matching turn_commit。\n"
            + local_commit_line(expected)
        )

    def _accept_ack(self, calls: list[dict], expected: dict) -> tuple[list[dict], list[dict]]:
        actions, commit, diagnostics = validate_ack_turn(calls, expected)
        web_ack_id = str((commit or {}).get("web_ack_id", "") or "").strip()
        if not diagnostics and web_ack_id in self.seen_web_ack_ids:
            diagnostics.append({
                "marker": "[WEBAGENT_ACK_REJECTED]",
                "reason": "replayed_web_ack_id",
                "detail": f"web_ack_id={web_ack_id}",
                "suggestion": "產生新的唯一 web_ack_id 後重送同一決策。",
            })
            actions = []
        if not diagnostics:
            self.seen_web_ack_ids.add(web_ack_id)
            self.pending_web_ack_id = web_ack_id
            self.pending_result_ack_id = ""
            self._emit(
                "ack_accepted",
                request_id=expected["run_id"],
                round=expected["turn_id"],
                previous_ack_id=expected["ack_web_ack_id"],
                ack_id=web_ack_id,
            )
        else:
            self._emit(
                "ack_rejected",
                request_id=expected["run_id"],
                round=expected["turn_id"],
                diagnostics=diagnostics,
            )
        return actions, diagnostics

    def _execute_action(self, action: dict) -> str:
        action_id = str(action.get("action_id", "") or "")
        signature = action_signature(action)
        cached = self.action_ledger.get(action_id)
        if cached:
            if cached["signature"] != signature:
                raise RuntimeError(f"action_id payload mismatch: {action_id}")
            return str(cached["result"])
        result = self.tools.execute(action)
        self.action_ledger[action_id] = {"signature": signature, "result": result}
        return str(result)

    def run(
        self,
        request: str,
        *,
        request_id: str = "",
        initial_attachments: list[str] | None = None,
        source_tag: str = "",
    ) -> str:
        request = str(request or "").strip()
        if not request:
            raise ValueError("WebAgent request 不可為空")
        self.run_id = str(request_id or "").strip() or (
            "WA-" + uuid.uuid4().hex[:12].upper()
        )
        self.turn_id = 0
        self.pending_result_ack_id = ""
        self.pending_web_ack_id = ""
        self.seen_web_ack_ids.clear()
        self.action_ledger.clear()
        authorized = extract_authorized_paths(request)
        self.tools.begin_run(self.run_id, request, authorized)
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
        prompt = (
            source_block + "[WEBAGENT_USER_REQUEST]\n"
            + request
            + "\n[/WEBAGENT_USER_REQUEST]\n"
            + f"[WEBAGENT_WORKSPACE]\n{self.workspace}\n[/WEBAGENT_WORKSPACE]"
        )

        for _ in range(self.max_turns):
            expected = self._new_commit()
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
            response = self.planner(self._with_commit(prompt, expected), expected, attachments)
            self._emit(
                "planner_response_received",
                request_id=self.run_id,
                round=self.turn_id,
                response=response,
            )
            report = analyze_tool_transport(response)
            diagnostics = list(report["diagnostics"])
            calls = list(report["calls"])
            if diagnostics:
                self._emit("protocol_rejected", request_id=self.run_id, round=self.turn_id, diagnostics=diagnostics)
                prompt = (
                    "[WEBAGENT_PROTOCOL_REJECTED]\n"
                    + format_tool_parse_diagnostics(diagnostics)
                    + "\n修正 transport/schema 後重送同一決策；不得假設 action 已執行。"
                )
                continue

            actions, ack_diagnostics = self._accept_ack(calls, expected)
            if ack_diagnostics:
                prompt = (
                    "[WEBAGENT_ACK_REJECTED]\n"
                    + format_tool_parse_diagnostics(ack_diagnostics)
                    + "\n修正 ACK 後重送同一決策；不得重做尚未執行的 action。"
                )
                continue

            if len(actions) == 1 and actions[0].get("tool") == "final_response":
                if self.tools.last_verification_status in {"FAIL", "UNVERIFIED"}:
                    prompt = (
                        "[WEBAGENT_VERIFICATION_GATE]\n"
                        f"run_command verification={self.tools.last_verification_status}; "
                        "不可結束。請執行必要修正或明確 verification，PASS 後再 final_response。"
                    )
                    continue
                content = str(actions[0].get("content", ""))
                self._emit(
                    "protocol_loop_completed",
                    request_id=self.run_id,
                    round=self.turn_id,
                    ack_id=self.pending_web_ack_id,
                    final_content=content,
                )
                return content

            results = []
            for action in actions:
                self._emit(
                    "tool_started",
                    request_id=self.run_id,
                    round=self.turn_id,
                    action_id=action.get("action_id", ""),
                    tool=action.get("tool", ""),
                )
                print(
                    f"[{self.display_name}] {self.run_id} round={self.turn_id} 正在執行 "
                    f"{action.get('tool', '')} ({action.get('action_id', '')})...",
                    flush=True,
                )
                result = self._execute_action(action)
                self._emit(
                    "tool_completed",
                    request_id=self.run_id,
                    round=self.turn_id,
                    action_id=action.get("action_id", ""),
                    tool=action.get("tool", ""),
                    result=result,
                )
                results.append({
                    "action_id": action.get("action_id", ""),
                    "tool": action.get("tool", ""),
                    "result": result,
                })
            result_id = "RES-" + uuid.uuid4().hex[:12].upper()
            self.pending_result_ack_id = result_id
            prompt = (
                f"[WEBAGENT_TOOL_RESULTS]\nRUN_ID={self.run_id}\nRESULT_ID={result_id}\n"
                + json.dumps(results, ensure_ascii=False, separators=(",", ":"))
                + "\n[/WEBAGENT_TOOL_RESULTS]\n"
                + "根據結果決定下一步；下一輪 turn_commit 必須 ACK 此 RESULT_ID。"
            )
        raise RuntimeError(f"WebAgent 超過最大 protocol turns: {self.max_turns}")
