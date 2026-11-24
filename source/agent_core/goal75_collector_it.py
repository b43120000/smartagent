"""Windows-only end-to-end collect.bat ZIP roundtrip for Git75 traces.

Creates a scoped, non-production diagnostic task in the CI checkout;
runs the ACTUAL shipped Windows collector; validates its ZIP payload.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

from .goal75_blackboard import emit_transition, read_events
from .goal75_stage_ledger import GoalStageLedger


def run_collector_it(root: str | Path = ".") -> dict:
    if os.name != "nt":
        return {"schema": "SMARTAGENT_GOAL75_COLLECTOR_IT_V1",
                "status": "SKIPPED_NONWINDOWS"}
    base = Path(root).resolve()
    task, request, epoch = (
        "TASK-G75-CI-COLLECT", "RR-G75-CI-COLLECT", "EPOCH-G75-CI"
    )
    target = base / "localdata" / "runtime" / "goal76-ci-clone-conflict"
    target.mkdir(parents=True, exist_ok=True)
    goal = ("Clone 目標：" + str(target) + "\n"
            "先確認 Clone 目標不存在，不得刪除既有資料。")
    stage = GoalStageLedger(
        base, task_id=task, request_id=request, task_epoch=epoch, goal=goal)
    stage.create([{
        "stage_id": "S01", "description": "Record verified diagnostic fact",
        "depends_on": [], "acceptance": [
            {"field": "data.exit_code", "operator": "eq", "expected": 0}
        ],
    }, {
        "stage_id": "S02", "description": "Fresh clone precondition",
        "depends_on": ["S01"], "acceptance": [
            {"field": "data.observations.0.value", "operator": "eq", "expected": False,
             "binding": {"kind": "file.exists", "subject": str(target), "before_mutation": True}}
        ],
    }])
    receipt = {
        "tool": "run_command", "status": "SUCCEEDED", "executed": True,
        "data": {"exit_code": 0, "process_state": "EXITED"},
    }
    evidence = "MIN-EV-" + "a" * 24
    stage.record_action(
        "S01", action_id="ACT-G75-COLLECT", evidence_ref=evidence,
        receipt=receipt)
    stage.commit("S01", evidence_refs=[evidence])
    from .evidence_foundation import probe_file_exists
    from .runtime_evidence_store import persist_runtime_evidence
    from .tool_result_contract import attach_runtime_result, decode_result
    conflict_action = "ACT-G76-COLLECT-CONFLICT"
    conflict_ref = "MIN-EV-" + "b" * 24
    fact = probe_file_exists(target, raw_ref=conflict_action)
    raw = attach_runtime_result("{}", "SUCCEEDED", "FACT_OBSERVED",
        tool="observe_fact", executed=True, requested_path=str(target),
        resolved_path=str(target), task_id=task, request_id=request, action_id=conflict_action,
        data={"observations": [fact], "execution_finality": "COMPLETED",
              "observation_status": "CONFIRMED", "terminal_eligible": True,
              "verification_status": "UNVERIFIED"})
    observed = decode_result(raw)
    pointer = persist_runtime_evidence(base, task_id=task, request_id=request,
        task_epoch=epoch, action_id=conflict_action, evidence_ref=conflict_ref,
        raw_result=raw, receipt=observed)
    stage.record_action("S02", action_id=conflict_action, evidence_ref=conflict_ref,
        receipt=observed, expected_match=False,
        evidence_store_verified=pointer["store_status"] == "PERSISTED_AND_READBACK_VERIFIED")
    assert stage.record_terminal_conflict(goal)["evidence_ref"] == conflict_ref
    emit_transition(
        base, task_id=task, request_id=request, task_epoch=epoch,
        module="COLLECTOR_SIDECAR", event="INTEGRATION_SOURCE_PERSISTED",
        before={"runtime_state": "PROCESSING"},
        after={"runtime_state": "COMPLETED", "stage_status": "VERIFIED_COMMITTED"},
        stage_id="S01", evidence_refs=[evidence],
    )
    events = read_events(base, task)
    if not any(e["module"] == "COLLECTOR_SIDECAR" for e in events):
        raise AssertionError("SIDECAR_EVENT_NOT_PERSISTED")
    output = subprocess.run(
        ["cmd.exe", "/d", "/c", "collect.bat", task, request],
        cwd=base, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    if output.returncode != 0:
        raise AssertionError(
            "COLLECT_BAT_FAILED:" + output.stdout[-2200:] +
            ":" + output.stderr[-2200:]
        )
    match = re.search(r"(?mi)^\[COLLECT\] ZIP: ([^\r\n]+)", output.stdout)
    if not match:
        raise AssertionError("COLLECT_ZIP_PATH_NOT_EXPORTED:" + output.stdout[-2000:])
    filename = Path(match.group(1).strip())
    if not filename.is_file():
        raise AssertionError("COLLECT_ZIP_MISSING")
    try:
        with zipfile.ZipFile(filename) as bundle:
            names = bundle.namelist()
            expected = {
                "goal75_big_blackboard_events.json",
                "goal75_stage_progress.json",
                "goal75_flow_summary.json",
                "goal76_stage_binding_audit.json",
                "manifest.json",
            }
            missing = expected - set(names)
            if missing:
                raise AssertionError("COLLECT_G75_FILES_MISSING:" + ",".join(sorted(missing)))
            data = {key: json.loads(bundle.read(key).decode("utf-8-sig"))
                    for key in expected}
        audit = data["goal76_stage_binding_audit.json"]
        assert audit["task_id"] == task and audit["request_id"] == request
        assert audit["predicate_count"] == 2
        assert audit["terminal_proof_count"] == 1
        terminal = audit["terminal_proofs"][0]
        assert terminal["terminal_authorized"] is True
        assert terminal["status"] == "TERMINAL_BLOCKED_VERIFIED"
        assert terminal["evidence_ref"] == conflict_ref
        assert terminal["observed"] is True and terminal["expected"] is False
        assert terminal["proof_sha256"] and terminal["subject_sha256"]
        assert "subject" not in terminal
        assert audit["predicates"][0]["field"] == "data.exit_code"
        assert audit["predicates"][0]["matching_evidence_refs"] == [evidence]
        summary = data["goal75_flow_summary.json"]
        if summary["trace_status"] != "SEQUENCE_AND_PARENT_LINKS_VALID":
            raise AssertionError("COLLECT_TRACE_CHAIN_REJECTED:" + summary["trace_status"])
        if summary["frozen_stage_count"] != 1:
            raise AssertionError("COLLECT_FROZEN_STAGE_COUNT_INVALID")
        refs = data["goal75_big_blackboard_events.json"]
        if not any(event["module"] == "COLLECTOR_SIDECAR" for event in refs):
            raise AssertionError("COLLECT_MODULE_EVENT_MISSING")
        assert all(row["task_id"] == task and row["request_id"] == request
                   for row in refs)
        manifest = data["manifest.json"]
        if manifest["goal75_frozen_stage_count"] != 1:
            raise AssertionError("COLLECT_MANIFEST_G75_MISMATCH")
        return {
            "schema": "SMARTAGENT_GOAL75_COLLECTOR_IT_V1",
            "status": "PASS",
            "task_id": task, "request_id": request,
            "collector_exit_code": output.returncode,
            "zip_exists": True,
            "zip_bytes": filename.stat().st_size,
            "zip_verified_fields": sorted(expected),
            "blackboard_module_count": len({event["module"] for event in refs}),
            "blackboard_event_count": len(refs),
            "stage_01_frozen": True,
            "collector_manifest_stage_freeze_verified": True,
            "no_model_command_or_secret_exported": all(
                "command" not in json.dumps(row).lower()
                for row in refs
            ),
        }
    finally:
        # In CI the ZIP is retained by the user-facing collector directory.
        # It is not copied into a source release or uploaded by this test.
        pass


def main() -> int:
    result = run_collector_it()
    target = Path("git75-ci-evidence/goal75-collector-it.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
