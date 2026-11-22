"""DIR-001 deterministic directory routing and native Tool Action tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core.decision_rules.directory_workspace_rule import (
    RULE_ID, SCHEMA, evaluate_directory_decision,
)
from agent_core.shared_blackboard import (
    build_runtime_capability_manifest, build_strategy_candidate_registry,
    evaluate_terminal_readiness, interrupt_terminal_diagnostic,
    validate_strategy_candidate_bindings,
)
from agent_core.tool_result_contract import decode_result
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.goal_strategy_transition import classify_runtime_evidence
from agent_core.goal_gap_ledger import reconstruct_goal_gaps
from agent_core.tools import execute_tool, preview_tool_scope

REQUEST = "RR-DIR-001-TEST"
TASK = "TASK-DIR-001-TEST"
EPOCH = "EPOCH-DIR-001-TEST"


class PreviewTools:
    def __init__(self, *, allowed=True):
        self.allowed = allowed

    def preview_tool_scope(self, action):
        return {
            "allowed": self.allowed,
            "resolved_paths": [action["path"]] if self.allowed else [],
            "scope_diagnostic": {
                "reason_code": "SCOPE_ALLOWED" if self.allowed else "PATH_NOT_AUTHORIZED",
            },
        }


def runtime_for(path: str, *, allowed=True, intent="CONDITIONAL_REMEDIATION"):
    target_contract = {
        "binding_status": "BOUND",
        "requested_target": path,
    }
    return SimpleNamespace(
        run_id=REQUEST, task_id=TASK, task_epoch=EPOCH,
        task_request="If target missing, create the folder and work inside.",
        tool_interface="web_direct",
        _target_path_contract=target_contract,
        tools=PreviewTools(allowed=allowed),
        action_ledger={}, action_result_ledger={},
        _rejected_action_resolution_traces=[],
        progress_ledger=SimpleNamespace(goal="Create directory " + path,
                                        strategy_state={"revision": 1, "history": []}),
        _source_discovery_bootstrap={},
        directory_policy_intent=intent,
    )


def gap_for(path: str, *, exists=None, intent="CONDITIONAL_REMEDIATION"):
    row = {
        "path": path, "gap_id": "GAP-DIR-001",
        "observation_status": "CONFIRMED",
        "observed_value": exists,
        "observed_fact_ref": "ACT-INSPECT-001",
        "status": "RESOLVED" if exists else "UNRESOLVED",
    }
    return {
        "goal_policy": {"intent": intent, "restrictions": ["NO_OVERWRITE"]},
        "gaps": [row] if type(exists) is bool else [],
        "active_gaps": [row] if exists is False else [],
        "source_requirement": "REQUIRED",
    }


class DirectoryDecisionRuleTests(unittest.TestCase):
    def test_missing_observation_requests_one_exact_inspection(self):
        target = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
        row = evaluate_directory_decision(runtime_for(target),
                                          gap_for(target, exists=None))
        self.assertEqual(row["schema"], SCHEMA)
        self.assertEqual(row["rule_id"], RULE_ID)
        self.assertEqual(row["decision"], "INSPECT_DIRECTORY_REQUIRED")
        self.assertEqual(row["next_action"], "INSPECT_DIRECTORY")
        self.assertFalse(row["authorization_granted"])

    def test_absent_target_prepares_native_creation_without_cwd_or_source_guess(self):
        target = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
        runtime = runtime_for(target)
        gap = gap_for(target, exists=False)
        row = evaluate_directory_decision(runtime, gap)
        self.assertEqual(row["decision"], "CREATE_DIRECTORY_REQUIRED")
        self.assertEqual(row["next_action"], "CREATE_DIRECTORY")
        candidate = row["candidate"]
        self.assertEqual(candidate["status"], "UNTRIED")
        self.assertEqual(candidate["tool"], "create_directory")
        self.assertEqual(candidate["target"], target)
        self.assertEqual(candidate["working_directory"], "")
        self.assertEqual(candidate["target_absence_evidence_ref"], "ACT-INSPECT-001")
        self.assertFalse(candidate["authorization_granted"])
        registry = build_strategy_candidate_registry(
            runtime, goal_gap_view=gap, directory_decision=row,
        )
        self.assertEqual(registry["candidate_count"], 1)
        ready = evaluate_terminal_readiness(
            registry, request_id=REQUEST, task_epoch=EPOCH,
        )
        self.assertFalse(ready["interrupt_allowed"])
        self.assertEqual(ready["decision"], "REPLAN_REQUIRED")
        self.assertEqual(
            interrupt_terminal_diagnostic(ready)["reason"],
            "goal_relevant_strategy_not_exhausted",
        )
        action = {
            "tool": "create_directory", "action_id": "ACT-CREATE-001",
            "strategy_id": candidate["strategy_id"],
            "path": target,
        }
        good, err = validate_tool_envelope(action)
        self.assertTrue(good, err)
        self.assertIsNone(validate_strategy_candidate_bindings(
            [action], registry, request_id=REQUEST, task_epoch=EPOCH,
        ))
        alternate = {**action, "path": target + r"\unexpected"}
        diag = validate_strategy_candidate_bindings(
            [alternate], registry, request_id=REQUEST, task_epoch=EPOCH,
        )
        self.assertEqual(diag["reason"], "strategy_candidate_directory_target_mismatch")

    def test_present_target_enters_work_without_creating_again(self):
        target = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
        row = evaluate_directory_decision(runtime_for(target),
                                          gap_for(target, exists=True))
        self.assertEqual(row["decision"], "ENTER_EXISTING_DIRECTORY")
        self.assertEqual(row["next_action"], "WORK_IN_EXISTING_DIRECTORY")
        self.assertEqual(row["candidate"], {})

    def test_read_only_intent_never_prepares_creation(self):
        target = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
        row = evaluate_directory_decision(runtime_for(target),
                gap_for(target, exists=False, intent="READ_ONLY_ONLY"))
        self.assertEqual(row["decision"], "READ_ONLY")
        self.assertEqual(row["candidate"], {})

    def test_unverified_scope_does_not_authorize_create(self):
        target = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
        row = evaluate_directory_decision(runtime_for(target, allowed=False),
                                          gap_for(target, exists=False))
        self.assertEqual(row["candidate_status"], "NEEDS_USER_AUTHORIZATION")
        self.assertFalse(row["candidate"]["blocks_interrupt"])
        self.assertFalse(row["authorization_granted"])

    def test_unbound_target_cannot_infer_creation_from_goal_prose(self):
        target = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
        runtime = runtime_for(target)
        runtime._target_path_contract["binding_status"] = "NOT_BOUND"
        row = evaluate_directory_decision(runtime, gap_for(target, exists=False))
        self.assertEqual(row["decision"], "NOT_APPLICABLE")
        self.assertEqual(row["candidate"], {})

    def test_native_create_directory_exact_scope_idempotence_and_verification(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            target = workspace / "new_goal"
            agent = SimpleNamespace(
                workspace_root=str(workspace),
                _authorized_local_paths=(str(target),),
                current_request_id=REQUEST, current_task_id=TASK,
                _request_write_ceiling=None,
            )
            action = {
                "tool": "create_directory",
                "action_id": "ACT-NATIVE-CREATE-001",
                "path": str(target),
            }
            preview = preview_tool_scope(action, agent)
            self.assertTrue(preview["allowed"], preview)
            self.assertEqual(len(preview["resolved_paths"]), 1)
            # Windows may resolve 8.3 short names into long paths.
            self.assertEqual(Path(preview["resolved_paths"][0]).resolve(),
                             target.resolve())
            result = execute_tool(action, agent=agent, models={})
            receipt = decode_result(result)
            self.assertEqual(receipt["tool"], "create_directory")
            self.assertEqual(receipt["status"], "SUCCEEDED")
            self.assertEqual(receipt["reason"], "DIRECTORY_CREATED")
            self.assertEqual(receipt["action_id"], action["action_id"])
            self.assertTrue(receipt["executed"])
            self.assertTrue(receipt["data"]["observed_is_directory"])
            self.assertTrue(target.is_dir())
            classified = classify_runtime_evidence({
                "tool": "create_directory", "result_status": "SUCCEEDED",
                "executed": True, "evidence_status": "ACTIVE",
            }, {"action": action, "result": result})
            self.assertEqual(classified["environment_observation_kind"],
                             "directory.exists")
            self.assertTrue(classified["environment_observed_exists"])
            runtime = runtime_for(str(target))
            runtime.action_ledger[action["action_id"]] = {
                "action": action, "result": result,
            }
            runtime.action_result_ledger[action["action_id"]] = {
                "tool": "create_directory", "result_status": "SUCCEEDED",
                "executed": True, "evidence_status": "ACTIVE",
                "request_id": REQUEST, "task_epoch": EPOCH,
            }
            latest = reconstruct_goal_gaps(runtime)
            self.assertTrue(latest["gaps"][0]["observed_value"])
            row = evaluate_directory_decision(runtime, latest)
            self.assertEqual(row["decision"], "ENTER_EXISTING_DIRECTORY")
            self.assertEqual(row["candidate"], {})
            repeat = decode_result(execute_tool(action, agent=agent, models={}))
            self.assertEqual(repeat["status"], "REJECTED")
            self.assertEqual(repeat["reason"], "DIRECTORY_ALREADY_EXISTS_REOBSERVE")
            self.assertTrue(target.is_dir())

    def test_native_create_no_sibling_or_ancestor_scope_grant(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            target = workspace / "allowed"
            sibling = workspace / "disallowed"
            agent = SimpleNamespace(
                workspace_root=str(workspace),
                _authorized_local_paths=(str(target),),
                current_request_id=REQUEST, current_task_id=TASK,
                _request_write_ceiling=None,
            )
            action = {"tool": "create_directory", "action_id": "ACT-SCOPE-002",
                      "path": str(sibling)}
            preview = preview_tool_scope(action, agent)
            self.assertFalse(preview["allowed"])
            result = decode_result(execute_tool(action, agent=agent, models={}))
            self.assertEqual(result["status"], "REJECTED")
            self.assertFalse(result["executed"])
            self.assertFalse(sibling.exists())


if __name__ == "__main__":
    unittest.main()
