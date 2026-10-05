#!/usr/bin/env python3
"""Status-driven Browser Operator (Agent 2) escalation policy.

Agent 2 is deliberately a fallback: deterministic DOM/event evidence remains
the primary controller.  The selected model is called only when evidence is
UNCERTAIN or CONTRADICTED, never to bypass an explicit platform error.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable


WHITELISTED_ACTIONS = {
    "INSPECT_ONLY", "PRESS_ESCAPE", "DISMISS_DIALOG", "RETRY_ONCE", "SAFE_STOP",
}
AMBIGUOUS_PHASES = {"ATTACHMENT_UPLOAD", "ARTIFACT_DOWNLOAD", "NAVIGATION", "COMPOSER"}
EXPLICIT_BLOCKERS = {"RATE_LIMITED", "UPLOAD_LIMIT", "LOGIN_REQUIRED", "ACCESS_DENIED"}


@dataclass(frozen=True)
class EscalationDecision:
    invoke_agent2: bool
    reason: str
    action: str = "INSPECT_ONLY"


def decide_browser_escalation(snapshot: dict[str, Any]) -> EscalationDecision:
    phase = str(snapshot.get("task_phase", "") or "").upper()
    observed = str(snapshot.get("observed_state", "") or "").upper()
    confidence = str(snapshot.get("ui_confidence", "CONFIRMED") or "CONFIRMED").upper()
    error_code = str(snapshot.get("error_code", "") or "").upper()

    if confidence == "CONFIRMED" and (observed in EXPLICIT_BLOCKERS or error_code in EXPLICIT_BLOCKERS):
        return EscalationDecision(False, f"confirmed_platform_blocker:{observed or error_code}", "SAFE_STOP")
    if phase not in AMBIGUOUS_PHASES:
        return EscalationDecision(False, f"phase_not_ui_ambiguous:{phase or 'UNKNOWN'}")
    if confidence in {"UNCERTAIN", "CONTRADICTED"}:
        return EscalationDecision(True, f"{confidence.lower()}:{error_code or observed or 'unknown'}")
    return EscalationDecision(False, "dom_event_evidence_confirmed")


class BrowserOperator:
    """Agent 2 model adapter with a strict, finite action vocabulary."""

    def __init__(self, model_key: str, model_name: str) -> None:
        self.model_key = model_key
        self.model_name = model_name
        self.last_result: dict[str, Any] = {}

    def handle(
        self,
        snapshot: dict[str, Any],
        model_call: Callable[[list[dict[str, str]]], str] | None = None,
    ) -> dict[str, Any]:
        decision = decide_browser_escalation(snapshot)
        result = {
            "agent": "BROWSER_OPERATOR_2",
            "model_key": self.model_key,
            "model": self.model_name,
            "invoked": decision.invoke_agent2,
            "reason": decision.reason,
            "action": decision.action,
            "capability": "DOM_EVENT_STATUS_NON_VISUAL",
        }
        if not decision.invoke_agent2 or model_call is None:
            self.last_result = result
            return result

        evidence = {
            key: snapshot.get(key)
            for key in (
                "task_phase", "observed_state", "ui_confidence", "primary_method",
                "error_code", "detail", "retryable", "retry_budget",
            )
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Browser Operator Agent 2. Analyze only the supplied UI status evidence. "
                    "Return one JSON object with action and reason. action must be one of: "
                    "INSPECT_ONLY, PRESS_ESCAPE, DISMISS_DIALOG, RETRY_ONCE, SAFE_STOP. "
                    "Never bypass rate limits, login, permissions, payments, deletion, or security prompts."
                ),
            },
            {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
        ]
        try:
            response = str(model_call(messages) or "")
            match = re.search(r"\{[\s\S]*?\}", response)
            payload = json.loads(match.group(0)) if match else {}
            action = str(payload.get("action", "INSPECT_ONLY") or "INSPECT_ONLY").upper()
            if action not in WHITELISTED_ACTIONS:
                action = "SAFE_STOP"
            result.update({
                "action": action,
                "model_reason": str(payload.get("reason", "") or "")[:500],
                "model_response_valid": bool(payload),
            })
        except Exception as exc:
            result.update({
                "action": "SAFE_STOP", "model_response_valid": False,
                "error": f"{type(exc).__name__}: {exc}",
            })
        self.last_result = result
        return result
