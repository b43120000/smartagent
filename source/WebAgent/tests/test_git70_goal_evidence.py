"""Git70 universal Expected/Observed Unit Tests: neutral, bounded, immutable."""
import hashlib
import json

import pytest

from agent_core.goal_evidence_matching import (
    ExpectedContractError, append_match_report, frozen_expected_sha256,
    match_expected, safety_decision, terminal_goal_verdict, validate_expected,
    negative_assertion_goal_evidence_eligible,
)

REF = "MIN-EV-" + "a"*24
STORE = "PERSISTED_AND_READBACK_VERIFIED"


def expected(field="data.exit_code", value=0, operator="eq"):
    return {"contract_version":1,"aggregation":"ALL",
            "assertions":[{"field":field,"operator":operator,"value":value}]}


def match(spec, receipt, store=STORE):
    return match_expected(spec,receipt,evidence_ref=REF,evidence_store_status=store,
                          frozen_sha256=frozen_expected_sha256(spec),action_id="ACT-1")


@pytest.mark.parametrize("observed,expectation,verdict,next_action", [
    (0,0,"MATCH","PROCEED"),
    (7,7,"MATCH","PROCEED"),
    (7,0,"MISMATCH","REPLAN"),
    (None,None,"MATCH","PROCEED"),
])
def test_neutral_typed_exit_code(observed,expectation,verdict,next_action):
    receipt={"status":"SUCCEEDED" if observed==0 else "FAILED",
             "executed":observed is not None,
             "data":{"exit_code":observed,"process_state":"NOT_STARTED" if observed is None else "EXITED",
                     "possible_partial_execution":False}}
    spec=expected(value=expectation,operator="is_null" if expectation is None else "eq")
    result=match(spec,receipt)
    assert result["match_state"]==verdict
    assert result["next_action"]==next_action
    assert result["final_verdict"] is None


def test_missing_observed_field_is_recollect_not_false_or_null_match():
    receipt={"status":"SUCCEEDED","data":{}}
    for value in (False,None,0):
        spec=expected("data.process_started",value)
        result=match(spec,receipt)
        assert result["match_state"]=="RECOLLECT_EVIDENCE"
        assert result["next_action"]=="RECOLLECT_EVIDENCE"
        assert result["unobserved_fields"]==["data.process_started"]
        assert not result["recovery"]["replay_original_action"]
        assert result["final_verdict"] is None


def test_broken_evidence_store_never_matches_known_preview():
    receipt={"status":"SUCCEEDED","data":{"exit_code":0}}
    r=match(expected(),receipt,store="PERSIST_FAILED")
    assert r["match_state"]=="RECOLLECT_EVIDENCE"
    assert r["observed_integrity"]=="UNVERIFIED"
    assert r["action_assertion_verdict"] is None


def test_process_start_negative_is_valid_assertion_but_unexecuted():
    receipt={"status":"FAILED","executed":False,
             "data":{"process_started":False,"process_spawn_attempted":True,
                     "process_start_phase":"POPEN",
                     "process_start_error_class":"FileNotFoundError",
                     "process_state":"NOT_STARTED","exit_code":None,
                     "possible_partial_execution":False}}
    spec={"contract_version":1,"aggregation":"ALL",
          "assertions":[{"field":field,"operator":"eq","value":value}
                        for field,value in [
                          ("executed",False),("data.process_started",False),
                          ("data.process_spawn_attempted",True),
                          ("data.process_start_error_class","FileNotFoundError"),
                          ("data.exit_code",None)]]}
    r=match(spec,receipt)
    assert r["match_state"]=="MATCH"
    assert r["action_assertion_verdict"]=="PASS"
    assert r["safety_decision"]=="CONTINUE_OR_REPLAN"
    assert receipt["status"]=="FAILED" and receipt["executed"] is False


def test_timeout_assertion_match_does_not_grant_replay():
    receipt={"status":"UNKNOWN","executed":True,
             "data":{"timed_out":True,"exit_code":None,"process_state":"UNKNOWN",
                     "possible_partial_execution":True}}
    r=match(expected("data.timed_out",True),receipt)
    assert r["match_state"]=="MATCH"
    assert r["safety_decision"]=="PAUSE_AND_RECONCILE"
    assert r["next_action"]=="RECONCILE_WITHOUT_REPLAY"
    assert r["recovery"]["replay_original_action"] is False


