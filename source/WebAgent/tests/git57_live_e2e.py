#!/usr/bin/env python3
"""Git57 local Live E2E: real SmartAgent tools/evidence, test-only Planner.

No fake ToolResult, monkeypatch, host CMD, shell command or ACL modification.
Run under the operator's existing Runtime policy. The Planner is deliberately
deterministic: this verifies real Executor/ToolContext/Evidence/Finish code,
not DOM-based GPT transport. For real-model E2E, wrap its existing Planner
callable with TestOnlyFirstFinishInjector in a separately authorized test host.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

SOURCE = Path(__file__).resolve().parents[2]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from agent_core.executor_run_blackboard import read_executor_run, read_executor_event
from agent_core.git57_live_finish_injector import TestOnlyFirstFinishInjector
from agent_core.runtime_evidence_store import _record_path, read_runtime_evidence
from agent_core.tool_result_contract import decode_result
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tool_context import WebAgentToolContext


class ObservedStatePlanner:
    """Controlled Planner boundary, not a replacement Executor/ToolContext."""
    def __init__(self, scenario: str):
        self.scenario = scenario
        self.loop = None
        self.call_count = 0
        self.finish_count = 0
        self.ref = ""
        self.first_source_sha256 = ""

    def __call__(self, prompt, expected, attachments):
        self.call_count += 1
        if self.call_count == 1:
            return json.dumps({
                "type": "tool_call", "name": "inspect_directory",
                "arguments": {"paths": [self.loop.git57_target], "recursive": False},
            }, separators=(",", ":"))
        ledger = self.loop.action_result_ledger
        if len(ledger) != 1:
            raise RuntimeError("GIT57_EXPECTED_EXACTLY_ONE_EXECUTED_ACTION")
        entry = next(iter(ledger.values()))
        receipt = decode_result(entry["result"])
        if (receipt is None or receipt["status"] != "SUCCEEDED"
                or receipt.get("executed") is not True
                or entry.get("tool") != "inspect_directory"):
            raise RuntimeError("GIT57_REAL_INSPECTION_NOT_SUCCEEDED")
        data = receipt.get("data") or {}
        actual = data.get("observed_exists")
        if (type(actual) is not bool
                or data.get("observation_status") != "CONFIRMED"):
            raise RuntimeError("GIT57_OBSERVATION_EVIDENCE_INCOMPLETE")
        self.ref = entry["evidence_ref"]
        self.finish_count += 1
        # In the "exhaust" case, the first response is truthful before
        # test-only injection; subsequent test Planner replies deliberately
        # remain wrong so Runtime must exhaust its bounded repair budget.
        claimed = (not actual if self.scenario == "exhaust"
                   and self.finish_count > 1 else actual)
        result = {
            "type": "finish",
            "summary": (
                "根據實際 Runtime 觀察，目標資料夾"
                + ("存在" if actual else "不存在")
            ),
            "expected_runtime": [{
                "evidence_ref": self.ref,
                "fields": {
                    "status": receipt["status"],
                    "data.observed_exists": claimed,
                    "data.observation_status": "CONFIRMED",
                },
            }],
            "goal_checks": [{
                "criterion": "原始使用者要求的資料夾存在性觀察",
                "evidence_ref": self.ref,
                "field": "data.observed_exists",
                "operator": "eq",
                "expected": actual,
            }],
        }
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _safe_path(path: Path, workspace: Path) -> Path:
    # Explicit test only in an existing authorized parent. A junction inside
    # the requested target path may not silently escape the workspace.
    target = path.expanduser().absolute()
    if os.name != "nt":
        raise RuntimeError("GIT57_LIVE_WINDOWS_ONLY")
    if target == workspace or not target.is_relative_to(workspace):
        raise RuntimeError("GIT57_TARGET_OUTSIDE_WORKSPACE")
    if target.name.upper().startswith("GIT57_LIVE_") is False:
        raise RuntimeError("GIT57_TARGET_MUST_HAVE_TEST_PREFIX")
    if not target.parent.is_dir():
        raise RuntimeError("GIT57_TARGET_PARENT_NOT_FOUND")
    from agent_core.path_security import reject_reparse_components
    reject_reparse_components(target.parent, workspace)
    if target.exists():
        raise RuntimeError("GIT57_TARGET_ALREADY_EXISTS")
    if target.is_symlink():
        raise RuntimeError("GIT57_TARGET_REPARSE_FORBIDDEN")
    if target.resolve(strict=False).parent != target.parent.resolve():
        raise RuntimeError("GIT57_TARGET_PATH_NOT_CANONICAL")
    return target


def _run(args) -> dict:
    workspace = Path(args.workspace).expanduser().resolve()
    progress_root = Path(args.progress_root).expanduser().resolve()
    if not workspace.is_dir() or not progress_root.is_dir():
        raise RuntimeError("GIT57_WORKSPACE_OR_PROGRESS_ROOT_MISSING")
    target = _safe_path(Path(args.target), workspace)
    # The original task is intentionally ONLY an observation, never the
    # meta-instructions describing how to test/inject a Planner response.
    request = "這個資料夾是否存在？\n" + str(target)
    key = uuid.uuid4().hex[:20].upper()
    task_id, request_id, epoch = (
        "TASK-GIT57-" + key, "RR-GIT57-" + key, "EPOCH-" + key,
    )
    planner = ObservedStatePlanner(args.scenario)
    tools = WebAgentToolContext(workspace, interface_name="webdirect")
    loop = WebAgentProtocolLoop(
        workspace, planner, tool_context=tools,
        progress_root=progress_root, max_turns=6, display_name="Git57 Test",
    )
    loop.run_id, loop.task_id, loop.task_epoch = request_id, task_id, epoch
    loop.task_request = request
    loop.executor_only_mode = True
    loop.git57_target = str(target)
    loop.authorized_paths = [str(target)]
    tools.current_task_id = task_id
    tools.current_task_epoch = epoch
    tools.begin_run(request_id, request, [str(target)])
    planner.loop = loop
    injector = TestOnlyFirstFinishInjector(planner, loop)
    loop.planner = injector

    try:
        response = run_minimal_core(loop, request)
    except Exception as exc:
        response = "EXCEPTION:" + type(exc).__name__
    board = read_executor_run(
        progress_root, task_id=task_id, request_id=request_id,
        task_epoch=epoch,
    )
    if not isinstance(board, dict):
        raise RuntimeError("GIT57_EXECUTOR_BLACKBOARD_NOT_PERSISTED")
    all_events = [
        read_executor_event(
            progress_root, task_id=task_id, request_id=request_id,
            task_epoch=epoch, sequence=i,
        )
        for i in range(1, 1 + int(board.get("event_count_total", 0)))
    ]
    receipts = [row for row in all_events if row.get("kind") == "TOOL_RESULT"]
    repairs = [row for row in all_events
               if row.get("kind") == "MODEL_FINISH_SEMANTIC_REPAIR"]
    injects = [row for row in all_events
               if row.get("kind") == "GIT57_TEST_FINISH_INJECTION"]
    terminal = board.get("terminal") or {}
    comparison = terminal.get("finish_comparison") or {}
    entries = list(loop.action_result_ledger.values())
    actual = None
    evidence_unchanged = False
    evidence_digest = ""
    evidence_ref = ""
    if len(entries) == 1:
        receipt = decode_result(entries[0]["result"])
        actual = (receipt or {}).get("data", {}).get("observed_exists")
        evidence_ref = str(entries[0].get("evidence_ref") or "")
        evidence_path = _record_path(progress_root, task_id, evidence_ref)
        if evidence_path.is_file():
            record = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence_digest = record.get("raw_result_sha256", "")
            evidence_unchanged = (
                hashlib.sha256(record["raw_result"].encode("utf-8")).hexdigest()
                == evidence_digest
                == hashlib.sha256(entries[0]["result"].encode("utf-8")).hexdigest()
                and (entries[0].get("evidence_store") or {}).get("store_status")
                == "PERSISTED_AND_READBACK_VERIFIED"
            )
            # Verify the public task-bound evidence API retrieves the exact
            # immutable result, not just a filesystem record.
            readback = read_runtime_evidence(
                progress_root, task_id=task_id, request_id=request_id,
                task_epoch=epoch, evidence_ref=evidence_ref,
                section="raw_result", max_chars=2400,
            )
            evidence_unchanged = (
                evidence_unchanged
                and readback["source_result_sha256"] == evidence_digest
                and readback["task_bound"] is True
            )
    frozen_contract = board.get("goal_state_contract") or {}
    from agent_core.requirement_goal_state import (
        compile_requirement_goal_state, validate_frozen_goal_contract,
    )
    contract_unchanged = (
        validate_frozen_goal_contract(frozen_contract, request)
        and frozen_contract == compile_requirement_goal_state(request)
    )
    common_ok = (
        len(receipts) == 1 and len(entries) == 1
        and len(injects) == 1 and injector.injected
        and evidence_unchanged and contract_unchanged
        and len(set(x.get("runtimeEvidence", {}).get("evidence_ref") for x in receipts)) == 1
        and evidence_ref == injects[0].get("target_evidence_ref")
        and type(actual) is bool
        and frozen_contract.get("status") == "COMPILED"
        and frozen_contract.get("goal_kind") == "DIRECTORY_OBSERVATION"
        and not target.exists()
    )
    if args.scenario == "recover":
        passed = (
            common_ok and len(repairs) == 1 and planner.finish_count == 2
            and repairs[0].get("reason_code") == "MODEL_RUNTIME_MISMATCH"
            and repairs[0].get("repair_only") is True
            and repairs[0].get("failure_origin") == "PLANNER_ASSERTION_MISMATCH"
            and comparison.get("verdict") == "PASS"
            and comparison.get("reason_code") == "FROZEN_GOAL_STATE_SATISFIED"
            and comparison.get("coverage_basis") == "RUNTIME_FROZEN_GOAL_CONTRACT"
            and loop.terminal_outcome == "SUCCESS"
        )
    else:
        passed = (
            common_ok and len(repairs) == 2 and planner.finish_count == 3
            and board.get("executor_state") == "FINISH_CONTRACT_INVALID"
            and comparison.get("verdict") != "PASS"
            and not terminal.get("goal_verified", True)
            and loop.terminal_outcome != "SUCCESS"
        )
    report = {
        "schema": "SMARTAGENT_GIT57_LIVE_E2E_REPORT_V1",
        "scenario": args.scenario,
        "task_id": task_id, "request_id": request_id, "task_epoch": epoch,
        "original_requirement": request,
        "goal_contract_status": frozen_contract.get("status"),
        "goal_kind": frozen_contract.get("goal_kind"),
        "goal_contract_sha256": frozen_contract.get("contract_sha256"),
        "goal_contract_unchanged": contract_unchanged,
        "tool_execution_count": len(receipts),
        "semantic_repair_count": len(repairs),
        "finish_attempt_count": planner.finish_count,
        "injection_count": len(injects),
        "injected_value": injects[0].get("planner_declared_value_after_injection")
                          if injects else None,
        "runtime_actual_value": actual,
        "evidence_ref": evidence_ref,
        "evidence_sha256": evidence_digest,
        "evidence_unchanged": evidence_unchanged,
        "final_verdict": comparison.get("verdict") or "NOT_ADMITTED",
        "comparator_cause": comparison.get("reason_code") or "",
        "failure_origin": comparison.get("failure_origin") or "",
        "executor_state": board.get("executor_state"),
        "runtime_event_sequence": [
            {"sequence": e.get("sequence"), "round": e.get("round"),
             "kind": e.get("kind"), "action_id": e.get("action_id"),
             "evidence_ref": (
                 e.get("runtimeEvidence", {}).get("evidence_ref")
                 or e.get("target_evidence_ref") or ""),
             "reason_code": e.get("reason_code", "")}
            for e in all_events
            if e.get("kind") in {
                "ACTION_ADMITTED", "TOOL_RESULT",
                "GIT57_TEST_FINISH_INJECTION",
                "MODEL_FINISH_SEMANTIC_REPAIR",
                "MODEL_FINISH_CONTRACT_INVALID", "MODEL_FINISH",
            }
        ],
        "verdict": "GIT57_LIVE_E2E_PASS" if passed else "GIT57_LIVE_E2E_FAIL",
        "test_driver": "DETERMINISTIC_PLANNER_REAL_RUNTIME",
        "dom_model_planner_tested": False,
        "user_files_modified": False,
        "runtime_test_evidence_written": True,
        "runtime_summary_preview": response[:300],
    }
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--progress-root", default=str(SOURCE.parent))
    parser.add_argument("--scenario", choices=["recover", "exhaust"], required=True)
    args = parser.parse_args(argv)
    try:
        result = _run(args)
    except Exception as exc:
        result = {
            "schema": "SMARTAGENT_GIT57_LIVE_E2E_REPORT_V1",
            "scenario": args.scenario, "verdict": "GIT57_LIVE_E2E_BLOCKED",
            "reason_code": type(exc).__name__, "error": str(exc)[:250],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verdict"] == "GIT57_LIVE_E2E_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
