#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.capability_recovery import (
    build_result_evidence_to_action_route,
    render_result_evidence_to_action_guidance,
)
from agent_core.edit_plan_contract import validate_edit_plan
from agent_core.project_sync import inspect_project_scope
from agent_core.result_exchange import prepare_tool_result
from agent_core.smartagent_protocol import validate_tool_envelope
from WebAgent.protocol_loop import WebAgentProtocolLoop


def _fence(payload: dict) -> str:
    return "```smartagent_tool\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def _response(*blocks: dict) -> str:
    return "\n".join([
        *(_fence(block) for block in blocks),
        _fence({"tool": "turn_commit", "action_count": len(blocks)}),
    ])


def _continuing_progress(action_id: str) -> dict:
    return {
        "tool": "report_progress", "action_id": action_id,
        "base_evaluation": "Plan prerequisite is not complete.",
        "total_steps": 2, "current_step": 1,
        "steps": [
            {"step": 1, "desc": "Collect evidence", "status": "COMPLETED"},
            {"step": 2, "desc": "Apply plan", "status": "IN_PROGRESS"},
        ],
        "current_focus": "Resolve semantic prerequisite",
        "next_action": "Follow the Runtime recovery route",
        "completion_contract": {
            "success": ["Plan applied and verified"],
            "failure": ["Plan application failed"],
            "in_progress": ["Semantic prerequisite remains"],
            "interrupted": ["Runtime recovery has no safe action"],
        },
        "decision": "CONTINUE", "outcome": "PENDING",
        "matched_condition": "Semantic prerequisite remains",
        "evidence_refs": ["REQUEST_ACCEPTED"],
        "decision_reason": "Runtime has not completed the prerequisite.",
    }


class FakeAgent:
    def __init__(self, workspace: Path):
        self.workspace_root = workspace


