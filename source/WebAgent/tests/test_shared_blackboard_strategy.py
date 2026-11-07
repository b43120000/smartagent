"""Shared Blackboard, Runtime capability and strategy exhaustion regressions.

Offline and side-effect free apart from temporary TaskProgress test files.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from agent_core.action_loop_state import semantic_action_signature
from agent_core.shared_blackboard import (
    CANDIDATE_SCHEMA,
    SCHEMA,
    build_runtime_capability_manifest,
    build_shared_blackboard,
    build_strategy_candidate_registry,
    evaluate_terminal_readiness,
    interrupt_terminal_diagnostic,
    validate_strategy_candidate_bindings,
)
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.task_progress import (
    initialize_progress, record_runtime_strategy_revision,
)

REQUEST = "RR-SHARED-BLACKBOARD-001"
TASK = "TASK-SHARED-BLACKBOARD-001"
EPOCH = "EPOCH-SHARED-BLACKBOARD-001"
GOAL = (
    "Download the registered source ZIP from GitHub and deploy it to "
    r"E:\SmartAgent_GoalDriven_IT_GH_v1 without overwriting existing files."
)
TARGET = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
PARENT = r"E:\"
ARTIFACTS = {
    "artifact_references": [{
        "kind": "REMOTE_ARTIFACT_REFERENCE",
        "candidate_id": "E1-CASE-D",
        "repository": "b43120000/test",
        "commit_sha": "6b0d680185e40ae654acef03d0eeb9074536fb8e",
        "artifact_path": "goal-driven-replan/v1/SmartAgent_GoalDriven_IT_fixture.zip",
    }]
}
GAPS = {
    "active_gaps": [{
        "path": TARGET,
        "status": "UNRESOLVED",
        "observation_status": "CONFIRMED",
        "observed_value": False,
        "observed_fact_ref": "FACT-TARGET-ABSENT-001",
    }]
}


class FakeTools:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.preview_calls = []

    def preview_tool_scope(self, action):
        self.preview_calls.append(dict(action))
        path = action.get("working_directory") or action.get("path")
        if not self.allowed:
            return {
                "allowed": False,
                "error": "command_working_directory_outside_request_scope",
                "resolved_paths": [],
            }
        return {"allowed": True, "resolved_paths": [path]}


class FakeRuntime:
    def __init__(self, progress, *, scope_allowed=True):
        self.progress_ledger = progress
        self.run_id = REQUEST
        self.task_id = TASK
        self.task_epoch = EPOCH
        self.turn_id = 4
        self.task_request = GOAL
        self.workspace = PARENT
        self.authorized_paths = [PARENT, TARGET]
        self.tools = FakeTools(scope_allowed)
        self.action_ledger = {}
        self.action_result_ledger = {}
        self._rejected_action_resolution_traces = []
        self._source_discovery_bootstrap = ARTIFACTS


def create_runtime(root: str | Path, *, scope_allowed=True):
    progress = initialize_progress(TASK, request_id=REQUEST, goal=GOAL, root=root)
    evidence = {
        "schema": "SMARTAGENT_PREEXECUTION_REPLAN_V1",
        "request_id": REQUEST,
        "task_epoch": EPOCH,
        "rejected_action_id": "ACT-ORIGINAL-CWD-001",
        "admission_reason": "command_working_directory_not_directory",
        "target": TARGET,
        "target_exists": False,
        "existing_writable_parent": PARENT,
        "scope_verified": True,
        "executed": False,
        "authorization_granted": False,
        "evidence_digest": hashlib.sha256(b"attested-preexecution-replan").hexdigest(),
        "routes": [
            {
                "id": "NATIVE_FILE_CREATION",
                "tool": "write_file",
                "rule": "Use write_file only for an exact requested file.",
            },
            {
                "id": "EXISTING_CWD_CREATION",
                "tool": "run_command",
                "rule": "Use a new action from an existing CWD; normal admission remains mandatory.",
            },
        ],
    }
    progress, changed = record_runtime_strategy_revision(
        TASK, request_id=REQUEST, evidence=evidence,
        round_id=3, root=root,
    )
    if not changed:
        raise AssertionError("test strategy revision was not persisted")
    return FakeRuntime(progress, scope_allowed=scope_allowed)


def build_registry(runtime, *, remote_state="AUTHORIZED", gaps=GAPS):
    manifest = build_runtime_capability_manifest(runtime)
    registry = build_strategy_candidate_registry(
        runtime,
        capability_manifest=manifest,
        prerequisite_view={"source_requirement": "REQUIRED"},
        remote_access_view={
            "authorization_state": remote_state,
            "git_executable_status": "AVAILABLE",
            "registered_artifact_count": 1,
        },
        artifact_view=ARTIFACTS,
        goal_gap_view=gaps,
    )
    return manifest, registry


class SharedBlackboardTests(unittest.TestCase):
    def test_runtime_capability_manifest_describes_tools_not_grants(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = create_runtime(temp)
            manifest = build_runtime_capability_manifest(runtime)
        self.assertEqual(
            manifest["schema"], "SMARTAGENT_RUNTIME_CAPABILITY_MANIFEST_V1"
        )
        self.assertIn("run_command", manifest["allowed_tools"])
        command = next(row for row in manifest["tools"] if row["tool"] == "run_command")
        self.assertEqual(command["side_effect_class"], "OPERATION_DEPENDENT")
        self.assertTrue(command["normal_tool_admission_required"])
        self.assertTrue(manifest["capability_is_not_authorization"])
        self.assertFalse(manifest["scope_grants_created"])
        self.assertFalse(manifest["network_authorization_granted"])

    def test_runtime_attested_route_is_persisted_and_becomes_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = create_runtime(temp)
            persisted = runtime.progress_ledger.strategy_state["history"][-1]
            self.assertEqual(
                [row["route_id"] for row in persisted["routes"]],
                ["NATIVE_FILE_CREATION", "EXISTING_CWD_CREATION"],
            )
            manifest, registry = build_registry(runtime)
        self.assertEqual(registry["schema"], CANDIDATE_SCHEMA)
        self.assertEqual(registry["source_requirement"], "REQUIRED")
        self.assertEqual(registry["candidate_count"], 1)
        candidate = registry["candidates"][0]
        self.assertEqual(candidate["route_id"], "EXISTING_CWD_CREATION")
        self.assertEqual(candidate["tool"], "run_command")
        self.assertEqual(candidate["operation"], "GIT_MUTATE")
        self.assertEqual(candidate["working_directory"], PARENT)
        self.assertEqual(candidate["target"], TARGET)
        self.assertEqual(candidate["target_absence_evidence_ref"], "FACT-TARGET-ABSENT-001")
        self.assertEqual(candidate["working_directory_scope_preflight"], "PASS")
        self.assertEqual(candidate["status"], "UNTRIED")
        self.assertFalse(candidate["authorization_granted"])
        self.assertFalse(candidate["execution_authorized"])
        self.assertFalse(candidate["candidate_is_executable_action"])
        self.assertEqual(len(runtime.tools.preview_calls), 1)

    def test_untried_candidate_blocks_interrupt_until_strategy_is_attempted(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = create_runtime(temp)
            _, registry = build_registry(runtime)
        readiness = evaluate_terminal_readiness(
            registry, request_id=REQUEST, task_epoch=EPOCH,
        )
        self.assertFalse(readiness["interrupt_allowed"])
        self.assertEqual(readiness["decision"], "REPLAN_REQUIRED")
        self.assertEqual(
            readiness["reason_code"], "GOAL_RELEVANT_STRATEGY_CANDIDATE_NOT_ATTEMPTED"
        )
        diagnostic = interrupt_terminal_diagnostic(readiness)
        self.assertEqual(diagnostic["reason"], "goal_relevant_strategy_not_exhausted")
        self.assertFalse(diagnostic["authorization_granted"])

    def test_scope_rejection_turns_candidate_into_user_input_not_false_availability(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = create_runtime(temp, scope_allowed=False)
            _, registry = build_registry(runtime)
        candidate = registry["candidates"][0]
        self.assertEqual(candidate["status"], "NEEDS_USER_AUTHORIZATION")
        self.assertFalse(candidate["blocks_interrupt"])
        readiness = evaluate_terminal_readiness(
            registry, request_id=REQUEST, task_epoch=EPOCH,
        )
        self.assertTrue(readiness["interrupt_allowed"])
        self.assertEqual(readiness["reason_code"], "REMAINING_STRATEGIES_REQUIRE_USER_INPUT")

    def test_missing_current_target_absence_does_not_offer_stale_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = create_runtime(temp)
            _, registry = build_registry(runtime, gaps={"active_gaps": []})
        candidate = registry["candidates"][0]
        self.assertEqual(candidate["status"], "NEEDS_TARGET_OBSERVATION")
        self.assertFalse(candidate["blocks_interrupt"])

    def test_unavailable_scope_preflight_holds_terminal_until_reconciled(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = create_runtime(temp)
            runtime.tools = object()
            _, registry = build_registry(runtime)
        candidate = registry["candidates"][0]
        self.assertEqual(
            candidate["status"], "RUNTIME_PREFLIGHT_RECONCILIATION_REQUIRED"
        )
        readiness = evaluate_terminal_readiness(
            registry, request_id=REQUEST, task_epoch=EPOCH,
        )
        self.assertFalse(readiness["interrupt_allowed"])
        self.assertEqual(
            readiness["reason_code"], "STRATEGY_SCOPE_PREFLIGHT_UNRESOLVED"
        )

    def test_remote_authorization_unknown_does_not_claim_runnable_strategy(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = create_runtime(temp)
            _, registry = build_registry(runtime, remote_state="UNKNOWN")
        candidate = registry["candidates"][0]
        self.assertEqual(candidate["status"], "NEEDS_REMOTE_ACCESS_EVIDENCE")
        self.assertFalse(candidate["blocks_interrupt"])

    def test_strategy_id_binds_action_to_exact_runtime_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = create_runtime(temp)
            _, registry = build_registry(runtime)
        candidate = registry["candidates"][0]
        action = {
            "tool": "run_command",
            "action_id": "ACT-CANDIDATE-001",
            "strategy_id": candidate["strategy_id"],
            "operation": "GIT_MUTATE",
            "command": "git clone https://github.com/b43120000/test.git SmartAgent_GoalDriven_IT_GH_v1",
            "working_directory": PARENT,
        }
        self.assertIsNone(validate_strategy_candidate_bindings(
            [action], registry, request_id=REQUEST, task_epoch=EPOCH,
        ))
        wrong_cwd = {**action, "working_directory": TARGET}
        diagnostic = validate_strategy_candidate_bindings(
            [wrong_cwd], registry, request_id=REQUEST, task_epoch=EPOCH,
        )
        self.assertEqual(
            diagnostic["reason"], "strategy_candidate_working_directory_mismatch"
        )
        wrong_target = {
            **action,
            "command": "git clone https://github.com/b43120000/test.git unrelated-target",
        }
        diagnostic = validate_strategy_candidate_bindings(
            [wrong_target], registry, request_id=REQUEST, task_epoch=EPOCH,
        )
        self.assertEqual(
            diagnostic["reason"], "strategy_candidate_target_mismatch"
        )

    def test_candidate_matching_action_requires_explicit_strategy_id(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = create_runtime(temp)
            _, registry = build_registry(runtime)
        action = {
            "tool": "run_command",
            "action_id": "ACT-CANDIDATE-NO-ID",
            "operation": "GIT_MUTATE",
            "command": "git clone https://github.com/b43120000/test.git SmartAgent_GoalDriven_IT_GH_v1",
            "working_directory": PARENT,
        }
        diagnostic = validate_strategy_candidate_bindings(
            [action], registry, request_id=REQUEST, task_epoch=EPOCH,
        )
        self.assertEqual(diagnostic["reason"], "strategy_candidate_id_required")
        self.assertFalse(diagnostic["authorization_granted"])

    def test_strategy_id_is_validated_as_non_authorizing_action_metadata(self):
        valid, diagnostic = validate_tool_envelope({
            "tool": "run_command",
            "action_id": "ACT-STRATEGY-ID-001",
            "strategy_id": "STRAT-0123456789ABCDEF0123",
            "operation": "GIT_MUTATE",
            "command": "git clone https://github.com/b43120000/test.git SmartAgent_GoalDriven_IT_GH_v1",
        })
        self.assertTrue(valid, diagnostic)
        valid, diagnostic = validate_tool_envelope({
            "tool": "run_command",
            "action_id": "ACT-STRATEGY-ID-002",
            "strategy_id": "STRAT-not-a-valid-id",
            "operation": "GIT_MUTATE",
            "command": "git clone https://github.com/b43120000/test.git SmartAgent_GoalDriven_IT_GH_v1",
        })
        self.assertFalse(valid)
        self.assertEqual(diagnostic["reason"], "strategy_candidate_reference_invalid")

    def test_strategy_id_does_not_change_semantic_action_identity(self):
        action = {
            "tool": "run_command",
            "action_id": "ACT-1",
            "operation": "GIT_MUTATE",
            "command": "git clone https://github.com/b43120000/test.git SmartAgent_GoalDriven_IT_GH_v1",
            "working_directory": PARENT,
        }
        tagged = {**action, "strategy_id": "STRAT-0123456789ABCDEF0123"}
        self.assertEqual(semantic_action_signature(action), semantic_action_signature(tagged))

    def test_rejected_candidate_requires_user_input_and_exhausts_only_that_route(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = create_runtime(temp)
            _, first = build_registry(runtime)
            candidate = first["candidates"][0]
            runtime._rejected_action_resolution_traces = [{
                "request_id": REQUEST,
                "task_epoch": EPOCH,
                "round": 5,
                "action_id": "ACT-CANDIDATE-REJECTED",
                "planner_requested": {
                    "strategy_id": candidate["strategy_id"],
                    "tool": "run_command",
                    "operation": "GIT_MUTATE",
                    "working_directory": PARENT,
                    "command": "git clone https://github.com/b43120000/test.git SmartAgent_GoalDriven_IT_GH_v1",
                },
                "runtime_resolved": {
                    "disposition": "REJECTED_PREEXECUTION",
                    "reason_codes": ["run_command_working_directory_rejected"],
                },
            }]
            _, retried = build_registry(runtime)
        self.assertEqual(retried["candidates"][0]["status"], "NEEDS_USER_AUTHORIZATION")
        readiness = evaluate_terminal_readiness(
            retried, request_id=REQUEST, task_epoch=EPOCH,
        )
        self.assertTrue(readiness["interrupt_allowed"])

    def test_schema_rejection_keeps_same_candidate_repairable(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = create_runtime(temp)
            _, first = build_registry(runtime)
            candidate = first["candidates"][0]
            runtime._rejected_action_resolution_traces = [{
                "request_id": REQUEST,
                "task_epoch": EPOCH,
                "round": 5,
                "action_id": "ACT-CANDIDATE-SCHEMA-REJECTED",
                "planner_requested": {
                    "strategy_id": candidate["strategy_id"],
                    "tool": "run_command",
                    "operation": "GIT_MUTATE",
                    "working_directory": PARENT,
                    "command": "git clone https://github.com/b43120000/test.git SmartAgent_GoalDriven_IT_GH_v1",
                },
                "runtime_resolved": {
                    "disposition": "REJECTED_PREEXECUTION",
                    "reason_codes": ["run_command_verification_contract_required"],
                },
            }]
            _, retried = build_registry(runtime)
        self.assertEqual(retried["candidates"][0]["status"], "NEEDS_ACTION_REPAIR")
        self.assertFalse(evaluate_terminal_readiness(
            retried, request_id=REQUEST, task_epoch=EPOCH,
        )["interrupt_allowed"])

    def test_shared_blackboard_is_owner_tagged_and_contains_actual_event_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = create_runtime(temp)
            manifest, registry = build_registry(runtime)
            terminal = evaluate_terminal_readiness(
                registry, request_id=REQUEST, task_epoch=EPOCH,
            )
            payload = {
                "goal": {"status": "INCOMPLETE", "strategy_revision": 2},
                "goal_transition_readiness": {"status": "INCOMPLETE"},
                "persistent_goal_gaps": GAPS,
                "observed_runtime_facts": [{
                    "evidence_ref": "FACT-TARGET-ABSENT-001",
                    "state": "ABSENT",
                    "probe_status": "CONFIRMED",
                }],
                "environment_facts": [],
                "runtime_software_inventory": {},
                "runtime_capability_manifest": manifest,
                "scope": {"authorized_paths": [PARENT, TARGET]},
                "remote_artifact_access_readiness": {
                    "authorization_state": "AUTHORIZED",
                    "git_executable_status": "AVAILABLE",
                },
                "deployment_path_prerequisites": {},
                "recent_action_evidence": [],
                "recent_action_provenance": [],
                "action_resolution_trace": [],
                "action_loop_blockers": [],
                "failure_causal_receipts": [],
                "condition_evidence": [],
                "evidence_total": 0,
                "strategy_candidates": registry,
                "prerequisite_transition_gate": {},
                "goal_prerequisite_resolution": {},
                "dynamic_progress_contract": {},
                "terminal_readiness": terminal,
            }
            board = build_shared_blackboard(runtime, payload)
        self.assertEqual(board["schema"], SCHEMA)
        self.assertEqual(board["identity"]["request_id"], REQUEST)
        self.assertEqual(board["identity"]["task_epoch"], EPOCH)
        self.assertTrue(board["snapshot_id"].startswith("BB-"))
        self.assertEqual(
            board["sections"]["capabilities"]["owner"],
            "RUNTIME_CAPABILITY_REGISTRY",
        )
        self.assertEqual(
            board["sections"]["authorization"]["owner"],
            "SECURITY_CONTEXT_AND_REMOTE_ACCESS",
        )
        self.assertEqual(
            board["sections"]["strategy"]["owner"],
            "STRATEGY_CANDIDATE_ENGINE",
        )
        self.assertTrue(board["invariants"]["candidate_is_not_authorization"])
        self.assertTrue(board["invariants"]["planner_proposal_is_not_runtime_evidence"])
        self.assertIn(
            "STRATEGY_REVISION_RECORDED",
            [row["event_type"] for row in board["event_projection"]],
        )
        self.assertFalse(board["raw_tool_results_included"])
        self.assertFalse(board["raw_commands_included"])


if __name__ == "__main__":
    unittest.main()
