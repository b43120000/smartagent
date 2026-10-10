#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.protocol import WEBAGENT_PROTOCOL_BODY, render_initial_planner_toolkit
from agent_core.capability_recovery import build_result_evidence_to_action_route
from agent_core.project_sync import inspect_project_scope
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.task_plan import (
    TASK_PLAN_SCHEMA,
    repair_task_plan,
    validate_task_plan,
)


def _progress(action_id: str, next_action: str = "repair_task_plan") -> dict:
    return {
        "tool": "report_progress",
        "action_id": action_id,
        "base_evaluation": "Task plan validation is blocked on canonical repair.",
        "total_steps": 2,
        "current_step": 1,
        "steps": [
            {"step": 1, "desc": "Repair the plan", "status": "IN_PROGRESS"},
            {"step": 2, "desc": "Freeze the plan", "status": "PENDING"},
        ],
        "current_focus": "Run the Runtime-issued repair prerequisite",
        "next_action": next_action,
        "completion_contract": {
            "success": ["Plan validation returns PLAN_FROZEN"],
            "failure": ["Plan validation returns a terminal failure"],
            "in_progress": ["Canonical plan repair is required"],
            "interrupted": ["Runtime cannot repair the task plan"],
        },
        "decision": "CONTINUE",
        "outcome": "PENDING",
        "matched_condition": "Canonical plan repair is required",
        "evidence_refs": ["REQUEST_ACCEPTED"],
        "decision_reason": "The validator rejected the submitted schema.",
    }


def _response(*blocks: dict) -> str:
    payloads = [
        "```smartagent_tool\n" + json.dumps(block, ensure_ascii=False) + "\n```"
        for block in blocks
    ]
    payloads.append(
        "```smartagent_tool\n"
        + json.dumps({"tool": "turn_commit", "action_count": len(blocks)})
        + "\n```"
    )
    return "\n".join(payloads)


