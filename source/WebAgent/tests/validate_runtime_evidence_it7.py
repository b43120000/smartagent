"""IT7 IT06 deterministic regressions: invalid Predicate and recovery boundary.

PYTHONPATH=source python source/WebAgent/tests/validate_runtime_evidence_it7.py
No browser or Telegram is started; actual Live E2E remains a separate gate.
"""
from pathlib import Path

from agent_core.context_acquisition import context_interrupt_allowed
from agent_core.smartagent_protocol import validate_tool_envelope
from WebAgent.protocol_loop import WebAgentProtocolLoop, extract_authorized_paths


def _loop() -> WebAgentProtocolLoop:
    # The checks exercise the production admission-evidence helpers without
    # launching a real worker, browser, or writing a Progress ledger.
    loop = object.__new__(WebAgentProtocolLoop)
    loop.workspace = Path.cwd()
    loop.run_id = "IT7-REQUEST"
    loop.task_epoch = "IT7-EPOCH"
    loop.authorized_paths = [r"C:\fixture\IT06\present.txt"]
    loop.action_ledger = {}
    loop.action_result_ledger = {}
    loop.continuation_source = None
    loop.pending_result_recovery = {}
    loop._protocol_spec_rejection = {}
    return loop


def test_invalid_predicate_is_objective_preexecution_blocker() -> None:
    loop = _loop()
    original = {
        "tool": "observe_fact", "action_id": "IT06-OBSERVE-01",
        "path": r"C:\fixture\IT06\present.txt",
        "predicate": {"field": "file.exists", "operator": "contains", "expected": "yes"},
        "condition_id": "IT06-SUCCESS",
    }
    valid, diagnostic = validate_tool_envelope(original)
    assert valid is False and diagnostic["reason"] == "evidence_file_exists_boolean_required", diagnostic
    loop._record_preexecution_predicate_rejection(
        [{"tool": "report_progress"}, original, {"tool": "turn_commit"}],
        [diagnostic],
    )
    caps = loop._context_capabilities()
    assert loop._runtime_evidence_refs() == {"REQUEST_ACCEPTED", "RUNTIME_STATUS", "PROTOCOL_SPEC_REJECTION"}
    assert caps["objective_blockers"] == [{
        "action_id": "IT06-OBSERVE-01", "reason": "PREDICATE_SPEC_INVALID",
        "executed": False, "evidence_ref": "PROTOCOL_SPEC_REJECTION",
    }], caps
    assert context_interrupt_allowed(
        {"kind": "BLOCKED", "blocking_action_id": "IT06-OBSERVE-01"},
        caps,
    )
    assert not context_interrupt_allowed(
        {"kind": "BLOCKED", "blocking_action_id": "IT06-FOREIGN"},
        caps,
    )
    # Never turn a protocol rejection into an observed fact or an execution.
    assert loop.action_result_ledger == {}
    assert loop.action_ledger == {}
    assert loop._safe_protocol_spec_rejection()["executed"] is False
    # A later real Tool result or a new task epoch invalidates this exception.
    loop.action_result_ledger["OTHER"] = {"executed": True}
    assert not loop._safe_protocol_spec_rejection()
    assert "PROTOCOL_SPEC_REJECTION" not in loop._runtime_evidence_refs()
    loop.action_result_ledger.clear()
    loop.task_epoch = "OTHER-EPOCH"
    assert not loop._safe_protocol_spec_rejection()


def test_rejection_provenance_is_not_model_writable() -> None:
    loop = _loop()
    action = {"tool": "observe_fact", "action_id": "FAKE",
              "path": r"C:\fixture\IT06\present.txt",
              "predicate": {"field": "file.exists", "operator": "equals", "expected": True},
              "condition_id": "C"}
    loop._record_preexecution_predicate_rejection(
        [{"tool": "report_progress"}, action, {"tool": "turn_commit"}],
        [{"reason": "evidence_file_exists_boolean_required"}],
    )
    assert not loop._safe_protocol_spec_rejection()
    assert not loop._context_capabilities()["objective_blockers"]
    # Ambiguous multi-action batches cannot create a special terminal blocker.
    invalid = {**action,
               "predicate": {"field": "file.exists", "operator": "contains", "expected": "yes"}}
    loop._record_preexecution_predicate_rejection(
        [invalid, {**action, "action_id": "OTHER"}],
        [{"reason": "evidence_file_exists_boolean_required"}],
    )
    assert not loop._safe_protocol_spec_rejection()


def test_it06_path_and_ads_boundaries() -> None:
    f = r"C:\fixture\IT06\present.txt"
    request = (f + ' 驗證以下我指定的原始 Predicate：'
               'field=file.exists、operator=contains、expected="yes"。')
    assert extract_authorized_paths(request) == [f]
    quoted = '"' + f + ' 驗證以下我指定的原始 Predicate"。'
    assert extract_authorized_paths(quoted) == [f + ' 驗證以下我指定的原始 Predicate']
    ads = f + ":secret"
    assert extract_authorized_paths("讀取 " + ads) == [ads]


if __name__ == "__main__":
    test_invalid_predicate_is_objective_preexecution_blocker()
    test_rejection_provenance_is_not_model_writable()
    test_it06_path_and_ads_boundaries()
    print("RUNTIME_EVIDENCE_IT7_IT06_REGRESSION_PASS")
