"""Live Task-1D73... two-decision regression, cross-platform and native.

The original user command (創立這個資料夾) drives translation and target
binding. No test fixture supplies a fake positive Goal intent. Actual native
inspect/create actions run in a disposable temp directory.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core.semantic_capability_resolution import (
    action_semantic_registry, translate_goal_action,
    validate_action_semantic_registry,
)
from agent_core.conditional_goal_policy import conditional_goal_policy
from agent_core.semantic_path_resolution import readonly_narrowing_allowed
from agent_core.target_path_contract import extract_target_contract
from agent_core.action_loop_state import build_action_loop_state
from agent_core.decision_rules.directory_workspace_rule import evaluate_directory_decision
from agent_core.goal_gap_ledger import reconstruct_goal_gaps
from agent_core.shared_blackboard import (
    build_shared_blackboard, build_strategy_candidate_registry,
    evaluate_terminal_readiness, validate_strategy_candidate_bindings,
)
from agent_core.smartagent_protocol import (
    TOOL_ENVELOPE_SCHEMAS, validate_tool_envelope,
)
from agent_core.tools import execute_tool, preview_tool_scope
from agent_core.tool_result_contract import decode_result
from agent_core.tool_capabilities import get_allowed_tools

REQUEST = "RR-TELEGRAM-F07E199413472CCF"
TASK = "TASK-1D73A945513F4CABF694"
EPOCH = "EPOCH-LIVE-REGRESSION-001"
WINDOWS_TARGET = r"E:\SmartAgent_GoalDriven_IT_GH_v1"


class RealNativeTools:
    def __init__(self, agent):
        self.agent = agent

    def preview_tool_scope(self, action):
        return preview_tool_scope(action, self.agent)


class DirectorySemanticBlackboardTests(unittest.TestCase):
    def test_live_original_chinese_binding_and_shared_alias_registry(self):
        request = "創立這個資料夾\n" + WINDOWS_TARGET
        semantic = translate_goal_action(request)
        self.assertEqual(semantic["status"], "MATCHED")
        self.assertEqual(semantic["canonical_tool"], "create_directory")
        self.assertEqual(semantic["semantic_intent"], "CREATE_DIRECTORY")
        self.assertEqual(conditional_goal_policy(request)["intent"],
                         "REMEDIATION_CANDIDATE")
        self.assertFalse(readonly_narrowing_allowed(request, [WINDOWS_TARGET]))
        contract = extract_target_contract(request)
        self.assertEqual(contract["binding_status"], "BOUND")
        self.assertEqual(contract["requested_target"], WINDOWS_TARGET)
        self.assertEqual(contract["target_kind"], "DIRECTORY_ONLY")
        self.assertEqual(contract["canonical_tool"], "create_directory")
        self.assertFalse(contract["authorization_granted"])

    def test_registered_verbs_share_one_tool_and_no_implicit_authorization(self):
        for verb in ("創立", "建立", "創建", "新增"):
            with self.subTest(verb=verb):
                row = translate_goal_action(verb + "這個資料夾")
                self.assertEqual(row["canonical_tool"], "create_directory")
                self.assertFalse(row["semantic_translation_is_not_authorization"] is False)
                self.assertTrue(row["tool_admission_required"])
        for negative in ("不要創立資料夾", "禁止建立資料夾", "不新增資料夾"):
            with self.subTest(negative=negative):
                row = translate_goal_action(negative)
                self.assertNotEqual(row["canonical_tool"], "create_directory")
        self.assertEqual(translate_goal_action("建立新的 API")["status"], "UNMAPPED")
        self.assertEqual(translate_goal_action("創立資料夾並刪除檔案")["status"],
                         "AMBIGUOUS")

    def test_all_envelope_tools_have_one_canonical_semantic_descriptor(self):
        validate_action_semantic_registry()
        registry = action_semantic_registry()
        self.assertEqual(set(registry), set(TOOL_ENVELOPE_SCHEMAS))
        self.assertEqual(registry["create_directory"]["capability"], "directory.create")
        self.assertIn("創立", registry["create_directory"]["fixed_aliases"])
        for tool, row in registry.items():
            with self.subTest(tool=tool):
                self.assertTrue(row["capability"])
                self.assertTrue(row["tool_admission_required"])
                self.assertTrue(row["aliases_are_non_authorizing"])

    def test_two_decision_rounds_real_native_creation_and_shared_blackboard(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            target = workspace / "SmartAgent_GoalDriven_IT_GH_v1"
            original = "創立這個資料夾\n" + str(target)
            contract = extract_target_contract(original)
            self.assertIsNotNone(contract)
            self.assertEqual(contract["binding_status"], "BOUND")
            self.assertEqual(contract["requested_target"], str(target))
            agent = SimpleNamespace(
                workspace_root=str(workspace),
                _authorized_local_paths=(str(target),),
                current_request_id=REQUEST,
                current_task_id=TASK,
                _request_write_ceiling=None,
            )
            runtime = SimpleNamespace(
                run_id=REQUEST, task_id=TASK, task_epoch=EPOCH, turn_id=2,
                task_request=original, tool_interface="web_direct",
                _target_path_contract=contract,
                workspace=str(workspace),
                authorized_paths=[str(target)],
                tools=RealNativeTools(agent),
                action_ledger={}, action_result_ledger={},
                _rejected_action_resolution_traces=[],
                _source_discovery_bootstrap={},
                progress_ledger=SimpleNamespace(
                    goal=original, decision="CONTINUE", outcome="PENDING",
                    strategy_state={"revision": 1, "history": []},
                ),
            )
            self.assertFalse(target.exists())
            inspect = {
                "tool": "inspect_directory",
                "action_id": "inspect-dir23-001",
                "paths": [str(target)],
            }
            okay, error = validate_tool_envelope(inspect)
            self.assertTrue(okay, error)
            raw_inspect = execute_tool(inspect, agent=agent, models={})
            observed = decode_result(raw_inspect)
            self.assertEqual(observed["status"], "SUCCEEDED")
            self.assertEqual(observed["reason"], "DIRECTORY_ABSENCE_OBSERVED")
            runtime.action_ledger[inspect["action_id"]] = {
                "round": 1, "action": inspect, "result": raw_inspect,
                "execution_status": "SUCCEEDED",
            }
            runtime.action_result_ledger[inspect["action_id"]] = {
                "request_id": REQUEST, "task_epoch": EPOCH,
                "result_status": "SUCCEEDED", "execution_status": "SUCCEEDED",
                "result_reason": "DIRECTORY_ABSENCE_OBSERVED",
                "evidence_status": "ACTIVE", "executed": True,
            }

            gaps = reconstruct_goal_gaps(runtime)
            from agent_core.semantic_path_resolution import same_original_request_path
            self.assertEqual(gaps["active_count"], 1, {
                "gaps": gaps,
                "bound_target": contract["requested_target"],
                "native_observed_result": observed,
                "path_link_check": [
                    {
                        "observed_path": row["path"],
                        "same": same_original_request_path(
                            contract["requested_target"], row["path"]),
                        "parent_same": (
                            Path(contract["requested_target"]).parent.resolve(strict=False)
                            == Path(row["path"]).parent.resolve(strict=False)
                        ),
                    } for row in gaps["gaps"]
                ],
            })
            self.assertEqual(gaps["known_absences"][0]["path"], str(target))
            self.assertEqual(gaps["goal_policy"]["intent"], "REMEDIATION_CANDIDATE")
            rule = evaluate_directory_decision(runtime, gaps)
            self.assertEqual(rule["decision"], "CREATE_DIRECTORY_REQUIRED")
            self.assertEqual(rule["candidate_status"], "UNTRIED")
            als = build_action_loop_state(
                progress={"decision": "CONTINUE", "outcome": "PENDING"},
                action=inspect, result=raw_inspect,
                execution_status="SUCCEEDED", verification_status="UNKNOWN",
                goal_gap_view=gaps, directory_decision=rule,
                available_tools=sorted(get_allowed_tools("web_direct")),
            )
            self.assertEqual(als["evidence_state"], "OBSERVED_NEGATIVE")
            self.assertEqual(als["allowed_next_actions"], ["create_directory"])
            self.assertEqual(als["decision_rule_id"], "DIR-001")
            self.assertEqual(als["decision_reason_code"],
                             "GOAL_DIRECTORY_ABSENCE_CONFIRMED")
            self.assertEqual(als["decision_evidence_ref"], inspect["action_id"])
            runtime.action_loop_state = als

            registry = build_strategy_candidate_registry(
                runtime, goal_gap_view=gaps, directory_decision=rule,
            )
            self.assertEqual(registry["candidate_count"], 1)
            candidate = registry["candidates"][0]
            self.assertEqual(candidate["route_id"], "GOAL_DIRECTORY_CREATE")
            self.assertFalse(evaluate_terminal_readiness(
                registry, request_id=REQUEST, task_epoch=EPOCH,
            )["interrupt_allowed"])
            create = {
                "tool": "create_directory", "action_id": "create-dir23-001",
                "strategy_id": candidate["strategy_id"],
                "path": str(target),
            }
            okay, error = validate_tool_envelope(create)
            self.assertTrue(okay, error)
            self.assertIsNone(validate_strategy_candidate_bindings(
                [create], registry, request_id=REQUEST, task_epoch=EPOCH,
            ))
            self.assertTrue(runtime.tools.preview_tool_scope(create)["allowed"])

            board = build_shared_blackboard(runtime, {
                "action_loop_decision": {
                    "state_id": als["state_id"],
                    "required_transition": als["required_transition"],
                    "allowed_next_actions": als["allowed_next_actions"],
                    "evidence_state": als["evidence_state"],
                    "decision_rule_id": als["decision_rule_id"],
                    "decision_reason_code": als["decision_reason_code"],
                    "decision_evidence_ref": als["decision_evidence_ref"],
                    "decision_strategy_id": als["decision_strategy_id"],
                    "decision_target": als["decision_target"],
                    "decision_source": als["decision_source"],
                },
                "action_semantic_translation":
                    gaps["goal_policy"]["semantic_action_translation"],
                "directory_decision_rule": rule,
                "strategy_candidates": registry,
                "persistent_goal_gaps": gaps,
                "action_loop_state": als,
            })
            events = [e for e in board["event_projection"]
                      if e["event_type"] == "ACTION_LOOP_DECISION_EVALUATED"]
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["decision_rule_id"], "DIR-001")
            self.assertEqual(event["observed_evidence_ref"], inspect["action_id"])
            self.assertEqual(event["allowed_next_actions"], ["create_directory"])
            self.assertFalse(event["authorization_granted"])
            self.assertEqual(
                board["sections"]["actions"]["value"]["action_loop_decision"]["state_id"],
                als["state_id"],
            )
            self.assertEqual(
                board["sections"]["capabilities"]["value"]
                ["action_semantic_translation"]["canonical_tool"],
                "create_directory",
            )

            created_raw = execute_tool(create, agent=agent, models={})
            created = decode_result(created_raw)
            self.assertEqual(created["status"], "SUCCEEDED")
            self.assertEqual(created["reason"], "DIRECTORY_CREATED")
            self.assertTrue(target.is_dir())
            runtime.action_ledger[create["action_id"]] = {
                "round": 2, "action": create, "result": created_raw,
                "execution_status": "SUCCEEDED",
            }
            runtime.action_result_ledger[create["action_id"]] = {
                "request_id": REQUEST, "task_epoch": EPOCH,
                "result_status": "SUCCEEDED", "execution_status": "SUCCEEDED",
                "evidence_status": "ACTIVE", "executed": True,
            }
            newer = reconstruct_goal_gaps(runtime)
            self.assertEqual(newer["active_count"], 0)
            rule2 = evaluate_directory_decision(runtime, newer)
            self.assertEqual(rule2["decision"], "ENTER_EXISTING_DIRECTORY")
            self.assertEqual(rule2["next_action"], "WORK_IN_EXISTING_DIRECTORY")
            self.assertEqual(rule2["candidate"], {})
            self.assertEqual(build_strategy_candidate_registry(
                runtime, goal_gap_view=newer, directory_decision=rule2,
            )["candidate_count"], 0)

    def test_unconfirmed_fact_cannot_enable_creation(self):
        runtime = SimpleNamespace(
            run_id=REQUEST, task_id=TASK, task_epoch=EPOCH, turn_id=2,
            _target_path_contract=extract_target_contract(
                "創立這個資料夾\n" + WINDOWS_TARGET,
            ),
            tools=SimpleNamespace(preview_tool_scope=lambda _: {
                "allowed": True, "resolved_paths": [WINDOWS_TARGET],
            }),
            tool_interface="web_direct",
        )
        absent_no_evidence = {
            "goal_policy": conditional_goal_policy(
                "創立這個資料夾\n" + WINDOWS_TARGET),
            "gaps": [{
                "path": WINDOWS_TARGET, "status": "UNRESOLVED",
                "observed_value": False, "observed_fact_ref": "",
                "observation_status": "CONFIRMED",
            }],
        }
        row = evaluate_directory_decision(runtime, absent_no_evidence)
        self.assertNotEqual(row["decision"], "CREATE_DIRECTORY_REQUIRED")


if __name__ == "__main__":
    unittest.main()
