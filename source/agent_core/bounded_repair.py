"""Guardrail against repeated Planner protocol repair with no new Runtime evidence.

The guard never turns a rejection into success, never replays an action, and
never generates a terminal receipt. It signals a safe PAUSED state for review.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping


class BoundedRepairBudget:
    """Bound repeated rejected Planner turns within a single request."""

    def __init__(self, max_without_evidence: int = 6, max_total: int = 16,
                 max_same_reason: int = 5):
        if min(max_without_evidence, max_total, max_same_reason) < 1:
            raise ValueError("bounded_repair_budget_invalid")
        self.max_without_evidence = max_without_evidence
        self.max_total = max_total
        self.max_same_reason = max_same_reason
        self._generation: int | None = None
        self._since_evidence = 0
        self._total = 0
        self._reasons: Counter[str] = Counter()

    @property
    def rejected_turns(self) -> int:
        return self._total

    def observe(self, diagnostics: Iterable[Mapping[str, object]],
                *, evidence_generation: int) -> dict[str, object]:
        """Count one rejected turn, not the number of error fields within it."""
        if not isinstance(evidence_generation, int) or evidence_generation < 0:
            raise ValueError("bounded_repair_evidence_generation_invalid")
        if self._generation != evidence_generation:
            self._generation = evidence_generation
            self._since_evidence = 0
            self._reasons.clear()
        reasons = sorted({
            str(diag.get("reason", "protocol_rejected") or "protocol_rejected")
            for diag in diagnostics if isinstance(diag, Mapping)
        }) or ["protocol_rejected"]
        self._total += 1
        self._since_evidence += 1
        for reason in reasons:
            self._reasons[reason] += 1
        exhausted = (
            self._since_evidence >= self.max_without_evidence
            or self._total >= self.max_total
            or any(count >= self.max_same_reason for count in self._reasons.values())
        )
        return {
            "schema": "SMARTAGENT_BOUNDED_REPAIR_V1",
            "status": "EXHAUSTED" if exhausted else "REPAIR_ALLOWED",
            "rejected_turns_total": self._total,
            "rejected_turns_without_new_evidence": self._since_evidence,
            "evidence_generation": evidence_generation,
            "reason_codes": reasons,
            "max_without_evidence": self.max_without_evidence,
            "max_total": self.max_total,
            "max_same_reason": self.max_same_reason,
            "action_replay_allowed": False,
            "terminal_outcome_inferred": False,
        }

class SemanticStagnationGuard:
    """Detect repeated unsuccessful remediation, even when actions have new IDs.

    Count a repeated normalized blocker reason across tools, rather than treating
    executed=True, a new action ID or a timestamp-only response as advancement.
    An actually verified PASS resets the consecutive blocker streak. This is
    deliberately limited to denial/rejection cases; an expected negative
    predicate FAIL is not an error-repair loop.
    """

    def __init__(self, max_same_blocker: int = 4):
        if max_same_blocker < 2:
            raise ValueError("semantic_stagnation_limit_invalid")
        self.max_same_blocker = max_same_blocker
        self._last_blocker = ""
        self._consecutive = 0

    def observe(self, action: Mapping[str, object],
                evidence: Mapping[str, object]) -> dict[str, object]:
        reason = str(evidence.get("result_reason", "") or "").upper()
        verification = str(evidence.get("verification_status", "") or "").upper()
        status = str(evidence.get("result_status", "") or "").upper()
        # A negative predicate FAIL/UNKNOWN may be the correct expected fact.
        scope_blockers = {
            "TOOL_SCOPE_REJECTED", "PREFLIGHT_REJECTED",
            "WORKING_DIRECTORY_REJECTED", "COMMAND_REJECTED",
            "PATH_NOT_AUTHORIZED", "ACTION_DEPENDENCY_UNSATISFIED",
        }
        blocker = reason if reason in scope_blockers and status in {
            "REJECTED", "FAILED", "UNKNOWN"
        } else ""
        if verification == "PASS" or not blocker:
            self._last_blocker = ""
            self._consecutive = 0
        else:
            self._consecutive = self._consecutive + 1 if blocker == self._last_blocker else 1
            self._last_blocker = blocker
        exhausted = bool(blocker and self._consecutive >= self.max_same_blocker)
        return {
            "schema": "SMARTAGENT_SEMANTIC_STAGNATION_V1",
            "status": "EXHAUSTED" if exhausted else "CONTINUE",
            "blocker_reason": blocker,
            "consecutive_same_blocker": self._consecutive,
            "limit": self.max_same_blocker,
            "request_action_tool": str(action.get("tool", "") or ""),
            "authorization_changed": False,
            "terminal_outcome_inferred": False,
        }
