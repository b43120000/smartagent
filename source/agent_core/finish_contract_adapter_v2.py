"""Git68: Runtime-owned Finish Contract Adapter V2.

The Planner proposes completion, never Runtime receipts or identity. This
adapter binds the proposal to trusted Task/Request/Epoch and frozen Goal.
The Comparator independently replays the complete executor ledger.
"""
from __future__ import annotations

from collections.abc import Mapping

SCHEMA = "SMARTAGENT_CANONICAL_FINISH_CONTRACT_V2"


def adapt_finish_contract_v2(
    decision, *, task_id: str, request_id: str, task_epoch: str,
    frozen_goal_contract: Mapping,
) -> dict:
    """Canonicalize intent without copying or fabricating ToolResults."""
    if (decision.kind != "finish"
            or decision.contract_version != 2
            or decision.expected_runtime not in (None, [])):
        raise ValueError("FINISH_V2_PROPOSAL_INVALID")
    if not isinstance(frozen_goal_contract, Mapping):
        raise ValueError("FINISH_V2_FROZEN_GOAL_UNAVAILABLE")
    goal_sha = frozen_goal_contract.get("contract_sha256")
    if not isinstance(goal_sha, str) or len(goal_sha) != 64:
        raise ValueError("FINISH_V2_FROZEN_GOAL_UNAVAILABLE")
    return {
        "schema": SCHEMA,
        "task_binding": {
            "task_id": task_id,
            "request_id": request_id,
            "task_epoch": task_epoch,
        },
        "planner_intent": {
            "type": "finish",
            "summary": decision.message,
            "goal_checks": decision.goal_checks,
        },
        "runtime_authority": {
            "ledger_source": "EXECUTOR_BLACKBOARD_AND_ACTION_LEDGER",
            "evidence_scope": "ALL_TASK_ACTIONS",
            "goal_source": "FROZEN_GOAL_STATE_CONTRACT",
            "goal_contract_sha256": goal_sha,
        },
    }


def validate_finish_contract_v2(contract: object, loop, board: Mapping) -> bool:
    """Fail closed on changed identity, source, contract, or invented fields."""
    if not isinstance(contract, dict) or set(contract) != {
        "schema", "task_binding", "planner_intent", "runtime_authority",
    }:
        return False
    expected_binding = {
        "task_id": loop.task_id,
        "request_id": loop.run_id,
        "task_epoch": loop.task_epoch,
    }
    frozen = board.get("goal_state_contract") if isinstance(board, Mapping) else None
    if not isinstance(frozen, Mapping):
        return False
    authority = {
        "ledger_source": "EXECUTOR_BLACKBOARD_AND_ACTION_LEDGER",
        "evidence_scope": "ALL_TASK_ACTIONS",
        "goal_source": "FROZEN_GOAL_STATE_CONTRACT",
        "goal_contract_sha256": frozen.get("contract_sha256"),
    }
    intent = contract.get("planner_intent")
    return (
        contract.get("schema") == SCHEMA
        and contract.get("task_binding") == expected_binding
        and contract.get("runtime_authority") == authority
        and isinstance(intent, dict)
        and set(intent) == {"type", "summary", "goal_checks"}
        and intent.get("type") == "finish"
        and isinstance(intent.get("summary"), str)
        and 0 < len(intent["summary"]) <= 3000
        and (
            intent.get("goal_checks") is None
            or isinstance(intent["goal_checks"], list)
        )
    )


__all__ = ["SCHEMA", "adapt_finish_contract_v2", "validate_finish_contract_v2"]