def run() -> None:
    root = r"D:\workspace\SmartAgentv2"
    action = {
        "tool": "apply_edit_plan",
        "action_id": "APPLY-INVALID",
        "plan": {"scope": ["source/agent_core/task_plan.py"]},
    }
    result = json.dumps({
        "status": "INVALID_PLAN",
        "validation": {
            "status": "INVALID_PLAN",
            "reason": "MISSING_FIELDS",
            "missing_fields": [
                "base_snapshot_id", "files_to_modify", "verification_commands",
                "expected_observable_result", "rollback_condition",
            ],
            "current_snapshot_id": "SNAP-CURRENT",
        },
        "applied": [],
        "rolled_back": False,
    })
    route = build_result_evidence_to_action_route(
        action, result, project_root_hint=root,
    )
    assert route["active"] is True
    canonical = route["canonical_action"]
    assert canonical["workspace"] == root
    assert canonical["plan"]["base_snapshot_id"] == "SNAP-CURRENT"
    assert "files_to_modify" in canonical["plan"]
    assert "base_snapshot_id" not in {
        key for key in canonical if key not in {"plan"}
    }
    guidance = render_result_evidence_to_action_guidance(route)
    assert "WEBAGENT_RESULT_EVIDENCE_TO_ACTION" in guidance
    assert "Do not merely report" in guidance

    missing_content_action = {
        "tool": "apply_edit_plan",
        "action_id": "APPLY-MISSING-CONTENT",
        "workspace": root,
        "plan": {
            "base_snapshot_id": "SNAP-CONTENT",
            "files_to_modify": [{
                "path": "source/agent_core/task_plan.py",
                "modification_intent": "Add lifecycle reporting",
                "mode": "whole_file",
            }],
            "verification_commands": ["python -m pytest"],
            "expected_observable_result": "tests pass",
            "rollback_condition": "tests fail",
        },
    }
    missing_route = build_result_evidence_to_action_route(
        missing_content_action,
        json.dumps({
            "status": "ROLLED_BACK",
            "error": "MISSING_CONTENT:source/agent_core/task_plan.py",
            "applied": [],
            "rolled_back": True,
        }),
        project_root_hint=root,
    )
    assert missing_route["reason"] == "edit_plan_missing_content"
    assert missing_route["blocked_tool"] == "apply_edit_plan"
    assert missing_route["prerequisite_satisfied"] is False
    missing_read = missing_route["next_actions"][0]
    assert missing_read["tool"] == "query_project"
    assert missing_read["snapshot_id"] == "SNAP-CONTENT"
    assert missing_read["queries"] == [{
        "operation": "read_range",
        "path": "source/agent_core/task_plan.py",
        "start_line": 1,
        "end_line": 240,
    }]
    missing_continuation = build_result_evidence_to_action_route(
        missing_read,
        json.dumps({
            "status": "READY",
            "results": [{
                "operation": "read_range", "status": "OK",
                "path": "source/agent_core/task_plan.py",
                "file_sha256": "b" * 64,
                "content": "first page", "truncated": True,
                "next_cursor": 241,
            }],
        }),
        project_root_hint=root,
        prior_context=missing_route,
    )
    assert missing_continuation["prerequisite_satisfied"] is False
    assert missing_continuation["next_actions"][0]["queries"][0]["start_line"] == 241
    missing_ready = build_result_evidence_to_action_route(
        missing_continuation["next_actions"][0],
        json.dumps({
            "status": "READY",
            "results": [{
                "operation": "read_range", "status": "OK",
                "path": "source/agent_core/task_plan.py",
                "file_sha256": "b" * 64,
                "content": "last page", "truncated": False,
                "next_cursor": None,
            }],
        }),
        project_root_hint=root,
        prior_context=missing_continuation,
    )
    assert missing_ready["reason"] == "edit_plan_source_evidence_ready"
    assert missing_ready["prerequisite_satisfied"] is True
    assert missing_ready["completed_content_paths"] == [
        "source/agent_core/task_plan.py"
    ]

    with tempfile.TemporaryDirectory(prefix="smartagent-edit-contract-") as temp:
        contract_root = Path(temp)
        (contract_root / "source.py").write_text("value = 1\n", encoding="utf-8")
        snapshot = inspect_project_scope(contract_root)
        incomplete_plan = {
            "base_snapshot_id": snapshot["snapshot_id"],
            "files_to_modify": [{
                "path": "source.py", "mode": "whole_file",
                "modification_intent": "change value",
            }],
            "verification_commands": ["python -m pytest"],
            "expected_observable_result": "tests pass",
            "rollback_condition": "tests fail",
        }
        contract_result = validate_edit_plan(contract_root, incomplete_plan)
        assert contract_result["reason"] == "MISSING_CONTENT"
        assert contract_result["error"] == "MISSING_CONTENT:source.py"
        contract_loop = WebAgentProtocolLoop(
            str(contract_root), lambda *_args: "", progress_root=contract_root,
        )
        contract_loop._execute_action({
            "tool": "apply_edit_plan",
            "action_id": "APPLY-CONTRACT-MISSING-CONTENT",
            "workspace": str(contract_root),
            "plan": incomplete_plan,
        })
        assert contract_loop.pending_result_recovery["reason"] == "edit_plan_missing_content"
        assert contract_loop.pending_result_recovery["next_actions"][0]["queries"][0]["path"] == "source.py"

    with tempfile.TemporaryDirectory(prefix="smartagent-edit-prerequisite-") as temp:
        guarded_loop = WebAgentProtocolLoop(temp, lambda *_args: "")
        guarded_loop.pending_result_recovery = missing_route
        premature_calls = [
            _continuing_progress("P-PREMATURE-APPLY"),
            missing_content_action,
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = guarded_loop._accept_ack(premature_calls, {})
        assert accepted == []
        assert diagnostics[0]["reason"] == "edit_plan_prerequisite_unsatisfied"
        wrong_read = dict(missing_read)
        wrong_read["action_id"] = "READ-WRONG-PATH"
        wrong_read["queries"] = [{
            "operation": "read_range", "path": "source/agent_core/task_progress.py",
            "start_line": 1, "end_line": 240,
        }]
        wrong_calls = [
            _continuing_progress("P-WRONG-READ"), wrong_read,
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = guarded_loop._accept_ack(wrong_calls, {})
        assert accepted == []
        assert diagnostics[0]["reason"] == "edit_plan_prerequisite_action_mismatch"

        shorthand_calls = [
            _continuing_progress("P-QUERY-SHORTHAND"),
            {
                "tool": "query_project", "action_id": "QUERY-SHORTHAND",
                "project_root": temp,
                "queries": ["read source/agent_core/task_plan.py content"],
            },
            {"tool": "turn_commit", "action_count": 2},
        ]
        accepted, diagnostics = guarded_loop._accept_ack(shorthand_calls, {})
        assert accepted == []
        assert diagnostics[0]["reason"] == "query_project_structured_queries_required"

    task_plan_action = {
        "tool": "propose_task_plan",
        "action_id": "PLAN-SEMANTIC",
        "workspace": root,
        "plan": {
            "base_snapshot_id": "SNAP-OLD",
            "semantic_map_revision": "",
            "files_to_read": ["source/agent_core/task_plan.py"],
            "edit_plan": {"files_to_modify": [
                {"path": "source/agent_core/task_progress.py"},
            ]},
        },
    }
    plan_route = build_result_evidence_to_action_route(
        task_plan_action,
        json.dumps({
            "status": "SEMANTIC_MAP_NOT_FRESH",
            "required_semantic_paths": [
                "source/agent_core/task_plan.py",
                "source/agent_core/task_progress.py",
            ],
            "semantic_map_status": {
                "status": "MISSING", "snapshot_id": "SNAP-NEW",
                "semantic_map_revision": "",
            },
        }),
        project_root_hint=root,
    )
    inspect_action = plan_route["next_actions"][0]
    assert inspect_action["tool"] == "inspect_semantic_map"
    assert inspect_action["paths"] == [
        "source/agent_core/task_plan.py",
        "source/agent_core/task_progress.py",
    ]
    inspect_route = build_result_evidence_to_action_route(
        inspect_action,
        json.dumps({
            "status": "MISSING", "snapshot_id": "SNAP-NEW",
            "required_paths": inspect_action["paths"],
            "needs_analysis": inspect_action["paths"],
            "needs_analysis_count": 2,
        }),
        project_root_hint=root,
        prior_context=plan_route,
    )
    query_action = inspect_route["next_actions"][0]
    assert query_action["tool"] == "query_project"
    assert query_action["snapshot_id"] == "SNAP-NEW"
    query_route = build_result_evidence_to_action_route(
        query_action,
        json.dumps({
            "status": "READY",
            "results": [{
                "status": "OK", "path": "source/agent_core/task_plan.py",
                "file_sha256": "a" * 64, "content": "def validate_task_plan(): pass",
            }],
        }),
        project_root_hint=root,
        prior_context=inspect_route,
    )
    update_action = query_route["next_actions"][0]
    assert update_action["tool"] == "update_semantic_map"
    assert update_action["patch"]["base_snapshot_id"] == "SNAP-NEW"
    assert update_action["patch"]["files"][0]["source_sha256"] == "a" * 64
    continuation_route = build_result_evidence_to_action_route(
        query_action,
        json.dumps({
            "status": "READY",
            "results": [{
                "status": "OK", "path": "source/agent_core/task_plan.py",
                "file_sha256": "a" * 64, "content": "first page",
                "truncated": True, "next_cursor": 241,
            }],
        }),
        project_root_hint=root,
        prior_context=inspect_route,
    )
    continuation = continuation_route["next_actions"][0]
    assert continuation["tool"] == "query_project"
    assert continuation["queries"][0]["start_line"] == 241
    reinspection = build_result_evidence_to_action_route(
        update_action,
        json.dumps({"status": "PARTIAL"}),
        project_root_hint=root,
        prior_context=query_route,
    )
    assert reinspection["next_actions"][0]["tool"] == "inspect_semantic_map"
    fresh_route = build_result_evidence_to_action_route(
        reinspection["next_actions"][0],
        json.dumps({
            "status": "FRESH", "snapshot_id": "SNAP-NEW",
            "semantic_map_revision": "SEM-REV-1",
            "required_paths": inspect_action["paths"],
        }),
        project_root_hint=root,
        prior_context=reinspection,
    )
    resumed = fresh_route["next_actions"][0]
    assert resumed["tool"] == "propose_task_plan"
    assert resumed["plan"]["base_snapshot_id"] == "SNAP-NEW"
    assert resumed["plan"]["semantic_map_revision"] == "SEM-REV-1"

    with tempfile.TemporaryDirectory(prefix="smartagent-loop-recovery-") as temp:
        loop = WebAgentProtocolLoop(temp, lambda *_args: "")
        loop.action_ledger["SYNC"] = {
            "action": {
                "tool": "project_sync",
                "project_root": root,
            },
        }
        loop.tools.execute = lambda _action: result
        loop._execute_action(action)
        recorded = loop.action_ledger[action["action_id"]]["recovery_context"]
        assert recorded["canonical_action"]["workspace"] == root
        injected = loop._result_recovery_guidance([action])
        assert "WEBAGENT_RESULT_EVIDENCE_TO_ACTION" in injected
        assert '"workspace":"D:\\\\workspace\\\\SmartAgentv2"' in injected
        assert "WEBAGENT_RESULT_EVIDENCE_TO_ACTION" in loop._result_recovery_guidance([])
        loop.tools.execute = lambda _action: json.dumps({
            "status": "APPLIED", "applied": ["source/agent_core/task_plan.py"],
            "rolled_back": False,
        })
        valid_action = dict(action)
        valid_action["action_id"] = "APPLY-VALID"
        loop._execute_action(valid_action)
        assert loop.pending_result_recovery == {}

    malformed = {
        "tool": "apply_edit_plan",
        "action_id": "APPLY-WRONG-LAYER",
        "plan": {},
        "base_snapshot_id": "SNAP",
        "files_to_modify": [],
    }
    valid, diagnostic = validate_tool_envelope(malformed)
    assert valid is False
    assert diagnostic["reason"] == "unexpected_field"
    assert "全部放在 plan 內" in diagnostic["suggestion"]

    normalized, diagnostic = WebAgentProtocolLoop._normalize_report_progress_envelope({
        "tool": "report_progress",
        "action_id": "PROGRESS-RUNTIME-STATE",
        "current_step": 1,
        "total_steps": 2,
        "current_focus": "working",
        "runtime_state": "PROCESSING",
    })
    assert diagnostic is None
    assert "runtime_state" not in normalized

    with tempfile.TemporaryDirectory(prefix="smartagent-result-recovery-") as temp:
        workspace = Path(temp)
        oversized = json.dumps({
            "status": "SEMANTIC_MAP_NOT_FRESH",
            "current_snapshot_id": "SNAP-LATEST",
            "semantic_map_status": {
                "status": "MISSING",
                "semantic_map_revision": "",
                "needs_analysis_count": 274,
                "needs_analysis": ["source/file_%05d.py" % index for index in range(5000)],
            },
        })
        compact = prepare_tool_result(
            {"tool": "propose_task_plan", "action_id": "PLAN-LARGE"},
            oversized,
            FakeAgent(workspace),
        )
        assert "SMARTAGENT_RESULT_LOCAL_REF" in compact
        assert 'actionable_summary={' in compact
        assert '"status":"SEMANTIC_MAP_NOT_FRESH"' in compact
        assert '"needs_analysis_count":274' in compact

    with tempfile.TemporaryDirectory(prefix="smartagent-dead-end-route-") as temp:
        prompts: list[str] = []

        def planner(prompt: str, _expected: dict, _attachments: list[str]) -> str:
            prompts.append(prompt)
            if len(prompts) == 1:
                return _response(
                    _continuing_progress("P-DEAD-END-1"),
                    {
                        "tool": "propose_task_plan", "action_id": "PLAN-DEAD-END",
                        "workspace": temp,
                        "plan": {
                            "files_to_read": ["source/a.py"],
                            "edit_plan": {"files_to_modify": [{"path": "source/a.py"}]},
                        },
                    },
                )
            return _response(_continuing_progress(f"P-DEAD-END-{len(prompts)}"))

        loop = WebAgentProtocolLoop(temp, planner, progress_root=temp, max_turns=8)
        loop.tools.execute = lambda _action: json.dumps({
            "status": "SEMANTIC_MAP_NOT_FRESH",
            "required_semantic_paths": ["source/a.py"],
            "semantic_map_status": {
                "status": "MISSING", "snapshot_id": "SNAP-DEAD-END",
                "needs_analysis": ["source/a.py"], "needs_analysis_count": 1,
            },
        })
        try:
            loop.run("Modify the exact project file")
        except RuntimeError as exc:
            assert "未執行 Runtime 指定的 prerequisite action" in str(exc)
        else:
            raise AssertionError("dead-end recovery must pause after one bounded enforcement")
        assert len(prompts) == 3
        assert "WEBAGENT_DEAD_END_RECOVERY_REQUIRED" in prompts[2]

    print("RESULT_EVIDENCE_TO_ACTION_RECOVERY_OK")


if __name__ == "__main__":
    run()
