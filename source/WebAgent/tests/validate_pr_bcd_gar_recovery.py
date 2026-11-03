"""GAR recovery classification: safely repairable vs unknown effects."""
from __future__ import annotations

from types import SimpleNamespace

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

    # Live B: a denied list_directory with known REJECTED outcome is not an
    # in-flight operation. GAR may repair or give a truthful failure report.
    read = mock_loop()
    read.action_ledger = {"LIST-REJECTED": {
        "action": {"tool": "list_directory", "action_id": "LIST-REJECTED"},
        "execution_status": "FAILED",
    }}
    read.action_result_ledger = {"LIST-REJECTED": {
        "executed": False, "execution_status": "FAILED",
        "result_status": "REJECTED", "effective_verification_status": "UNKNOWN",
    }}
    read.action_loop_state = {"blockers": [{
        "blocking_action_id": "LIST-REJECTED",
        "reason": "REPLAN_REQUIRED", "execution_status": "FAILED",
        "satisfied": False,
    }]}
    assert_safe(read._gar_snapshot())
    # The same rejected operation cannot become a fabricated SUCCESS.
    assert read.action_result_ledger["LIST-REJECTED"]["result_status"] == "REJECTED"

    # Live L03: a known rejected metadata read plus two NOT_EXECUTED,
    # read-only dependents is a safe *replanning* condition, not an unknown
    # in-flight mutation. The blocked chain itself is never replayed by GAR.
    chain = mock_loop()
    chain.action_ledger = {"META-DENIED": {
        "action": {"tool": "inspect_directory", "action_id": "META-DENIED"},
        "execution_status": "FAILED",
    }}
    chain.action_result_ledger = {"META-DENIED": {
        "executed": False, "result_status": "REJECTED",
        "execution_status": "FAILED", "effective_verification_status": "UNKNOWN",
    }}
    for ref, parent, action in [
        ("INSPECT-NOT-RUN", "META-DENIED", {
            "tool": "run_command", "operation": "INSPECT",
            "command": "Get-Item -LiteralPath 'C:\\\\fixture\\\\safe.whl'",
        }),
        ("DEPS-NOT-RUN", "INSPECT-NOT-RUN", {"tool": "list_directory", "path": "deps"}),
    ]:
        action.update({"action_id": ref, "depends_on": [parent]})
        chain.deferred_action_repairs[ref] = {
            "action_id": ref, "tool": action["tool"], "action": action,
            "status": "BLOCKED_DEPENDENCY", "executed": False,
        }
    chain.action_loop_state = {"blockers": [
        {"blocking_action_id": "META-DENIED", "reason": "REPLAN_REQUIRED",
         "execution_status": "FAILED", "satisfied": False},
        {"blocking_action_id": "INSPECT-NOT-RUN", "reason": "BLOCKED_DEPENDENCY",
         "execution_status": "NOT_EXECUTED", "satisfied": False},
        {"blocking_action_id": "DEPS-NOT-RUN", "reason": "BLOCKED_DEPENDENCY",
         "execution_status": "NOT_EXECUTED", "satisfied": False},
    ]}
    assert_safe(chain._gar_snapshot())
    # A blocked mutation is not covered by this exception.
    chain.deferred_action_repairs["MUTATE-NOT-RUN"] = {
        "action_id": "MUTATE-NOT-RUN", "tool": "run_command",
        "status": "BLOCKED_DEPENDENCY", "executed": False,
        "action": {"tool": "run_command", "operation": "MUTATE",
                   "action_id": "MUTATE-NOT-RUN", "depends_on": ["META-DENIED"]},
    }
    assert chain._gar_snapshot()["pending_operations"]["deferred_action_repairs"]

    # Live L05: a typed FILE_NOT_FOUND read is a completed observation of
    # absence, even though it cannot be counted as a successful file read.
    absent = mock_loop()
    absent.action_ledger = {"MISSING-INDEX": {
        "action": {"tool": "read_file", "action_id": "MISSING-INDEX"},
        "execution_status": "FAILED",
    }}
    absent.action_result_ledger = {"MISSING-INDEX": {
        "executed": False, "result_status": "FAILED",
        "execution_status": "FAILED", "result_reason": "FILE_NOT_FOUND",
        "effective_verification_status": "UNKNOWN",
    }}
    absent.action_loop_state = {"blockers": [{
        "blocking_action_id": "MISSING-INDEX", "reason": "REPLAN_REQUIRED",
        "execution_status": "FAILED", "satisfied": False,
    }]}
    assert_safe(absent._gar_snapshot())

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

    # Completed past reads and an unrelated never-attempted observation may
    # coexist with a safely repairable, never-executed command schema error.
    sibling = mock_loop()
    sibling.pending_command_field_repairs = dict(y.pending_command_field_repairs)
    sibling.deferred_action_repairs = dict(y.deferred_action_repairs)
    sibling.deferred_action_repairs["READ-DEFERRED"] = {
        "action_id": "READ-DEFERRED", "tool": "list_directory",
        "status": "REPAIR_REQUIRED", "executed": False,
        "action": {"tool": "list_directory", "path": "deferred"},
    }
    sibling.action_ledger = {"READ-DONE": {
        "action": {"tool": "list_directory"}, "execution_status": "SUCCEEDED",
    }}
    sibling.action_result_ledger = {"READ-DONE": {
        "result_status": "SUCCEEDED", "execution_status": "SUCCEEDED",
        "effective_verification_status": "PASS", "executed": True,
    }}
    assert_safe(sibling._gar_snapshot())
    # But a deferred mutation must remain hard-blocked.
    sibling.deferred_action_repairs["MUTATE-DEFERRED"] = {
        "action_id": "MUTATE-DEFERRED", "tool": "run_command",
        "status": "BLOCKED_DEPENDENCY", "executed": False,
        "action": {"tool": "run_command", "operation": "MUTATE"},
    }
    assert sibling._gar_snapshot()["pending_operations"]["deferred_action_repairs"]

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
