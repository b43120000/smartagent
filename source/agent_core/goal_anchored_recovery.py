"""Goal-Anchored Recovery (GAR): shared, fail-closed Runtime decision engine.

The engine never dispatches a tool, sends a model prompt, creates a browser
session, claims completion, or grants a scope. Existing V9/Progress/Tool/
Terminal admission remains authoritative. Interface-neutral: RemoteAgent
and WebCopilot/WebDirect feed the same bounded events when on shared V9.

GAR v1 is opt-in (SMARTAGENT_GAR_V1=1) and uses L0 -> L1 -> L2 -> L3 ->
L4 (capability gated) -> L5. L4 is *unavailable* until an independently
attested fresh-context adapter exists; the engine never fakes one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "SMARTAGENT_GAR_SNAPSHOT_V1"
FEATURE_FLAG = "SMARTAGENT_GAR_V1"
_LEVELS = {"L0", "L1", "L2", "L3", "L4", "L5"}
_PENDING = {"OPEN", "RUNNING", "QUEUED", "UNKNOWN", "PARTIAL", "PAUSING"}
_READBACK = ("WEB_PROTOCOL_RECOVERY_STALE_READBACK", "RESPONSE_SHA_UNCHANGED",
             "STALE_READBACK", "BOUND_PAGE_COUNT_0")
_SECRETS = re.compile(
    r"(?i)\b(?:bearer|authorization|api[_-]?key|password|token|secret|cookie)"
    r"\b\s*(?:=|:)\s*\S+"
)


def enabled(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return str(values.get(FEATURE_FLAG, "") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _digest_json(value: object) -> str:
    return _hash(json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), default=str))


def _bounded_code(value: object, *, default: str = "UNKNOWN") -> str:
    # Diagnostic code only, never arbitrary exception body or freeform log text.
    match = re.match(r"^[A-Za-z][A-Za-z0-9_:-]{0,95}$",
                     str(value or "").strip())
    return match.group(0).upper() if match else default


def classify_failure(*, error: str = "", reason: str = "",
                     response_fresh: bool | None = None) -> tuple[str, str]:
    """Distinguish stale DOM from an independently fresh model rejection."""
    value = (str(error or "") + " " + str(reason or "")).upper()
    if response_fresh is False or any(word in value for word in _READBACK):
        return "L0", "STALE_TRANSPORT_READBACK"
    if "QUARANTINED" in value or "WRITER_TIMEOUT" in value:
        return "L0", "TRANSPORT_UNCERTAIN"
    if response_fresh is not True:
        return "L0", "FRESHNESS_UNATTESTED"
    if "PROTOCOL" in value or "SCHEMA" in value or "PROGRESS" in value:
        return "L1", "FRESH_PROTOCOL_REJECTION"
    return "L1", "FRESH_RUNTIME_REJECTION"


@dataclass(frozen=True)
class RecoveryDecision:
    level: str
    status: str
    reason: str
    prompt: str = ""
    mutation_allowed: bool = False
    new_session_allowed: bool = False

    def __post_init__(self):
        if self.level not in _LEVELS:
            raise ValueError("gar_unknown_level")
        if self.mutation_allowed or self.new_session_allowed:
            raise ValueError("gar_cannot_grant_execution_or_new_session")


class RecoveryEngine:
    """One request/task/epoch, one bounded recovery budget across rounds."""

    def __init__(self, *, request_id: str, task_id: str, task_epoch: str,
                 original_request: str, owner: str = "",
                 max_rejections: int = 4, max_rebases: int = 1,
                 max_replans: int = 1):
        if not all((request_id, task_id, task_epoch, original_request)):
            raise ValueError("gar_identity_or_goal_missing")
        if min(max_rejections, max_rebases, max_replans) < 1:
            raise ValueError("gar_budget_invalid")
        self.request_id = str(request_id)
        self.task_id = str(task_id)
        self.task_epoch = str(task_epoch)
        self.owner = str(owner)
        self._original_request = str(original_request)
        self.goal_digest = _hash(self._original_request)
        self.max_rejections = max_rejections
        self.max_rebases = max_rebases
        self.max_replans = max_replans
        self.rejected_rounds = 0
        self.rebases = 0
        self.replans = 0
        self.fresh_sessions = 0
        self._last_signature = ""
        self._last_generation: str | None = None
        self.same_blocker = 0
        self.level = "L0"
        self._decisions: list[dict[str, str | int]] = []

    @property
    def original_request(self) -> str:
        return self._original_request

    def _check_identity(self, identity: Mapping[str, object]) -> None:
        if any(str(identity.get(k, "") or "") != expected for k, expected in (
            ("request_id", self.request_id),
            ("task_id", self.task_id),
            ("task_epoch", self.task_epoch),
        )):
            raise ValueError("gar_identity_mismatch")

    def snapshot(self, *, identity: Mapping[str, object],
                 verified_evidence: Mapping[str, object],
                 blockers: list[object] | tuple[object, ...] = (),
                 pending_operations: Mapping[str, object] | None = None,
                 progress: Mapping[str, object] | None = None) -> dict[str, object]:
        """Evidence-minimal, deterministic, integrity-checkable checkpoint.

        The record contains only IDs, finite states, hashes and counters, not
        raw tool output, user request, paths, tokens or model-produced prose.
        """
        self._check_identity(identity)
        refs = []
        pending = []
        for key, value in sorted(verified_evidence.items(), key=lambda x: str(x[0])):
            if not isinstance(value, Mapping):
                continue
            status = _bounded_code(value.get("effective_verification_status")
                                   or value.get("verification_status"))
            refs.append({"ref_digest": _hash(str(key))[:24],
                         "verification": status,
                         "executed": value.get("executed") is True})
            state = _bounded_code(value.get("execution_status")
                                  or value.get("result_status"))
            if state in _PENDING:
                pending.append({"ref_digest": _hash(str(key))[:24],
                                "state": state})
        for name, value in sorted((pending_operations or {}).items()):
            if value:
                pending.append({"ref_digest": _hash(str(name))[:24],
                                "state": "UNKNOWN"})
        normalized_blockers = []
        for item in blockers:
            if isinstance(item, Mapping) and not item.get("satisfied", False):
                normalized_blockers.append(_bounded_code(
                    item.get("reason_code") or item.get("reason")
                ))
        summary = {
            "schema": SCHEMA,
            "goal": {"original_request_sha256": self.goal_digest, "goal_version": 1},
            "identity": {
                "request_id_sha256": _hash(self.request_id),
                "task_id_sha256": _hash(self.task_id),
                "task_epoch_sha256": _hash(self.task_epoch),
                "owner_sha256": _hash(self.owner),
            },
            "checkpoint": {
                "progress_digest": _digest_json(progress or {}),
                "evidence_digest": _digest_json(refs),
                "verified_evidence": refs,
            },
            "pending_operations": pending,
            "blockers": sorted(set(normalized_blockers)),
            "recovery_budget": {
                "rejected_rounds": self.rejected_rounds,
                "rebases": self.rebases,
                "replans": self.replans,
                "fresh_sessions": self.fresh_sessions,
                "max_rejections": self.max_rejections,
            },
            "last_level": self.level,
        }
        summary["integrity"] = {"content_sha256": _digest_json(summary)}
        return summary

    def validate_snapshot(self, snapshot: Mapping[str, object]) -> bool:
        if snapshot.get("schema") != SCHEMA:
            return False
        signature = dict(snapshot.get("integrity") or {}).get("content_sha256")
        payload = {k: v for k, v in snapshot.items() if k != "integrity"}
        identity = dict(payload.get("identity") or {})
        return bool(
            isinstance(signature, str) and signature == _digest_json(payload)
            and dict(payload.get("goal") or {}).get(
                "original_request_sha256"
            ) == self.goal_digest
            and identity.get("request_id_sha256") == _hash(self.request_id)
            and identity.get("task_id_sha256") == _hash(self.task_id)
            and identity.get("task_epoch_sha256") == _hash(self.task_epoch)
            and identity.get("owner_sha256") == _hash(self.owner)
        )

    @staticmethod
    def _unsafe(snapshot: Mapping[str, object]) -> bool:
        return bool(snapshot.get("pending_operations") or snapshot.get("blockers"))

    def _record(self, level: str, status: str, reason: str,
                prompt: str = "") -> RecoveryDecision:
        self.level = level
        self._decisions.append({
            "level": level, "status": status, "reason": reason,
            "rejected_rounds": self.rejected_rounds,
        })
        return RecoveryDecision(level, status, reason, prompt)

    def transport_failure(self, error: Exception | str,
                          snapshot: Mapping[str, object]) -> RecoveryDecision:
        """L0: never resend merely because DOM readback did not change."""
        if not self.validate_snapshot(snapshot):
            return self._record("L5", "PAUSED", "GAR_SNAPSHOT_UNVERIFIED")
        # The adapter already attempted the original request and raised.
        # Do not infer stale DOM merely from a generic network exception or
        # cancellation; classify only from a concrete trusted error code.
        _, reason = classify_failure(error=str(error), response_fresh=None)
        return self._record("L0", "PAUSED", reason)

    def planner_rejection(self, *, reason: str,
                          snapshot: Mapping[str, object],
                          response_fresh: bool,
                          evidence_generation: str,
                          original_goal_in_prompt: bool = True) -> RecoveryDecision:
        if not self.validate_snapshot(snapshot):
            return self._record("L5", "PAUSED", "GAR_SNAPSHOT_UNVERIFIED")
        if not response_fresh:
            return self._record("L0", "PAUSED", "STALE_TRANSPORT_READBACK")
        if self._unsafe(snapshot):
            return self._record("L5", "PAUSED",
                                "GAR_PENDING_OR_BLOCKED_OPERATION")
        if not original_goal_in_prompt or len(self._original_request) > 4096:
            return self._record("L5", "PAUSED", "GAR_GOAL_PROMPT_UNSAFE")
        self.rejected_rounds += 1
        code = _bounded_code(reason, default="PROTOCOL_REJECTED")
        generation = str(evidence_generation)
        signature = code + ":" + generation
        if generation != self._last_generation:
            self._last_generation = generation
            self.same_blocker = 0
        self.same_blocker = self.same_blocker + 1 if signature == self._last_signature else 1
        self._last_signature = signature
        if self.rejected_rounds > self.max_rejections:
            return self._record("L5", "PAUSED", "GAR_SHARED_BUDGET_EXHAUSTED")
        if self.same_blocker <= 1:
            return self._record("L1", "REPAIR", code)
        if self.rebases < self.max_rebases:
            self.rebases += 1
            return self._record("L2", "REBASE", "GAR_PROGRESS_REBASE",
                                self._prompt("L2", snapshot, code))
        if self.replans < self.max_replans:
            self.replans += 1
            return self._record("L3", "REPLAN", "GAR_BOUNDED_REPLAN",
                                self._prompt("L3", snapshot, code))
        # L4 is unsafe without an externally verified fresh-context adapter.
        # It must not silently create a new browser conversation or clear ACK.
        return self._record("L5", "PAUSED", "GAR_FRESH_CONTEXT_UNAVAILABLE")

    def _prompt(self, level: str, snapshot: Mapping[str, object],
                code: str) -> str:
        digest = str(dict(snapshot.get("integrity") or {}).get("content_sha256"))
        # This is a request for a proposal only, not authority to execute.
        return (
            "[GAR_RUNTIME_RECOVERY]\n"
            f"level={level};reason={code};snapshot_sha256={digest}\n"
            "This continues the SAME original user task and immutable scope. "
            "Do not repeat already executed actions, assume unverified tool "
            "success, invent evidence, or change the original goal. "
            "Propose only a valid V9 Progress + canonical Action/Final Response "
            "through existing admission. A response with no safe action should "
            "explicitly INTERRUPT/UNKNOWN instead of claiming SUCCESS.\n"
            "Original user request (trusted Runtime reference, not evidence):\n"
            + self._original_request
            + "\n[/GAR_RUNTIME_RECOVERY]"
        )

    def diagnostic(self) -> dict[str, object]:
        """Safe bounded incident summary without original text or raw logs."""
        return {
            "schema": "SMARTAGENT_GAR_DIAGNOSTIC_V1",
            "level": self.level,
            "goal_sha256": self.goal_digest,
            "rejected_rounds": self.rejected_rounds,
            "rebase_count": self.rebases,
            "replan_count": self.replans,
            "fresh_session_count": self.fresh_sessions,
            "last_reason": self._decisions[-1]["reason"] if self._decisions else "NONE",
        }


__all__ = ["FEATURE_FLAG", "SCHEMA", "RecoveryDecision", "RecoveryEngine",
           "enabled", "classify_failure"]
