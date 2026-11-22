"""Test-only, nonauthorizing Git57 Finish response fault injector.

No global env switch or production loop hook. The caller must explicitly wrap a
Planner in an isolated test process. Only its first *valid* Finish is changed.
Original Runtime Evidence, user Goal, ToolResults and OS admission are not
writable through this adapter.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from agent_core.executor_run_blackboard import append_executor_event
from agent_core.tool_result_contract import decode_result


def sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TestOnlyFirstFinishInjector:
    def __init__(self, planner, loop, *, field: str = "data.observed_exists"):
        if field != "data.observed_exists":
            raise ValueError("GIT57_INJECTION_FIELD_NOT_ALLOWLISTED")
        self.planner = planner
        self.loop = loop
        self.field = field
        self.injected = False
        self.events: list[dict] = []

    def __call__(self, prompt, expected, attachments):
        answer = self.planner(prompt, expected, attachments)
        if self.injected:
            return answer
        if not isinstance(answer, str):
            return answer
        try:
            data = json.loads(answer)
        except (ValueError, TypeError):
            return answer
        if not isinstance(data, dict) or data.get("type") != "finish":
            return answer
        claims = data.get("expected_runtime")
        if (not isinstance(claims, list) or len(claims) != 1
                or not isinstance(claims[0], dict)):
            return answer
        if (not isinstance(claims[0].get("fields"), dict)
                or type(claims[0]["fields"].get(self.field)) is not bool):
            return answer
        ledger = getattr(self.loop, "action_result_ledger", None)
        if not isinstance(ledger, dict) or len(ledger) != 1:
            return answer
        entry = next(iter(ledger.values()))
        receipt = decode_result(entry.get("result", ""))
        ref = entry.get("evidence_ref")
        if (not isinstance(receipt, Mapping)
                or receipt.get("status") != "SUCCEEDED"
                or receipt.get("executed") is not True
                or not isinstance(ref, str) or not ref.startswith("MIN-EV-")
                or claims[0].get("evidence_ref") != ref
                or claims[0]["fields"].get("status") != receipt.get("status")
                or entry.get("tool") != "inspect_directory"):
            return answer
        actual = (receipt.get("data") or {}).get("observed_exists")
        if type(actual) is not bool or claims[0]["fields"][self.field] is not actual:
            return answer
        # Confirm Runtime already persisted this precise result. A preview
        # alone is not evidence and must never authorize test injection.
        pointer = entry.get("evidence_store") or {}
        if (pointer.get("store_status") != "PERSISTED_AND_READBACK_VERIFIED"
                or pointer.get("evidence_ref") != ref):
            return answer
        original = sha256(answer)
        data["expected_runtime"] = [{
            "evidence_ref": ref,
            "fields": {**claims[0]["fields"], self.field: not actual},
        }]
        injected = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        event = {
            "kind": "GIT57_TEST_FINISH_INJECTION",
            "round": int(self.loop.turn_id),
            "injected_field": self.field,
            "target_evidence_ref": ref,
            "runtime_actual_value": actual,
            "planner_declared_value_before_injection": actual,
            "planner_declared_value_after_injection": not actual,
            "response_sha256_before": original,
            "response_sha256_after": sha256(injected),
            "injection_count": 1,
            "test_only": True,
            "executed": False,
            "authorization_granted": False,
        }
        # The test event is additive metadata; it never edits a ToolResult.
        append_executor_event(
            getattr(self.loop, "progress_root", None) or self.loop.workspace,
            self.loop, event,
        )
        self.events.append(event)
        self.injected = True
        return injected


__all__ = ["TestOnlyFirstFinishInjector", "sha256"]
