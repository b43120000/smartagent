"""Git70 real Windows bypass IT: run Git69's real Runtime, match frozen expectations.

The installed-host --live entrypoint checks actual ACL OFF/Turbo OFF/
Auto Approve ON. CI may simulate only policy inputs; it never mocks commands,
process launching, evidence persistence, readback or collect.bat.
No Telegram Host CMD, Planner simulation or network mutation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .goal_evidence_matching import (
    append_match_report, frozen_expected_sha256, match_expected,
    terminal_goal_verdict, ExpectedContractError,
)
from .runtime_evidence_store import read_runtime_action_result
from .runtime_open_fidelity_it import run_open_it
from .tool_result_contract import decode_result

SCHEMA = "SMARTAGENT_GIT70_GOAL_EVIDENCE_IT_V1"
_EXITS = {
    "PS_SUCCESS":0, "PS_THROW":1, "PS_NONTERMINATING":0,
    "CMD_EXIT_7":7, "CMD_BAT_37":37,
    "DIRECT_EXIT_9":9, "DIRECT_EXIT_0":0,
    "DIRECT_GIT_VERSION":0, "DIRECT_GIT_128":128,
    "DIRECT_UTF8":0, "VERIFY_EXPECTED_FAIL":0,
    "DIRECT_TRUNCATED":0,
}


def _assert(field: str, value, operator="eq") -> dict:
    return {"field":field,"operator":operator,"value":value}


def expected_for_case(case_id: str) -> dict:
    """Predeclare intent by fixture identity, never from observed results."""
    if case_id == "DIRECT_MISSING":
        assertions = [
            _assert("status", "FAILED"),
            _assert("executed", False),
            _assert("data.process_started", False),
            _assert("data.process_spawn_attempted", True),
            _assert("data.process_start_phase", "POPEN"),
            _assert("data.process_start_error_class", "FileNotFoundError"),
            _assert("data.process_state", "NOT_STARTED"),
            _assert("data.exit_code", None, "is_null"),
            _assert("data.possible_partial_execution", False),
        ]
    elif case_id == "DIRECT_TIMEOUT":
        assertions = [
            _assert("data.timed_out", True),
            _assert("data.exit_code", None, "is_null"),
            _assert("data.possible_partial_execution", True),
        ]
    elif case_id in _EXITS:
        assertions = [_assert("data.exit_code", _EXITS[case_id])]
        if case_id == "VERIFY_EXPECTED_FAIL":
            assertions += [
                _assert("data.execution_status", "SUCCEEDED"),
                _assert("data.verification_status", "FAIL"),
                _assert("data.failure_origin", "VERIFICATION"),
            ]
        if case_id == "DIRECT_TRUNCATED":
            assertions += [
                _assert("data.stdout_truncated", True),
                _assert("data.output_complete", False),
                _assert("data.stdout_bytes", 10000000, "gt"),
                _assert("data.stdout_raw_sha256", hashlib.sha256(b"Z"*12000000).hexdigest()),
            ]
        if case_id == "PS_NONTERMINATING":
            assertions += [
                _assert("data.stderr_bytes", 0, "gt"),
                _assert("data.stdout_bytes", 0, "gt"),
            ]
        if case_id in {"PS_THROW", "CMD_EXIT_7", "CMD_BAT_37", "DIRECT_EXIT_9", "DIRECT_GIT_128"}:
            assertions.append(_assert("data.failure_origin", "CHILD_PROCESS"))
    else:
        raise ValueError("UNKNOWN_GIT70_TEST_CASE")
    return {"contract_version":1,"aggregation":"ALL","assertions":assertions}


def run_goal_it(workspace: str | Path, *, policy_source: str = "INSTALLED_HOST") -> dict:
    """Execute actual shell/process Runtime actions, then immutable MIN-EV readback."""
    # Every Expected is frozen from a fixed test recipe before the first dispatch.
    names = list(_EXITS) + ["DIRECT_MISSING", "DIRECT_TIMEOUT"]
    expectations = {name: expected_for_case(name) for name in names}
    digests = {name: frozen_expected_sha256(contract)
               for name, contract in expectations.items()}
    base = run_open_it(workspace, policy_source=policy_source)
    root = Path(__file__).resolve().parents[2]
    report = {
        "schema": SCHEMA, "task_id": base["task_id"],
        "request_id": base["request_id"], "task_epoch": base["task_epoch"],
        "policy_source": policy_source, "planner_invoked": False,
        "process_backend_mocked": False, "runtime_dispatch_shared": True,
        "source_git69_report_status": base["status"], "cases": [],
        "negative_probes": {}, "status": "RUNNING",
    }
    for case in base["cases"]:
        case_id = case["case_id"]
        if case_id not in expectations or not case.get("evidence_ref"):
            report["cases"].append({"case_id":case_id,"status":"FAIL",
                                    "reason":"SOURCE_EVIDENCE_MISSING"})
            continue
        raw = read_runtime_action_result(
            root, task_id=base["task_id"], request_id=base["request_id"],
            task_epoch=base["task_epoch"], action_id=case["action_id"],
            evidence_ref=case["evidence_ref"],
        )
        receipt = decode_result(raw)
        match = match_expected(
            expectations[case_id], receipt, evidence_ref=case["evidence_ref"],
            evidence_store_status="PERSISTED_AND_READBACK_VERIFIED",
            frozen_sha256=digests[case_id], action_id=case["action_id"],
        )
        append_match_report(root, base["task_id"], base["request_id"], match)
        # The expected Timeout is an assertion PASS, but NOT replay permission.
        safety_ok = (match["safety_decision"]=="PAUSE_AND_RECONCILE"
                     if case_id=="DIRECT_TIMEOUT" else
                     match["safety_decision"]=="CONTINUE_OR_REPLAN")
        passed = (case["status"]=="PASS" and match["match_state"]=="MATCH"
                  and safety_ok and match["final_verdict"] is None)
        report["cases"].append({
            "case_id":case_id, "action_id":case["action_id"],
            "evidence_ref":case["evidence_ref"],
            "frozen_expected_sha256":digests[case_id],
            "match_state":match["match_state"],
            "assertion_verdict":match["action_assertion_verdict"],
            "safety_decision":match["safety_decision"],
            "safety_reason":match["safety_reason"],
            "row_count":len(match["assertions"]),
            "status":"PASS" if passed else "FAIL",
        })
    # Negative controls are intentionally *not* added to the actual task
    # evidence ledger: they must not masquerade as live observed actions.
    first = next((r for r in base["cases"] if r.get("case_id")=="DIRECT_EXIT_0"), None)
    if first and first.get("evidence_ref"):
        raw = read_runtime_action_result(
            root,task_id=base["task_id"],request_id=base["request_id"],
            task_epoch=base["task_epoch"], action_id=first["action_id"],
            evidence_ref=first["evidence_ref"],
        )
        receipt = decode_result(raw)
        negative = {
            "contract_version":1, "aggregation":"ALL",
            "assertions":[_assert("data.exit_code",9999)],
        }
        mismatch = match_expected(
            negative,receipt,evidence_ref=first["evidence_ref"],
            evidence_store_status="PERSISTED_AND_READBACK_VERIFIED",
        )
        missing = match_expected(
            negative,{"status":"SUCCEEDED","data":{}},
            evidence_ref=first["evidence_ref"],
            evidence_store_status="READBACK_UNAVAILABLE",
        )
        tamper_rejected = False
        try:
            match_expected(negative,receipt,evidence_ref=first["evidence_ref"],
                           evidence_store_status="PERSISTED_AND_READBACK_VERIFIED",
                           frozen_sha256="0"*64)
        except ExpectedContractError:
            tamper_rejected = True
        report["negative_probes"] = {
            "mismatch_causes_replan": mismatch["match_state"]=="MISMATCH"
                 and mismatch["next_action"]=="REPLAN",
            "missing_evidence_requires_recollection": missing["match_state"]=="RECOLLECT_EVIDENCE"
                 and missing["next_action"]=="RECOLLECT_EVIDENCE"
                 and missing["recovery"]["replay_original_action"] is False,
            "post_dispatch_contract_tamper_rejected":tamper_rejected,
            "unproven_infeasibility_has_no_final_verdict":terminal_goal_verdict(
                goal_satisfied=False,all_required_evidence_verified=False) is None,
            "terminal_pass_requires_evidence":terminal_goal_verdict(
                goal_satisfied=True,all_required_evidence_verified=True)=="PASS",
        }
    report["passed"] = sum(c["status"]=="PASS" for c in report["cases"])
    report["failed"] = sum(c["status"]=="FAIL" for c in report["cases"])
    report["status"] = ("PASS" if base["status"]=="PASS"
                        and len(report["cases"])==14 and report["passed"]==14
                        and len(report["negative_probes"])==5
                        and all(report["negative_probes"].values()) else "FAIL")
    path = root/"localdata"/"runtime"/"goal_evidence_it"/(base["task_id"]+".json")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return report


def main(argv=None) -> int:
    ap=argparse.ArgumentParser(description="Git70 real Runtime Expected/Observed bypass IT")
    ap.add_argument("--workspace",required=True)
    ap.add_argument("--live",action="store_true")
    args=ap.parse_args(argv)
    if not args.live:
        print(json.dumps({"status":"BLOCKED","reason":"EXPLICIT_LIVE_OPT_IN_REQUIRED"}))
        return 2
    try:
        r=run_goal_it(args.workspace)
    except (OSError,ValueError,RuntimeError) as exc:
        print(json.dumps({"schema":SCHEMA,"status":"BLOCKED","error_class":type(exc).__name__}))
        return 2
    print(json.dumps({"schema":SCHEMA,"status":r["status"],"task_id":r["task_id"],
                      "request_id":r["request_id"],"passed":r["passed"],
                      "failed":r["failed"],"negative_probes":r["negative_probes"]},
                     ensure_ascii=False))
    return 0 if r["status"]=="PASS" else 1

if __name__=="__main__":
    raise SystemExit(main())
