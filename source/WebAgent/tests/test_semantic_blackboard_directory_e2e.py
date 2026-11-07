"""Reproduce TASK-1D73... using the literal Traditional Chinese original Goal.

End-to-end local Runtime components: existing SCR translator -> SPR-bound
Goal target -> actual native inspect -> GoalGap -> DIR-001 -> Action Loop
-> canonical protocol + real native creation -> fact reconciliation -> Blackboard.
Never asserts that a Web UI/Telegram transport has been tested.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core.semantic_capability_resolution import (
    CAPABILITY_TO_TOOL, SCHEMA as SCR_SCHEMA,
    validate_action_vocabulary, translate_request_action,
    verify_resolution_for_actions, SemanticCapabilityError,
)
from agent_core.directory_goal_contract import bind_directory_goal_target
from agent_core.conditional_goal_policy import conditional_goal_policy
from agent_core.decision_rules.directory_workspace_rule import evaluate_directory_decision
from agent_core.goal_gap_ledger import reconstruct_goal_gaps
from agent_core.action_loop_state import build_action_loop_state
from agent_core.shared_blackboard import (
    build_shared_blackboard, read_action_loop_blackboard, build_strategy_candidate_registry,
    evaluate_terminal_readiness, validate_strategy_candidate_bindings,
)
from agent_core.smartagent_protocol import validate_tool_envelope
from agent_core.tools import execute_tool, preview_tool_scope
from agent_core.tool_result_contract import decode_result
from WebAgent.protocol_loop import WebAgentProtocolLoop

REQUEST = "RR-TELEGRAM-F07E199413472CCF"
TASK = "TASK-1D73A945513F4CABF694"
EPOCH = "EPOCH-TEST-DIRECTORY-TRANSLATION"


class OriginalGoalDirectoryIntegration(unittest.TestCase):
    def test_all_fixed_scr_vocabulary_has_canonical_tool(self):
        validate_action_vocabulary()
        self.assertEqual(CAPABILITY_TO_TOOL["directory.create"], "create_directory")
        for verb in ("創立", "建立", "創建", "新增", "新建"):
            translated = translate_request_action(
                verb + "這個資料夾\n" + r"E:\SmartAgent_GoalDriven_IT_GH_v1"
            )
            self.assertEqual(translated["selected_capability"], "directory.create")
            self.assertEqual(translated["canonical_tool"], "create_directory")
            self.assertFalse(translated["translation_is_authorization"])
            self.assertEqual(
                conditional_goal_policy(
                    verb + "這個資料夾\n" + r"E:\SmartAgent_GoalDriven_IT_GH_v1"
                )["intent"], "REMEDIATION_CANDIDATE",
            )
        self.assertEqual(
            conditional_goal_policy("不要創立這個資料夾")["intent"],
            "READ_ONLY_ONLY",
        )
        self.assertEqual(
            translate_request_action("只查詢資料夾")["selected_capability"],
            "",
        )  # no fixed positive inspection verb -> no invented intent

    def test_original_target_binding_not_deployment_contract(self):
        path = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
        goal = "創立這個資料夾\n" + path
        semantic = translate_request_action(goal)
        binding = bind_directory_goal_target(
            goal, [path], path_resolution={"status": "LEXICAL_EXACT", "paths": [path]},
            translation=semantic,
        )
        self.assertEqual(binding["binding_status"], "BOUND")
        self.assertEqual(binding["requested_target"], path)
        self.assertEqual(binding["reason_code"], "EXACT_USER_DIRECTORY_CREATION_TARGET")
        self.assertFalse(binding["authorization_granted"])
        other = bind_directory_goal_target(
            goal, [r"E:\Sibling"], translation=semantic,
        )
        self.assertEqual(other["binding_status"], "UNBOUND")
        many = bind_directory_goal_target(goal, [path, r"E:\Second"], translation=semantic)
        self.assertEqual(many["binding_status"], "UNBOUND")
        readonly = bind_directory_goal_target(
            "檢查這個資料夾\n" + path, [path],
        )
        self.assertEqual(readonly["binding_status"], "UNBOUND")

    def test_attested_translation_satisfies_only_matching_scr_action(self):
        previous = {"schema": SCR_SCHEMA, "capabilities": ["directory.inspect_metadata"]}
        native = {"tool": "create_directory", "path": r"E:\Goal", "action_id": "A-CREATE"}
        with self.assertRaises(SemanticCapabilityError):
            verify_resolution_for_actions(None, previous, [native])
        self.assertIsNotNone(verify_resolution_for_actions(
            None, previous, [native], attested_goal_capabilities=["directory.create"],
        ))
        with self.assertRaises(SemanticCapabilityError):
            verify_resolution_for_actions(
                None, previous, [{"tool": "find_file", "action_id": "A-UNRELATED"}],
                attested_goal_capabilities=["directory.create"],
            )

    def test_two_round_actual_native_inspection_creation_and_blackboard(self):
        with tempfile.TemporaryDirectory(prefix="goal-dir-e2e-") as base:
            root = Path(base).resolve()
            target = root / "SmartAgent_GoalDriven_IT_GH_v1"
            goal = "創立這個資料夾\n" + str(target)
            translation = translate_request_action(goal)
            binding = bind_directory_goal_target(
                goal, [str(target)],
                path_resolution={"status": "LEXICAL_EXACT", "paths": [str(target)]},
                translation=translation,
            )
            self.assertEqual(binding["binding_status"], "BOUND")

            loop = WebAgentProtocolLoop(str(root), lambda *_: "")
            loop.run_id, loop.task_id, loop.task_epoch = REQUEST, TASK, EPOCH
            loop.task_request = goal
            loop._semantic_action_translation = translation
            loop._directory_target_contract = binding
            loop._target_path_contract = None
            loop.tool_interface = "web_direct"
            loop.tools._authorized_local_paths = [str(target)]
            loop.tools.current_request_id = REQUEST
            loop.tools.current_task_id = TASK
            loop.tools.current_task_epoch = EPOCH
            loop.progress_ledger = SimpleNamespace(
                goal=goal, decision="CONTINUE", outcome="PENDING",
                current_step=1, total_steps=2, current_focus="create directory",
                next_action="create_directory", context_request={}, evidence_refs=[],
                selected_action="", matched_condition_id="",
                strategy_state={"revision": 1, "history": []},
            )
            inspect = {
                "tool": "inspect_directory", "action_id": "ACT-INSPECT-DIR-001",
                "paths": [str(target)], "recursive": False,
            }
            self.assertTrue(validate_tool_envelope(inspect)[0])
            assert not target.exists()
            observation = execute_tool(inspect, agent=loop.tools, models={})
            observed = decode_result(observation)
            self.assertEqual(observed["reason"], "DIRECTORY_ABSENCE_OBSERVED")
            self.assertEqual(observed["status"], "SUCCEEDED")
            loop.action_ledger[inspect["action_id"]] = {
                "round": 1, "request_id": REQUEST, "task_epoch": EPOCH,
                "action": inspect, "result": observation, "execution_status": "SUCCEEDED",
            }
            loop.action_result_ledger[inspect["action_id"]] = {
                "request_id": REQUEST, "task_epoch": EPOCH, "produced_round": 1,
                "tool": "inspect_directory", "result_status": "SUCCEEDED",
                "execution_status": "SUCCEEDED", "executed": True,
                "result_reason": "DIRECTORY_ABSENCE_OBSERVED",
                "evidence_status": "ACTIVE",
            }
            gaps = reconstruct_goal_gaps(loop)
            self.assertEqual(gaps["goal_policy"]["intent"], "REMEDIATION_CANDIDATE")
            self.assertEqual(gaps["active_count"], 1)
            self.assertEqual(gaps["active_gaps"][0]["observed_value"], False)

            decision = evaluate_directory_decision(loop, gaps)
            self.assertEqual(decision["decision"], "CREATE_DIRECTORY_REQUIRED")
            self.assertEqual(decision["candidate_status"], "UNTRIED", decision)
            loop.action_loop_state = build_action_loop_state(
                action=inspect, result=observation, execution_status="SUCCEEDED",
                goal_gap_view=gaps, directory_decision=decision,
                available_tools=["inspect_directory", "create_directory", "run_command"],
            )
            als = loop.action_loop_state
            self.assertEqual(als["evidence_state"], "OBSERVED_NEGATIVE")
            self.assertEqual(als["required_transition"], "REPLAN_REQUIRED")
            self.assertEqual(als["allowed_next_actions"], ["create_directory"])
            self.assertEqual(als["decision_basis"]["evidence_ref"], inspect["action_id"])
            registry = build_strategy_candidate_registry(
                loop, goal_gap_view=gaps, directory_decision=decision,
            )
            self.assertEqual(registry["candidate_count"], 1)
            self.assertFalse(evaluate_terminal_readiness(
                registry, request_id=REQUEST, task_epoch=EPOCH,
            )["interrupt_allowed"])
            create = {
                "tool": "create_directory", "action_id": "ACT-CREATE-DIR-002",
                "strategy_id": decision["candidate"]["strategy_id"], "path": str(target),
            }
            self.assertTrue(validate_tool_envelope(create)[0])
            self.assertIsNone(validate_strategy_candidate_bindings(
                [create], registry, request_id=REQUEST, task_epoch=EPOCH,
            ))
            self.assertIsNone(loop._normalize_action_loop_decision(
                {"decision": "CONTINUE", "outcome": "PENDING"}, [create],
            ))
            # Confirm actual canonical Tool Scope + native execution.
            self.assertTrue(preview_tool_scope(create, loop.tools)["allowed"])
            effect = execute_tool(create, agent=loop.tools, models={})
            receipt = decode_result(effect)
            self.assertEqual(receipt["status"], "SUCCEEDED", receipt)
            self.assertEqual(receipt["reason"], "DIRECTORY_CREATED")
            self.assertTrue(target.is_dir())
            loop.action_ledger[create["action_id"]] = {
                "round": 2, "request_id": REQUEST, "task_epoch": EPOCH,
                "action": create, "result": effect, "execution_status": "SUCCEEDED",
            }
            loop.action_result_ledger[create["action_id"]] = {
                "request_id": REQUEST, "task_epoch": EPOCH, "produced_round": 2,
                "tool": "create_directory", "result_status": "SUCCEEDED",
                "execution_status": "SUCCEEDED", "executed": True,
                "result_reason": "DIRECTORY_CREATED", "evidence_status": "ACTIVE",
            }
            new_gaps = reconstruct_goal_gaps(loop)
            self.assertEqual(new_gaps["active_count"], 0)
            self.assertTrue(new_gaps["gaps"][0]["observed_value"])
            next_decision = evaluate_directory_decision(loop, new_gaps)
            self.assertEqual(next_decision["decision"], "ENTER_EXISTING_DIRECTORY")

            loop.turn_id = 2
            board = build_shared_blackboard(loop, {
                "persistent_goal_gaps": new_gaps,
                "directory_target_binding": binding,
                "semantic_action_translation": translation,
                "directory_decision_rule": next_decision,
                "strategy_candidates": registry,
            })
            self.assertEqual(
                board["sections"]["goal"]["value"]["semantic_action_translation"][
                    "selected_capability"], "directory.create",
            )
            decision_event = next(
                event for event in board["event_projection"]
                if event["event_type"] == "ACTION_LOOP_DECISION_EVALUATED"
            )
            self.assertEqual(decision_event["evidence_ref"], inspect["action_id"])
            self.assertEqual(decision_event["allowed_next_actions"], ["create_directory"])
            self.assertEqual(decision_event["reason_code"],
                             "GOAL_DIRECTORY_ABSENCE_CONFIRMED")
            self.assertFalse(decision_event["authorization_granted"])
            shared_loop = read_action_loop_blackboard(
                board, request_id=REQUEST, task_epoch=EPOCH,
            )
            self.assertEqual(shared_loop["status"], "BOUND")
            self.assertEqual(shared_loop["state_id"], als["state_id"])
            self.assertEqual(shared_loop["allowed_next_actions"], ["create_directory"])
            self.assertEqual(read_action_loop_blackboard(
                board, request_id="RR-WRONG", task_epoch=EPOCH,
            )["status"], "UNAVAILABLE")
            # A legal ActionLoop update after Planner Context is projected
            # must trigger a bounded Blackboard refresh, not a false denial.
            loop._last_shared_blackboard_snapshot = board
            loop.action_loop_state = build_action_loop_state()
            diagnostic = loop._normalize_action_loop_decision(
                {"decision": "CONTINUE", "outcome": "PENDING"}, [create],
            )
            # Reconciliation is allowed, but repeating a successful
            # creation must still be governed by the *current* Action Loop.
            self.assertNotEqual(
                (diagnostic or {}).get("reason"),
                "action_loop_blackboard_state_drift_unreconciled",
            )
            refreshed = read_action_loop_blackboard(
                loop._last_shared_blackboard_snapshot,
                request_id=REQUEST, task_epoch=EPOCH,
            )
            self.assertEqual(refreshed["status"], "BOUND")
            self.assertEqual(refreshed["state_id"], loop.action_loop_state["state_id"])


if __name__ == "__main__":
    unittest.main()
