"""Git71 Timeout Recovery UT: every safety boundary is independently verified.

Only declarative Planner decisions are injected here. Windows sidecar IT
launches real OS processes via production Executor/ToolResult path.
"""
import pytest

from agent_core.goal_evidence_matching import match_expected
from agent_core.timeout_reconciliation import (
    TimeoutReconciliationError, validate_reconciliation,
    reconciliation_digest, begin_timeout_reconciliation,
    check_reconciliation_observation, read_reconciliation_resolution,
)

A_REF="MIN-EV-"+"a"*24
B_REF="MIN-EV-"+"b"*24
C_REF="MIN-EV-"+"c"*24
PATH=r"E:\AIspace\git71_fixture\result.txt"


def action():
    return {
        "tool":"run_command", "action_id":"MIN-G71-TEST-SOURCE",
        "operation":"TEST", "working_directory":r"E:\AIspace\git71_fixture",
        "reconciliation":{"version":1,"checks":[{
            "tool":"observe_fact","path":PATH,"expected_exists":False}]},
        "expected":{"contract_version":1,"aggregation":"ALL",
                    "assertions":[{"field":"data.timed_out","operator":"eq","value":True}]},
    }


def process():
    return {"schema":"SMARTAGENT_GIT71_PROCESS_TERMINATION_V1",
            "pid":123, "owned_handle_exit_verified":True,
            "owned_process_state":"EXITED",
            "final_identity_source":"WIN32_WAIT_FOR_SINGLE_OBJECT",
            "tree_termination_source":"WIN32_TASKKILL_TREE",
            "tree_kill_exit_code":0, "tree_termination_verdict":"VERIFIED",
            "original_command_success_proven":False,
            "side_effect_scope":"NOT_EVALUATED","replay_original_action":False}


def receipt():
    return {"status":"UNKNOWN","executed":True,
            "data":{"pid":123,"timed_out":True,"process_started":True,
                    "possible_partial_execution":True,"process_state":"UNKNOWN",
                    "exit_code":None,"timeout_termination":process()}}


def observation():
    return {"tool":"observe_fact","action_id":"MIN-G71-TEST-OBS",
            "path":PATH,
            "expected":{"contract_version":1,"aggregation":"ALL",
                        "assertions":[{"field":"data.observations.0.value",
                                       "operator":"eq","value":False}]}}


def observed():
    return {"status":"SUCCEEDED","executed":True,
            "resolved_path":PATH,
            "data":{"observation_status":"CONFIRMED",
                    "observations":[{"field":"file.exists","value":False,
                       "observation_status":"CONFIRMED","freshness":"CURRENT",
                       "source":"filesystem.stat","scope":PATH}]}}


def match(a,r,ref):
    return match_expected(a["expected"],r,evidence_ref=ref,
        evidence_store_status="PERSISTED_AND_READBACK_VERIFIED",
        action_id=a["action_id"])


def staged(a=None,r=None):
    a=a or action()
    r=r or receipt()
    return begin_timeout_reconciliation(a,r,match(a,r,A_REF),A_REF)


def test_proven_os_finality_still_requires_observation():
    st=staged()
    assert st["state"]=="OBSERVATION_REQUIRED"
    assert st["process_finality_verified"] is True
    assert st["side_effect_scope"]=="DECLARED_PATHS_ONLY"
    assert st["replay_original_action"] is False
    assert st["completed_check_paths"]==[]


def test_missing_proof_never_claims_a_safe_continuation():
    r=receipt()
    for replacement in ({},{"owned_handle_exit_verified":False},
                        {"tree_termination_verdict":"UNVERIFIED"},
                        {"tree_kill_exit_code":1},
                        {"final_identity_source":"PID_ONLY"}):
        r["data"]["timeout_termination"]={**process(),**replacement}
        if not replacement:
            r["data"]["timeout_termination"]={}
        assert staged(r=r)["state"]=="BLOCKED"


def test_expected_timeout_required_before_dispatch():
    a=action()
    a["expected"]["assertions"][0]["field"]="data.process_started"
    assert staged(a=a)["reason"]=="EXPECTED_TIMEOUT_NOT_PREDECLARED"


@pytest.mark.parametrize("mutation",[
    lambda x: x.update(version=2),
    lambda x: x.update(checks=[]),
    lambda x: x["checks"][0].update(tool="run_command"),
    lambda x: x["checks"][0].update(expected_exists=0),
    lambda x: x["checks"][0].update(path=r"C:\outside\file.txt"),
    lambda x: x["checks"][0].update(path=r"E:\AIspace\git71_fixture\..\untrusted"),
])
def test_reconciliation_contract_is_bounded_and_path_scoped(mutation):
    a=action()
    mutation(a["reconciliation"])
    with pytest.raises(TimeoutReconciliationError):
        validate_reconciliation(a)


def test_observation_must_match_exact_declared_path_and_value():
    a=observation();r=observed()
    good=check_reconciliation_observation(staged(),a,r,match(a,r,B_REF),B_REF)
    assert good["state"]=="RECONCILED"
    assert good["source_evidence_ref"]==A_REF
    assert good["verification_evidence_ref"]==B_REF
    assert not good["replay_original_action"]
    for changed in [
        {"path":r"E:\AIspace\git71_fixture\other.txt"},
        {"expected":{"contract_version":1,"aggregation":"ALL","assertions":[{
            "field":"data.observations.0.value","operator":"eq","value":True}]}},
    ]:
        different={**a,**changed}
        mr=match(different,r,B_REF)
        result=check_reconciliation_observation(staged(),different,r,mr,B_REF)
        assert result["state"]=="BLOCKED"
    altered={**r,"data":{**r["data"],"observations":[{
            **r["data"]["observations"][0],"value":True}]}}
    result=check_reconciliation_observation(staged(),a,altered,match(a,altered,B_REF),B_REF)
    assert result["state"]=="BLOCKED"


def test_finish_only_resolves_source_from_real_linked_replayed_history():
    a=action();b=observation();ar=receipt();br=observed()
    stage=staged(a,ar)
    digest=reconciliation_digest(a)
    event={"kind":"TIMEOUT_RECONCILIATION_COMPLETED",
           "source_action_id":a["action_id"],"source_evidence_ref":A_REF,
           "verification_action_id":b["action_id"],"verification_evidence_ref":B_REF,
           "witness_action_ids":[b["action_id"]],
           "reconciliation_sha256":digest,"state":"RECONCILED"}
    histories=[
        {"action":a,"receipt":ar,"evidence_ref":A_REF,
         "expected_match":match(a,ar,A_REF),"sequence":1},
        {"action":b,"receipt":br,"evidence_ref":B_REF,
         "expected_match":match(b,br,B_REF),"sequence":2},
    ]
    assert read_reconciliation_resolution([event],histories)=={A_REF}
    for falsification in [
        {**event,"source_evidence_ref":C_REF},
        {**event,"reconciliation_sha256":"0"*64},
        {**event,"witness_action_ids":[]},
        {**event,"verification_evidence_ref":C_REF},
    ]:
        assert read_reconciliation_resolution([falsification],histories)==set()
    assert read_reconciliation_resolution([],histories)==set()
    tampered=[{**histories[0],"receipt":{**ar,"data":{**ar["data"],
                 "timeout_termination":{**process(),"tree_kill_exit_code":-1}}}},
             histories[1]]
    assert read_reconciliation_resolution([event],tampered)==set()
    assert read_reconciliation_resolution([event],
        [{**histories[0],"sequence":3},histories[1]])==set()