def test_aggregation_all_any_and_missing():
    receipt={"status":"FAILED","data":{"exit_code":7,"process_state":"EXITED"}}
    s={"contract_version":1,"aggregation":"ANY",
       "assertions":[{"field":"data.exit_code","operator":"eq","value":0},
                     {"field":"data.exit_code","operator":"eq","value":7}]}
    assert match(s,receipt)["match_state"]=="MATCH"
    s["aggregation"]="ALL"
    assert match(s,receipt)["match_state"]=="MISMATCH"
    s["aggregation"]="ANY"
    s["assertions"][1]["field"]="data.not_observed"
    assert match(s,receipt)["match_state"]=="RECOLLECT_EVIDENCE"


@pytest.mark.parametrize("bad",[
    {"contract_version":2,"aggregation":"ALL","assertions":[]},
    {"contract_version":1,"aggregation":"ALL","assertions":[]},
    expected("data.stdout", "leak"),
    expected("data.stderr","leak"),
    expected("data.command_error","secret"),
    expected("data.exit_code", 0, "eval"),
    expected("data.exit_code","1", "gt"),
    {"contract_version":1,"aggregation":"ALL","assertions":[{"field":"data.exit_code","operator":"eq","value":0,"extra":"no"}]},
])
def test_invalid_expected_is_rejected_before_dispatch(bad):
    with pytest.raises(ExpectedContractError):
        validate_expected(bad)


def test_frozen_contract_digest_cannot_be_changed_after_observation():
    spec=expected()
    digest=frozen_expected_sha256(spec)
    receipt={"status":"SUCCEEDED","data":{"exit_code":0}}
    assert match_expected(spec,receipt,evidence_ref=REF,
                          evidence_store_status=STORE,
                          frozen_sha256=digest)["match_state"]=="MATCH"
    altered=expected(value=7)
    with pytest.raises(ExpectedContractError,match="FROZEN"):
        match_expected(altered,receipt,evidence_ref=REF,
                       evidence_store_status=STORE,frozen_sha256=digest)


def test_terminal_only_pass_or_proven_fail():
    assert terminal_goal_verdict(goal_satisfied=False,
                                 all_required_evidence_verified=False) is None
    assert terminal_goal_verdict(goal_satisfied=True,
                                 all_required_evidence_verified=False) is None
    assert terminal_goal_verdict(goal_satisfied=True,
                                 all_required_evidence_verified=True)=="PASS"
    assert terminal_goal_verdict(goal_satisfied=False,
                                 all_required_evidence_verified=False,
                                 infeasibility_proven=True,
                                 infeasibility_evidence_refs=()) is None
    assert terminal_goal_verdict(goal_satisfied=False,
                                 all_required_evidence_verified=True,
                                 infeasibility_proven=True,
                                 infeasibility_evidence_refs=(REF,))=="FAIL"


def test_plain_jsonl_report_bound_to_task_and_request(tmp_path):
    r=match(expected(),{"status":"SUCCEEDED","data":{"exit_code":0}})
    task="TASK-70TEST-AAAA0000"
    request="RR-70TEST-BBBB0000"
    p=append_match_report(tmp_path,task,request,r)
    rows=[json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert len(rows)==1
    assert rows[0]["task_id"]==task
    assert rows[0]["request_id"]==request
    assert rows[0]["frozen_expected_sha256"]==frozen_expected_sha256(expected())
    assert rows[0]["final_verdict"] is None


def test_inaccessible_observation_is_not_fabricated_failure():
    receipt={"status":"FAILED","data":{"process_state":"NOT_STARTED"}}
    r=match(expected("data.exit_code",None,"is_null"),receipt)
    assert r["match_state"]=="RECOLLECT_EVIDENCE"
    assert r["final_verdict"] is None
    assert not r["recovery"]["replay_original_action"]


def test_only_predeclared_nonmutating_negative_test_is_goal_evidence():
    receipt={"status":"FAILED","executed":False,"data":{
        "process_spawn_attempted":True,"process_start_phase":"POPEN",
        "process_start_error_class":"FileNotFoundError",
        "process_started":False,"process_state":"NOT_STARTED",
        "possible_partial_execution":False,"exit_code":None,
    }}
    contract=expected("data.process_start_error_class","FileNotFoundError")
    report=match(contract,receipt)
    action={"tool":"run_command","operation":"TEST"}
    assert negative_assertion_goal_evidence_eligible(action,receipt,report)
    assert not negative_assertion_goal_evidence_eligible(
        {**action,"operation":"MUTATE"},receipt,report)
    assert not negative_assertion_goal_evidence_eligible(
        action,{**receipt,"data":{**receipt["data"],"possible_partial_execution":True}},report)
    assert not negative_assertion_goal_evidence_eligible(
        action,receipt,{**report,"observed_integrity":"UNVERIFIED"})
    assert not negative_assertion_goal_evidence_eligible(
        action,receipt,{**report,"match_state":"MISMATCH"})
