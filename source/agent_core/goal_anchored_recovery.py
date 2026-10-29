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


def is_unverified_nonexecuted_terminal_requirement(
    requirement: Mapping[str, object] | None, *,
    has_action_history: bool, has_condition_results: bool,
) -> bool:
    """Only classify a zero-evidence *terminal claim* as a protocol prerequisite.

    This is NOT an authorization to accept terminal SUCCESS. The original V9
    verification requirement stays live and must still be satisfied normally.
    Any executed, missing, unknown or inconsistent effect is a HARD blocker.
    """
    if not isinstance(requirement, Mapping) or not requirement:
        return False
    if has_action_history or has_condition_results:
        return False
    allowed = {
        "missing_condition", "condition_id", "current_verification_status",
        "evidence_refs", "required_action", "required_fields",
        "supported_verify_actions", "supported_verify_checks", "next_actions",
    }
    if set(requirement).difference(allowed):
        return False
    if str(requirement.get("current_verification_status", "")).upper() != "UNVERIFIED":
        return False
    # A single REQUEST_ACCEPTED is a Runtime admission marker, not ActionResult
    # or terminal verification evidence. V9 Progress permits this marker even
    # before any tool has run. Treat it as advisory ONLY after the remaining
    # exact-schema/no-history/UNVERIFIED checks below also pass. Arbitrary,
    # mixed or duplicated evidence references remain hard blockers.
    evidence_refs = requirement.get("evidence_refs")
    if evidence_refs not in ([], (), None, ["REQUEST_ACCEPTED"], ("REQUEST_ACCEPTED",)):
        return False
    if requirement.get("required_action") != "run_command":
        return False
    if not str(requirement.get("condition_id") or "").strip():
        return False
    # The verification diagnostic may be formed before the canonical
    # next_actions proposal is materialized. An absent proposal is still
    # an unexecuted prerequisite when the exact normal VERIFY fields exist;
    # it is NOT verification evidence and cannot admit terminal SUCCESS.
    actions = requirement.get("next_actions")
    if actions is None or actions == []:
        fields = requirement.get("required_fields")
        return bool(
            isinstance(fields, list)
            and all(field in fields for field in (
                "operation", "command", "success_criteria", "verify",
            ))
        )
    if not isinstance(actions, list) or len(actions) != 1:
        return False
    action = actions[0]
    return bool(
        isinstance(action, Mapping)
        and action.get("tool") == "run_command"
        and action.get("operation") == "VERIFY"
        and set(action).issubset({"tool", "operation", "condition_id"})
        and action.get("condition_id") == requirement.get("condition_id")
    )


def mutation_fingerprint(action: Mapping[str, object]) -> str:
    """Detect same mutation with a different model-authored Action ID.

    This is a conservative safety check used only during GAR rebase/replan.
    Tool admission and existing idempotency controls remain authoritative.
    """
    if not isinstance(action, Mapping):
        raise ValueError("gar_invalid_action")
    identity_only = {"action_id", "depends_on", "replaces_action_id"}
    normalized = {k: v for k, v in action.items() if k not in identity_only}
    return _digest_json(normalized)


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


@dataclass(frozen=True)
class FreshContextProof:
    """A transport-owned, independently verified *different* planner context.

    Model text, copied DOM and a self-proclaimed conversation ID cannot create
    this proof. Only the trusted browser adapter constructs it after a matching
    protocol bootstrap and a verified, separate conversation binding.
    """

    request_id: str
    task_id: str
    task_epoch: str
    owner: str
    original_conversation_id: str
    fresh_conversation_id: str
    protocol_session_id: str
    protocol_identity_verified: bool
    page_ownership_verified: bool
    previous_generation_inactive: bool


