#!/usr/bin/env python3
"""Goal-driven Plan Readiness: 3-way routing, evidence, safe admission and replan."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_core.capability_recovery import build_result_evidence_to_action_route
from agent_core.edit_plan_contract import REQUIRED_PLAN_FIELDS
from agent_core.plan_readiness import (
    DISCOVER, DETERMINISTIC, REPAIR, SCHEMA, classify_plan_readiness,
    transition_discovery_from_observation,
)
from agent_core.project_sync import inspect_project_scope
from agent_core.task_plan import (
    TASK_PLAN_SCHEMA, canonicalize_task_plan, repair_task_plan, validate_task_plan,
)
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.tests.validate_task_plan_schema_recovery import _progress


def check() -> None:
    with tempfile.TemporaryDirectory(prefix="gdpr-it-") as temp:
        workspace = Path(temp).resolve()
        (workspace / "source.cpp").write_text("int main(){return 0;}\n", encoding="utf-8")
        snapshot = inspect_project_scope(workspace)
        plan = {
            "schema": TASK_PLAN_SCHEMA,
            "base_snapshot_id": snapshot["snapshot_id"],
            "semantic_map_revision": "",
            "goal": "Create a bounded status note",
            "affected_flows": [], "files_to_read": [],
            "edit_plan": {
                "files_to_modify": [{
                    "path": "status.txt", "create": True, "mode": "whole_file",
                    "content": "ok\n", "modification_intent": "Write status note",
                }]
            },
            "verification_commands": ["python -c \"print('ok')\""],
            "acceptance_criteria": ["status.txt exists"],
            "rollback_condition": "Verification failed",
            "post_change_semantic": [],
        }
        action = {"tool": "propose_task_plan", "action_id": "GD-1",
                  "workspace": str(workspace), "plan": plan}
        # Missing field can be derived only from existing acceptance criteria.
        missing_derived = validate_task_plan(workspace, plan)
        assert missing_derived["reason"] == "INVALID_EDIT_PLAN", missing_derived
        assert missing_derived["edit_plan_validation"]["missing_fields"] == [
            "expected_observable_result"], missing_derived
        fixed, transforms = canonicalize_task_plan(plan)
        readiness = classify_plan_readiness(plan, fixed, missing_derived, transforms)
        assert readiness["readiness"] == DETERMINISTIC, readiness
        fixed_response = build_result_evidence_to_action_route(
            action, json.dumps(missing_derived), project_root_hint=str(workspace))
        assert fixed_response["readiness"] == DETERMINISTIC
        assert fixed_response["required_action"] == "repair_task_plan"
        assert fixed_response["next_actions"][0]["plan"]["edit_plan"][
            "expected_observable_result"] == "status.txt exists"
        assert repair_task_plan(workspace, plan)["status"] == "PLAN_FROZEN"

        # Missing concrete modification targets cannot be invented from goal.
        incomplete = {**plan, "edit_plan": {}}
        broken = validate_task_plan(workspace, incomplete)
        assert broken["reason"] == "INVALID_EDIT_PLAN", broken
        assert set(broken["edit_plan_validation"]["missing_fields"]) == {
            "files_to_modify", "expected_observable_result"}
        request = {**action, "action_id": "GD-2", "plan": incomplete}
        recovery = build_result_evidence_to_action_route(
            request, json.dumps(broken), project_root_hint=str(workspace))
        assert recovery["readiness"] == DISCOVER, recovery
        assert recovery["missing_fields"] == ["edit_plan.files_to_modify"], recovery
        assert recovery["required_action"] == ""
        assert recovery["next_actions"] == [
            {"tool": "inspect_project_scope", "workspace": str(workspace)}], recovery
        assert not recovery["pause_immediately"] and not recovery["prerequisite_satisfied"]

        # Admission: stale plan and unrelated observation are rejected.
        loop = WebAgentProtocolLoop(workspace, lambda *_: "")
        loop.run_id = "RR-GDPR-IT"
        loop.task_id = "TASK-GDPR-IT"
        loop.task_epoch = "EPOCH-GDPR-IT"
        loop.intent_digest = "d" * 64
        loop.pending_result_recovery = recovery
        denied, diagnostic = loop._accept_ack([
            _progress("P-1", "propose_task_plan"), request,
            {"tool": "turn_commit", "action_count": 2},
        ], {})
        assert denied == [] and diagnostic[0]["reason"] == (
            "task_plan_discovery_observation_required"), diagnostic
        unrelated = {"tool": "inspect_project_scope", "action_id": "GD-OTHER",
                     "workspace": str(workspace.parent)}
        denied, diagnostic = loop._accept_ack([
            _progress("P-2", "inspect_project_scope"), unrelated,
            {"tool": "turn_commit", "action_count": 2},
        ], {})
        assert denied == [] and diagnostic[0]["reason"] == (
            "task_plan_discovery_observation_required"), diagnostic
        observation = {**recovery["next_actions"][0], "action_id": "GD-OBS"}
        accepted, diagnostic = loop._accept_ack([
            _progress("P-3", "inspect_project_scope"), observation,
            {"tool": "turn_commit", "action_count": 2},
        ], {})
        assert diagnostic == [], diagnostic
        assert [x["tool"] for x in accepted] == [
            "report_progress", "inspect_project_scope"]

        # Bad or fabricated receipt cannot unlock a new plan.
        invalid = transition_discovery_from_observation(
            observation, {"status": "SUCCEEDED"}, recovery)
        assert invalid["pause_immediately"]
        assert invalid["reason"] == "task_plan_discovery_unverified"
        verified = transition_discovery_from_observation(
            observation, inspect_project_scope(workspace), recovery)
        assert verified["readiness"] == "EVIDENCE_READY", verified
        assert verified["prerequisite_satisfied"] and not verified["pause_immediately"]
        assert verified["last_inventory_digest"]
        assert verified["next_actions"] == []
        assert not verified["authorization_granted"] if "authorization_granted" in verified else True

        loop.pending_result_recovery = verified
        # After real evidence, fresh complete PLAN may be proposed.
        complete = {**plan, "edit_plan": {
            **plan["edit_plan"], "expected_observable_result": "status.txt exists"}}
        proposed = {**action, "action_id": "GD-FRESH", "plan": complete}
        allowed, diag = loop._accept_ack([
            _progress("P-4", "propose_task_plan"), proposed,
            {"tool": "turn_commit", "action_count": 2},
        ], {})
        assert diag == [], diag
        assert [x["tool"] for x in allowed] == ["report_progress", "propose_task_plan"]
        no_progress = build_result_evidence_to_action_route(
            request, json.dumps(broken),
            project_root_hint=str(workspace), prior_context=verified)
        assert no_progress["reason"] == "task_plan_replan_unresolved"
        assert no_progress["pause_immediately"] and no_progress["next_actions"] == []

        # Missing target is a prerequisite, malformed but present shape is
        # ordinary schema repair; existing strict repair regression still applies.
        malformed = {**plan, "schema": "SMARTAGENT_INSTANCE_AWARE_PLAN_V1"}
        validated = validate_task_plan(workspace, malformed)
        norm, repairs = canonicalize_task_plan(malformed)
        assert classify_plan_readiness(
            malformed, norm, validated, repairs)["readiness"] == REPAIR
        assert SCHEMA == "SMARTAGENT_PLAN_READINESS_V1"
        assert "files_to_modify" in REQUIRED_PLAN_FIELDS
    print("GOAL_DRIVEN_PLAN_READINESS_IT_PASS")


if __name__ == "__main__":
    check()
