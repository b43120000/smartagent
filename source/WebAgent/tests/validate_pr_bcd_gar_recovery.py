"""GAR recovery classification: safely repairable vs unknown effects."""
from __future__ import annotations

from types import SimpleNamespace

from agent_core.goal_anchored_recovery import GoalAnchoredRecovery
from WebAgent.protocol_loop import WebAgentProtocolLoop


class CaptureGAR:
    def snapshot(self, **kwargs):
        return dict(kwargs)


def mock_loop():
    obj = object.__new__(WebAgentProtocolLoop)
    obj._gar = CaptureGAR()
    obj.progress_ledger = SimpleNamespace()
    obj.turn_id = 0  # No external logger.
    obj.run_id = "RR-R1"
    obj.task_id = "TASK-R1"
    obj.task_epoch = "EPOCH-R1"
    obj.action_ledger = {}
    obj.action_result_ledger = {}
    obj.condition_result_ledger = {}
    obj.action_loop_state = {"blockers": []}
    obj.pending_verification_requirement = {}
    obj.pending_command_field_repairs = {}
    obj.deferred_action_repairs = {}
    return obj


def assert_safe(snapshot):
    assert not snapshot["blockers"], snapshot["blockers"]
    assert not any(snapshot["pending_operations"].values()), snapshot["pending_operations"]


def run():
    # A final, known FAILED INSPECT may be replanned or truthfully reported.
    # It must never be classified as an outstanding mutation.
    x = mock_loop()
    x.action_ledger = {"INSPECT-1": {
        "action": {"tool": "run_command", "operation": "INSPECT",
                   "action_id": "INSPECT-1"},
        "execution_status": "FAILED",
    }}
    x.action_result_ledger = {"INSPECT-1": {
        "executed": True, "result_status": "FAILED",
        "effective_verification_status": "FAIL",
    }}
    x.action_loop_state = {"blockers": [{
        "blocking_action_id": "INSPECT-1", "reason": "REPLAN_REQUIRED",
        "execution_status": "FAILED", "satisfied": False,
    }]}
    assert_safe(x._gar_snapshot())

    # A malformed, never-executed command is allowed bounded field repair.
    y = mock_loop()
    y.pending_command_field_repairs = {"MUTATE-1": {
        "action": {"tool": "run_command", "operation": "MUTATE"},
        "required_fields": ["verify"], "request_id": y.run_id,
        "task_epoch": y.task_epoch,
    }}
    y.deferred_action_repairs = {"MUTATE-1": {
        "action_id": "MUTATE-1", "tool": "run_command", "status": "REPAIR_REQUIRED",
        "executed": False, "action": {"tool": "run_command", "operation": "MUTATE"},
    }}
    y.action_loop_state = {"blockers": [{
        "blocking_action_id": "MUTATE-1", "reason": "REPAIR_REQUIRED",
        "execution_status": "NOT_EXECUTED", "satisfied": False,
    }]}
    assert_safe(y._gar_snapshot())

    # One real in-flight/unknown mutation remains an immediate GAR blocker.
    z = mock_loop()
    z.action_ledger = {"MUTATE-OLD": {
        "action": {"tool": "run_command", "operation": "MUTATE", "action_id": "MUTATE-OLD"},
        "execution_status": "UNKNOWN",
    }}
    z.action_loop_state = {"blockers": [{
        "blocking_action_id": "MUTATE-OLD", "reason": "UNKNOWN_NOT_COMPLETE",
        "execution_status": "UNKNOWN", "satisfied": False,
    }]}
    snapshot = z._gar_snapshot()
    assert snapshot["blockers"]
    assert snapshot["pending_operations"].get("unscoped_action_outcome") is True
    # An unexecuted repair unrelated to this unknown mutation cannot hide it.
    z.pending_command_field_repairs = dict(y.pending_command_field_repairs)
    z.deferred_action_repairs = dict(y.deferred_action_repairs)
    snapshot = z._gar_snapshot()
    assert snapshot["blockers"]
    assert snapshot["pending_operations"].get("unscoped_action_outcome") is True

    print("PR_BCD_GAR_CLASSIFICATION_PASS")


if __name__ == "__main__":
    run()
