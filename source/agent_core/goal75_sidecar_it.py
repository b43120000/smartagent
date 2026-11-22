"""Git75 real-process 10-stage sidecar IT.

Runs only in a temporary disposable directory. No SmartAgent permissions,
web transport, credentials or production workspace are used. This covers
Stage Planner contract -> real Executor -> Stage evidence -> local Recovery ->
Commit Freeze -> Big Blackboard readback -> independent Goal finality.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from .goal75_blackboard import read_events
from .goal75_stage_ledger import GoalStageLedger
from .goal75_recovery import choose_stage_recovery, goal_finish_readiness


def _ref(value: str) -> str:
    return "MIN-EV-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _run(workdir: Path, index: int, *, fail: bool = False) -> dict:
    # Each stage is a real fresh child process, never a mocked ToolResult.
    script = (
        "import pathlib,sys\n"
        "target=pathlib.Path(sys.argv[1]); i=int(sys.argv[2]); "
        "fail=sys.argv[3]=='1'\n"
        "if not fail: target.write_text('DONE:'+str(i),encoding='utf-8')\n"
        "sys.exit(17 if fail else 0)\n"
    )
    file = workdir / f"S{index:02d}.txt"
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(file), str(index),
         "1" if fail else "0"],
        cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, timeout=30,
    )
    return {
        "tool": "run_command",
        "status": "SUCCEEDED" if result.returncode == 0 else "FAILED",
        "executed": True,
        "data": {
            "stage": index, "exit_code": result.returncode,
            "process_state": "EXITED",
            "artifact_exists": file.exists(),
        },
    }


def run_sidecar(output: str | Path | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="git75-live-it-") as scratch:
        base = Path(scratch)
        artifacts = base / "artifacts"
        artifacts.mkdir()
        ledger = GoalStageLedger(
            base, task_id="TASK-G75-REALPROCESS-IT",
            request_id="RR-G75-REALPROCESS-IT", task_epoch="EPOCH-G75-IT",
            goal="Execute, verify and freeze ten independent stages",
        )
        plan = [
            {
                "stage_id": f"S{i:02d}",
                "description": f"Real process creates a verified stage {i} artifact",
                "depends_on": [f"S{i-1:02d}"] if i > 1 else [],
                "acceptance": [
                    {"field": "data.exit_code", "operator": "eq", "expected": 0},
                    {"field": "data.artifact_exists", "operator": "eq", "expected": True},
                    {"field": "data.stage", "operator": "eq", "expected": i},
                ],
            }
            for i in range(1, 11)
        ]
        ledger.create(plan)
        frozen = {}
        failures = []
        attempts = {f"S{i:02d}": 0 for i in range(1, 11)}
        for i in range(1, 11):
            sid = f"S{i:02d}"
            if ledger.current()["stage_id"] != sid:
                raise AssertionError("STAGE_ORDER_CHANGED")
            if i == 3:
                # Actual failing child process; it does NOT mutate an artifact.
                failed = _run(artifacts, i, fail=True)
                attempts[sid] += 1
                if failed["status"] != "FAILED" or failed["data"]["artifact_exists"]:
                    raise AssertionError("INJECTED_FAILURE_NOT_REPRODUCED")
                ledger.record_action(sid, action_id="ACT-S03-FAILED",
                    evidence_ref=_ref("S03-BAD"), receipt=failed)
                recovery = choose_stage_recovery(
                    reason_code="FAILED_PROCESS", stage_id=sid, attempt=1)
                if recovery["status"] != "RECOVERING":
                    raise AssertionError("RECOVERY_INCORRECT")
                ledger.recover(sid, reason_code="FAILED_PROCESS",
                               strategy_id=recovery["strategy_id"])
                failures.append({"stage_id": sid,
                                 "reason_code": "FAILED_PROCESS",
                                 "strategy_id": recovery["strategy_id"]})
            successful = _run(artifacts, i)
            attempts[sid] += 1
            ref = _ref(sid + "-PASS")
            if successful["status"] != "SUCCEEDED":
                raise AssertionError("PROCESS_EXECUTION_FAILED")
            if (artifacts / f"S{i:02d}.txt").read_text(encoding="utf-8") != f"DONE:{i}":
                raise AssertionError("ARTIFACT_READBACK_FAILED")
            ledger.record_action(sid, action_id=f"ACT-{sid}-PASS",
                                 evidence_ref=ref, receipt=successful)
            doc = ledger.commit(sid, evidence_refs=[ref])
            row = next(x for x in doc["stages"] if x["stage_id"] == sid)
            frozen[sid] = row["commit"]["commit_sha256"]
            for older in doc["stages"]:
                if older["stage_id"] in frozen and (
                    older["commit"]["commit_sha256"] != frozen[older["stage_id"]]
                ):
                    raise AssertionError("PREVIOUS_STAGE_MUTATED")
        state = ledger.read()
        events = read_events(base, "TASK-G75-REALPROCESS-IT")
        stage_commits = [x for x in events if x["event"] == "STAGE_VERIFIED_COMMITTED"]
        if len(stage_commits) != 10:
            raise AssertionError("STAGE_COMMIT_EVENT_COUNT_INVALID")
        if len({x["stage_id"] for x in stage_commits}) != 10:
            raise AssertionError("STAGE_COMMIT_DUPLICATED")
        independent_goal = all(
            (artifacts / f"S{i:02d}.txt").read_text(encoding="utf-8") == f"DONE:{i}"
            for i in range(1, 11)
        )
        final = goal_finish_readiness(
            state, independent_goal_verified=independent_goal)
        if final["status"] != "PASS":
            raise AssertionError("FINAL_GOAL_POSTCONDITION_UNVERIFIED")
        report = {
            "schema": "SMARTAGENT_GOAL75_SIDECAR_IT_V1",
            "status": "PASS",
            "real_child_process_count": sum(attempts.values()),
            "stage_count": 10, "committed_stage_count": len(frozen),
            "frozen_stage_reexecution_count": 0,
            "injected_failure_count": len(failures),
            "recovery": failures, "attempt_counts": attempts,
            "big_blackboard_event_count": len(events),
            "big_blackboard_modules": sorted({x["module"] for x in events}),
            "goal_finish": final,
            "terminal_claim_without_evidence": False,
        }
    if output is not None:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="git75-ci-evidence/goal75-sidecar-it.json")
    arguments = parser.parse_args()
    report = run_sidecar(arguments.output)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