class RecoveryEngine:
    """One request/task/epoch, one bounded recovery budget across rounds."""

    def __init__(self, *, request_id: str, task_id: str, task_epoch: str,
                 original_request: str, owner: str = "",
                 max_rejections: int = 10, max_rebases: int = 1,
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
        self._l4_requested = False
        self._local_repair_exhausted = False
        self._last_local_rejected_total = 0
        self._fresh_context_id = ""
        self._l4_fenced = False
        self._fresh_repair_attempts = 0
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
                 progress: Mapping[str, object] | None = None,
                 verification_preconditions: tuple[str, ...] = ()) -> dict[str, object]:
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
            # Advisory verification is never silently discarded. It may
            # coexist with GAR replan, but it cannot prove terminal SUCCESS.
            "verification_preconditions": sorted({
                _bounded_code(item) for item in verification_preconditions
            }),
            "blockers": sorted(set(normalized_blockers)),
            "recovery_budget": {
                "rejected_rounds": self.rejected_rounds,
                "rebases": self.rebases,
                "replans": self.replans,
                "fresh_sessions": self.fresh_sessions,
                "max_rejections": self.max_rejections,
                "local_repair_exhausted": self._local_repair_exhausted,
                "local_rejected_total": self._last_local_rejected_total,
                "fresh_repair_attempts": self._fresh_repair_attempts,
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
                          original_goal_in_prompt: bool = True,
                          local_repair_budget: Mapping[str, object] | None = None
                          ) -> RecoveryDecision:
        if not self.validate_snapshot(snapshot):
            return self._record("L5", "PAUSED", "GAR_SNAPSHOT_UNVERIFIED")
        if not response_fresh:
            return self._record("L0", "PAUSED", "STALE_TRANSPORT_READBACK")
        if self._unsafe(snapshot):
            return self._record("L5", "PAUSED",
                                "GAR_PENDING_OR_BLOCKED_OPERATION")
        if not original_goal_in_prompt or len(self._original_request) > 4096:
            return self._record("L5", "PAUSED", "GAR_GOAL_PROMPT_UNSAFE")
        # A single BoundedRepairBudget observes each rejected Planner turn.
        # If GAR is wired to it, require exact counter synchronization.
        # Neither L2/L3 nor L4 can reset the total budget by switching
        # conversation or changing Action IDs.
        if local_repair_budget is not None:
            try:
                total = int(local_repair_budget["rejected_turns_total"])
                hard_limit = int(local_repair_budget["max_total"])
                local_exhausted = str(
                    local_repair_budget["status"]
                ) == "EXHAUSTED"
            except (KeyError, TypeError, ValueError):
                return self._record("L5", "PAUSED", "GAR_LOCAL_BUDGET_INVALID")
            if (total != self.rejected_rounds + 1
                    or total <= self._last_local_rejected_total
                    or hard_limit < 1):
                return self._record("L5", "PAUSED", "GAR_LOCAL_BUDGET_DESYNC")
            self._last_local_rejected_total = total
            if local_exhausted:
                self._local_repair_exhausted = True
            if total >= hard_limit:
                self.rejected_rounds = total
                return self._record("L5", "PAUSED", "GAR_GLOBAL_SAFETY_BUDGET_EXHAUSTED")
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
        # A successfully fenced L4 context may return a newly generated but
        # malformed V9 response. Permit at most TWO in-place protocol repairs,
        # using the SAME Planner session and the SAME cumulative budget.
        # Never bootstrap yet another context or replay any tool mutation.
        if self._l4_fenced:
            if self._fresh_repair_attempts >= 2:
                return self._record("L5", "PAUSED",
                                    "GAR_FRESH_CONTEXT_REPAIR_EXHAUSTED")
            self._fresh_repair_attempts += 1
            return self._record(
                "L4", "REPAIR", "GAR_FRESH_CONTEXT_MINIMAL_REPAIR",
                self._prompt("L4", snapshot, code),
            )
        # Legacy call-sites/tests that do not pass the local budget preserve
        # the original GAR policy. A wired Runtime delegates to GAR only
        # after the Bounded Repair LOCAL limit is reached.
        if local_repair_budget is not None and not self._local_repair_exhausted:
            return self._record("L1", "REPAIR", "GAR_LOCAL_REPAIR_ACTIVE")
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
        # L4 is a *request* for a trusted adapter, not permission to send a
        # prompt or create another browser. The Runtime validates and fences
        # the adapter proof before any further Planner operation.
        if self.fresh_sessions >= 1 or self._l4_requested:
            return self._record("L5", "PAUSED", "GAR_FRESH_CONTEXT_UNAVAILABLE")
        self._l4_requested = True
        return self._record("L4", "PREPARE", "GAR_FRESH_CONTEXT_REQUIRED")

    def refuse_fresh_context(self, reason: str) -> RecoveryDecision:
        """Terminal L5 fallback; an unverified adapter must never re-ask."""
        code = _bounded_code(reason, default="GAR_FRESH_CONTEXT_UNAVAILABLE")
        return self._record("L5", "PAUSED", code)

    def activate_fresh_context(self, *, proof: FreshContextProof,
                               snapshot: Mapping[str, object]) -> RecoveryDecision:
        """Admit one new context only against a trusted, same-task proof."""
        if not self._l4_requested or self.fresh_sessions >= 1:
            return self._record("L5", "PAUSED", "GAR_L4_NOT_REQUESTED")
        if not self.validate_snapshot(snapshot) or self._unsafe(snapshot):
            return self._record("L5", "PAUSED", "GAR_L4_SNAPSHOT_OR_EFFECT_UNSAFE")
        if not isinstance(proof, FreshContextProof):
            return self._record("L5", "PAUSED", "GAR_L4_PROOF_INVALID")
        if any((
            proof.request_id != self.request_id,
            proof.task_id != self.task_id,
            proof.task_epoch != self.task_epoch,
            proof.owner != self.owner,
            not proof.original_conversation_id,
            not proof.fresh_conversation_id,
            proof.original_conversation_id == proof.fresh_conversation_id,
            not proof.protocol_session_id,
            not proof.protocol_identity_verified,
            not proof.page_ownership_verified,
            not proof.previous_generation_inactive,
        )):
            return self._record("L5", "PAUSED", "GAR_L4_PROOF_MISMATCH")
        self.fresh_sessions += 1
        self._fresh_context_id = _hash(proof.fresh_conversation_id)[:24]
        self._l4_fenced = True
        return self._record(
            "L4", "ACTIVATED", "GAR_FRESH_CONTEXT_VERIFIED",
            self._prompt("L4", snapshot, "GAR_FRESH_CONTEXT_VERIFIED"),
        )

    def fence_old_planner(self, *, context_id: str) -> bool:
        """Refuse any delayed reply from the previous Planner after L4."""
        if not self._l4_fenced:
            return True
        return _hash(str(context_id))[:24] == self._fresh_context_id

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

    def persist_snapshot(self, snapshot: Mapping[str, object],
                         *, root: str | Path) -> Path:
        """Durably save only the redacted checkpoint, never the raw Goal.

        Scoped under the caller's authorized Runtime state root. This never
        resumes or re-dispatches a task; a separate audited continuation must
        load and verify task ownership, ledger and original Goal first.
        """
        if not self.validate_snapshot(snapshot):
            raise ValueError("gar_snapshot_integrity_failed")
        base = Path(root).expanduser().resolve()
        destination_dir = base / "localdata" / "runtime" / "gar_snapshots"
        destination_dir.mkdir(parents=True, exist_ok=True)
        filename = _hash(
            self.request_id + "\\0" + self.task_id + "\\0" + self.task_epoch
        )[:40] + ".json"
        target = destination_dir / filename
        temp = destination_dir / (filename + ".pending")
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        # A prior crash may leave a .pending file; never consider it current.
        with temp.open("w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(target)
        return target

    def diagnostic(self) -> dict[str, object]:
        """Safe bounded incident summary without original text or raw logs."""
        return {
            "schema": "SMARTAGENT_GAR_DIAGNOSTIC_V1",
            "level": self.level,
            "goal_sha256": self.goal_digest,
            "rejected_rounds": self.rejected_rounds,
            "local_repair_exhausted": self._local_repair_exhausted,
            "local_rejected_total": self._last_local_rejected_total,
            "max_global_rejections": self.max_rejections,
            "rebase_count": self.rebases,
            "replan_count": self.replans,
            "fresh_session_count": self.fresh_sessions,
            "last_reason": self._decisions[-1]["reason"] if self._decisions else "NONE",
            "fresh_context_digest": self._fresh_context_id,
            "old_planner_fenced": self._l4_fenced,
            "fresh_repair_attempts": self._fresh_repair_attempts,
        }


__all__ = ["FEATURE_FLAG", "SCHEMA", "RecoveryDecision", "RecoveryEngine",
           "FreshContextProof", "enabled", "classify_failure", "mutation_fingerprint",
           "is_unverified_nonexecuted_terminal_requirement"]