def run() -> None:
    toolkit = render_initial_planner_toolkit()
    assert '"schema":"TASK_PLAN_V1"' in toolkit
    assert "repair_task_plan" in toolkit
    assert "SMARTAGENT_TASK_PLAN_SCHEMA_CONTRACT" in WEBAGENT_PROTOCOL_BODY

    with tempfile.TemporaryDirectory(prefix="smartagent-plan-schema-") as temp:
        workspace = Path(temp)
        snapshot = inspect_project_scope(workspace)
        alias_plan = {
            "schema": "SMARTAGENT_INSTANCE_AWARE_PLAN_V1",
            "base_snapshot_id": snapshot["snapshot_id"],
            "semantic_map_revision": "",
            "goal": "Create a bounded text artifact",
            "affected_flows": ["task plan validation"],
            "files_to_read": [],
            "edit_plan": [{
                "path": "notes.txt",
                "create": True,
                "mode": "whole_file",
                "content": "ready\n",
                "modification_intent": "Create validation fixture",
            }],
            "verification_commands": ["python -c \"print('ok')\""],
            "acceptance_criteria": ["notes.txt is produced"],
            "rollback_condition": ["verification fails"],
            "post_change_semantic": [],
        }

        rejected = validate_task_plan(workspace, alias_plan)
        assert rejected["status"] == "INVALID_PLAN"
        assert rejected["reason"] == "SCHEMA_MISMATCH"
        assert rejected["submitted_schema"] == "SMARTAGENT_INSTANCE_AWARE_PLAN_V1"
        assert rejected["expected_schema"] == TASK_PLAN_SCHEMA
        assert rejected["required_action"] == "repair_task_plan"
        assert rejected["diagnostics"][0]["path"] == "$.schema"

        proposed = {
            "tool": "propose_task_plan",
            "action_id": "PLAN-INVALID",
            "workspace": str(workspace),
            "plan": alias_plan,
        }
        route = build_result_evidence_to_action_route(
            proposed, json.dumps(rejected), project_root_hint=str(workspace),
        )
        assert route["runtime_state"] == "BLOCKED_ON_PREREQUISITE"
        assert route["required_action"] == "repair_task_plan"
        assert route["forbidden_actions"] == [
            "propose_task_plan", "propose_task_plan_file",
        ]
        repair_action = dict(route["next_actions"][0])
        repair_action["action_id"] = "PLAN-REPAIR-1"
        assert repair_action["plan"]["schema"] == TASK_PLAN_SCHEMA
        assert isinstance(repair_action["plan"]["edit_plan"], dict)
        assert isinstance(repair_action["plan"]["rollback_condition"], str)

        valid, diagnostic = validate_tool_envelope(repair_action)
        assert valid is True, diagnostic

        unlocked_loop = WebAgentProtocolLoop(str(workspace), lambda *_args: "")
        unlocked_calls = [
            _progress("P-UNLOCKED"), repair_action,
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = unlocked_loop._accept_ack(unlocked_calls, {})
        assert accepted == []
        assert diagnostics[0]["reason"] == "task_plan_repair_without_runtime_latch"

        loop = WebAgentProtocolLoop(str(workspace), lambda *_args: "")
        loop.run_id = "RR-PLAN-SCHEMA-TEST"
        loop.task_id = "TASK-PLAN-SCHEMA-TEST"
        loop.task_epoch = "EPOCH-PLAN-SCHEMA-TEST"
        loop.intent_digest = "d" * 64
        loop.pending_result_recovery = route
        forbidden_calls = [
            _progress("P-FORBIDDEN"),
            {**proposed, "action_id": "PLAN-FORBIDDEN-RESUBMIT"},
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = loop._accept_ack(forbidden_calls, {})
        assert accepted == []
        assert diagnostics[0]["reason"] == "task_plan_repair_latch_active"

        mismatched_repair = dict(repair_action)
        mismatched_repair["action_id"] = "PLAN-REPAIR-MISMATCH"
        mismatched_repair["plan"] = {
            **repair_action["plan"], "goal": "Changed outside Runtime repair route",
        }
        accepted, diagnostics = loop._accept_ack([
            _progress("P-MISMATCH"), mismatched_repair,
            {"tool": "turn_commit", "action_count": 2},
        ], {})
        assert accepted == []
        assert diagnostics[0]["reason"] == "task_plan_repair_payload_mismatch"

        accepted, diagnostics = loop._accept_ack([
            _progress("P-CANONICAL"), repair_action,
            {"tool": "turn_commit", "action_count": 2},
        ], {})
        assert diagnostics == []
        assert [item["tool"] for item in accepted] == [
            "report_progress", "repair_task_plan",
        ]

        repeated = build_result_evidence_to_action_route(
            repair_action, json.dumps(rejected),
            project_root_hint=str(workspace), prior_context=route,
        )
        assert repeated["reason"] == "task_plan_repair_stalled"
        assert repeated["same_diagnostic_count"] == 2
        assert repeated["next_actions"] == []
        assert repeated["pause_immediately"] is True

        frozen = repair_task_plan(workspace, alias_plan)
        assert frozen["status"] == "PLAN_FROZEN", frozen
        assert any(
            item["code"] == "SCHEMA_ALIAS_CANONICALIZED"
            for item in frozen["canonical_repairs"]
        )
        dispatched = json.loads(unlocked_loop.tools.execute(repair_action))
        assert dispatched["status"] == "PLAN_FROZEN", dispatched

        high_level_plan = {
            **alias_plan,
            "edit_plan": [{"area": "state_isolation", "change": "namespace state"}],
            "post_change_semantic": ["Each instance owns its lifecycle"],
        }
        high_level_rejected = validate_task_plan(workspace, high_level_plan)
        high_level_route = build_result_evidence_to_action_route(
            {**proposed, "plan": high_level_plan},
            json.dumps(high_level_rejected), project_root_hint=str(workspace),
        )
        high_level_repair = dict(high_level_route["next_actions"][0])
        high_level_repair["action_id"] = "PLAN-HIGH-LEVEL-REPAIR-1"
        high_level_result = repair_task_plan(workspace, high_level_plan)
        assert high_level_result["status"] == "INVALID_PLAN"
        assert high_level_result["reason"] == "task_plan_invalid_post_change_semantic_entry"
        assert high_level_result["diagnostics"][0]["path"] == "$.post_change_semantic"
        high_level_second_route = build_result_evidence_to_action_route(
            high_level_repair, json.dumps(high_level_result),
            project_root_hint=str(workspace), prior_context=high_level_route,
        )
        assert high_level_second_route["next_actions"][0]["tool"] == "repair_task_plan"
        high_level_second_repair = dict(high_level_second_route["next_actions"][0])
        high_level_second_repair["action_id"] = "PLAN-HIGH-LEVEL-REPAIR-2"
        high_level_stalled = build_result_evidence_to_action_route(
            high_level_second_repair, json.dumps(high_level_result),
            project_root_hint=str(workspace), prior_context=high_level_second_route,
        )
        assert high_level_stalled["reason"] == "task_plan_repair_stalled"
        assert high_level_stalled["pause_immediately"] is True

        prompts: list[str] = []

        def planner(prompt: str, _expected: dict, _attachments: list[str]) -> str:
            prompts.append(prompt)
            if len(prompts) == 1:
                return _response(_progress("P-LOOP-1", "propose_task_plan"), proposed)
            return _response(_progress("P-LOOP-2"), repair_action)

        stalled_loop = WebAgentProtocolLoop(
            str(workspace), planner, progress_root=workspace, max_turns=5,
        )
        stalled_loop.tools.execute = lambda _action: json.dumps(rejected)
        try:
            stalled_loop.run(
                "Freeze the task plan",
                request_id="RR-PLAN-REPAIR-STALLED",
                task_id="TASK-PLAN-REPAIR-STALLED",
                task_epoch="EPOCH-PLAN-REPAIR-STALLED",
            )
        except RuntimeError as exc:
            assert "相同 validator 診斷" in str(exc)
        else:
            raise AssertionError("identical task-plan diagnostics must pause immediately")
        assert len(prompts) == 2

    print("TASK_PLAN_SCHEMA_RECOVERY_OK")


if __name__ == "__main__":
    run()
