"""P0-P2: read-only Protocol Repair Transaction and Query schema normalization.

This module never executes tools, mutates Progress Ledger, grants read/write
scope, invents an operation, or creates Evidence. A cached valid Progress
*proposal* is not an admitted Progress record. Full v9 admission is mandatory.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from collections.abc import Mapping, Sequence

SCHEMA = "SMARTAGENT_PROTOCOL_REPAIR_TRANSACTION_V1"
_QUERY_OPERATIONS = frozenset({
    "search_text", "read_range", "list_tree", "find_references",
    "read_symbol", "get_build_configuration", "get_file_metadata",
})
_INHERITED = ("base_evaluation", "total_steps", "current_step",
              "steps", "completion_contract")
_SEMANTIC = (
    "decision", "outcome", "next_action", "matched_condition",
    "matched_condition_id", "evidence_refs", "next_phase", "goal_transition",
    "path_resolution", "fact_assertion", "context_request",
    "capability_resolution",
)


def _fingerprint(value: object) -> str:
    return sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _ledger_anchor(ledger: object) -> str:
    if ledger is None:
        return ""
    return _fingerprint({
        "request_id": getattr(ledger, "request_id", ""),
        "round_id": getattr(ledger, "round_id", 0),
        "current_step": getattr(ledger, "current_step", None),
        "total_steps": getattr(ledger, "total_steps", None),
        "steps": getattr(ledger, "steps", None),
        "completion_contract": getattr(ledger, "completion_contract", None),
        "base_evaluation": getattr(ledger, "base_evaluation", None),
        "strategy_state": getattr(ledger, "strategy_state", None),
    })


def normalize_query_project(action: Mapping) -> tuple[dict, str]:
    """Reshape ONLY an explicitly named, known query operation.

    - queries={operation:search_text,query:needle} -> queries=[{...}]
    - queries=[{search_text:{query:needle}}] -> explicit operation.
    Unknown operation, missing operation, conflicting envelope, or ambiguous
    multiple operations: no repair. Full Action Admission still follows.
    """
    original = dict(action)
    if original.get("tool") != "query_project":
        return original, ""
    queries = original.get("queries")
    if isinstance(queries, Mapping):
        queries = [dict(queries)]
        mode = "OBJECT_TO_LIST"
    elif isinstance(queries, list):
        queries = deepcopy(queries)
        mode = ""
    else:
        return original, ""
    if not queries or len(queries) > 32:
        return original, ""
    converted = []
    for q in queries:
        if not isinstance(q, Mapping):
            return original, ""
        row = dict(q)
        operation = row.get("operation")
        if operation is None and len(row) == 1:
            name, params = next(iter(row.items()))
            if name in _QUERY_OPERATIONS and isinstance(params, Mapping):
                if "operation" in params:
                    return original, ""
                row = {"operation": name, **dict(params)}
                mode = mode or "EXPLICIT_KEY_TO_OPERATION"
                operation = name
        if not isinstance(operation, str) or operation not in _QUERY_OPERATIONS:
            return original, ""
        converted.append(row)
    if not mode:
        return original, ""
    original["queries"] = converted
    return original, mode


class ProtocolRepairTransaction:
    """Hold a read-only candidate across narrowly scoped schema retries.

    A transaction may reuse a validated Progress candidate ONLY for a later
    query_project Action in the same Request/Epoch and unchanged Progress/
    Action Evidence state, with no contradictory Planner semantic fields.
    The reconstructed proposal goes through every normal v9 gate again.
    """

    def __init__(self, request_id: str, task_epoch: str):
        self.request_id = str(request_id)
        self.task_epoch = str(task_epoch)
        self._proposal: dict | None = None
        self._ledger: str = ""
        self._evidence: str = ""
        self._source_action_ids: tuple[str, ...] = ()
        self.revision = 0

    def clear(self) -> None:
        self._proposal = None
        self._ledger = ""
        self._evidence = ""
        self._source_action_ids = ()

    def capture_query_rejection(
        self, progress: Mapping, operational: Sequence[Mapping],
        *, ledger, action_results: Mapping,
        request_id: str, task_epoch: str,
    ) -> bool:
        from .task_progress import TaskProgressError, validate_model_progress
        if (str(request_id) != self.request_id
                or str(task_epoch) != self.task_epoch
                or not isinstance(progress, Mapping)
                or progress.get("tool") != "report_progress"
                or progress.get("decision") != "CONTINUE"
                or progress.get("next_action") != "query_project"
                or progress.get("outcome", "PENDING") != "PENDING"
                or "goal_transition" in progress
                or len(operational) != 1
                or operational[0].get("tool") != "query_project"):
            return False
        try:
            # Pure validation: do not persist Progress or Action, and do not
            # treat any candidate as evidence that a Tool was executed.
            validate_model_progress(progress, ledger)
        except (TaskProgressError, ValueError, TypeError, KeyError):
            return False
        self._proposal = deepcopy(dict(progress))
        self._ledger = _ledger_anchor(ledger)
        self._evidence = _fingerprint(action_results)
        self._source_action_ids = tuple(
            str(a.get("action_id", "")) for a in operational)
        self.revision += 1
        return True

    def restore_progress(
        self, progress: Mapping, operational: Sequence[Mapping],
        *, ledger, action_results: Mapping,
        request_id: str, task_epoch: str,
    ) -> tuple[dict, str]:
        original = dict(progress)
        saved = self._proposal
        if (not saved or str(request_id) != self.request_id
                or str(task_epoch) != self.task_epoch
                or _ledger_anchor(ledger) != self._ledger
                or _fingerprint(action_results) != self._evidence
                or len(operational) != 1
                or operational[0].get("tool") != "query_project"
                or str(operational[0].get("action_id", ""))
                   not in self._source_action_ids
                or original.get("tool") != "report_progress"
                or original.get("decision") != "CONTINUE"
                or original.get("next_action") != "query_project"):
            return original, ""
        # A changed Step, Goal clause, evidence reference, or selected
        # operation is NOT a schema repair. Preserve it for normal rejection
        # or an explicit semantic Replan rather than silently overriding it.
        for key in (*_SEMANTIC, *_INHERITED):
            if key in original and original[key] != saved.get(key):
                return original, ""
        restored = deepcopy(saved)
        if "action_id" in original:
            restored["action_id"] = original["action_id"]
        return restored, "CACHED_VALID_PROGRESS_PROPOSAL"


__all__ = [
    "SCHEMA", "ProtocolRepairTransaction", "normalize_query_project",
]
