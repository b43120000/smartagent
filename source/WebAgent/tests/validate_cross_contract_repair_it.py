#!/usr/bin/env python3
"""Offline CCRC V1 regression: diagnostics, windows, handoff and no execution.

Run with PYTHONPATH=source. No real Worker, Planner, Git or Tool execution.
"""
from __future__ import annotations

import unittest

from agent_core.cross_contract_repair import (
    inspect_cross_contract, CrossContractRepairCoordinator,
    FIELD_REPAIR, ACTION_RECONSTRUCTION, REPLAN_REQUIRED,
    repair_instruction,
)
from WebAgent.protocol_loop import WebAgentProtocolLoop


def progress(next_action="run_command", *, action_id="PROGRESS-1"):
    return {"tool": "report_progress", "action_id": action_id,
            "decision": "CONTINUE", "next_action": next_action}


def command(action_id="D9-DOWNLOAD"):
    return {"tool": "run_command", "action_id": action_id,
            "operation": "DOWNLOAD_VERIFY", "command": "echo sensitive-command-value",
            "verify": {}, "success_criteria": ""}


def issues(*, structural=False):
    return [
        {"reason": "invalid_progress_payload",
         "field_issues": [{"field": "evidence_refs", "code": "bad_type"}],
         "command_repair": {
             "mode": "ACTION_RECONSTRUCTION" if structural else "FIELD_REPAIR",
             "field_issues": [
                 {"field": "operation", "code": "operation_not_in_registry"},
                 {"field": "verify", "code": "verify_required"},
             ],
         }},
    ]


class FakeLoop:
    _declared_next_tools = staticmethod(WebAgentProtocolLoop._declared_next_tools)

    def __init__(self):
        self.run_id = "RR-CCRC-01"
        self.task_epoch = "EPOCH-CCRC-01"
        self.turn_id = 10
        self.action_ledger = {}
        self.action_result_ledger = {}
        self.pending_command_field_repairs = {}
        self.deferred_action_repairs = {}
        self.pending_progress_repair_actions = []
        self.pending_action_repair = {}
        self.events = []
        self._ccrc = CrossContractRepairCoordinator(
            request_id=self.run_id, task_epoch=self.task_epoch,
            goal="Deploy fixture to original authorized directory",
        )

    def _emit(self, event, **fields):
        self.events.append((event, fields))


class CrossContractIT(unittest.TestCase):
    def setUp(self):
        self.calls = [progress(), command(), {"tool": "turn_commit", "action_count": 2}]

    def test_batch_command_progress_and_action_mismatch(self):
        sample = [progress("query_project"), command()]
        observation = inspect_cross_contract(
            sample, issues(), declared_tools=["query_project"],
        )
        self.assertTrue(observation["relevant"])
        origins = {x["origin"] for x in observation["field_issues"]}
        self.assertTrue({"ADMISSION", "PROGRESS", "COMMAND", "CONSISTENCY"} <= origins)
        self.assertEqual(observation["declared_next_tools"], ["query_project"])
        self.assertEqual(observation["actual_operational_tools"], ["run_command"])
        self.assertNotIn("sensitive-command-value", repr(observation))
        self.assertFalse(observation["authorization_granted"])
        self.assertFalse(observation["executed"])

    def test_two_field_rounds_one_reconstruction_then_replan(self):
        unit = CrossContractRepairCoordinator(
            request_id="RR1", task_epoch="EP1", goal="Goal fixed")
        observed = []
        for _ in range(4):
            observed.append(unit.observe(self.calls, issues(), declared_tools=["run_command"]))
        self.assertEqual([o["mode"] for o in observed], [
            FIELD_REPAIR, FIELD_REPAIR, ACTION_RECONSTRUCTION, REPLAN_REQUIRED,
        ])
        self.assertEqual(observed[-1]["field_rounds"], 2)
        self.assertEqual(observed[-1]["reconstruction_rounds"], 1)
        self.assertEqual(observed[-1]["rejected_rounds"], 4)
        self.assertFalse(observed[-1]["budget_reset"])
        self.assertFalse(observed[-1]["runtime_strategy_selected"])

    def test_new_action_id_does_not_reset_a_transaction(self):
        unit = CrossContractRepairCoordinator(
            request_id="RR1", task_epoch="EP1", goal="Goal fixed")
        for index in range(4):
            calls = [progress(), command(f"D9-{index}")]
            observed = unit.observe(calls, issues())
        self.assertEqual(observed["mode"], REPLAN_REQUIRED)
        self.assertEqual(observed["transaction_seq"], 1)
        self.assertEqual(observed["tracked_action_count"], 4)

    def test_reconstruction_immediate_for_semantic_command(self):
        unit = CrossContractRepairCoordinator(
            request_id="RR1", task_epoch="EP1", goal="Goal fixed")
        first = unit.observe(self.calls, issues(structural=True))
        second = unit.observe(self.calls, issues(structural=True))
        self.assertEqual(first["mode"], ACTION_RECONSTRUCTION)
        self.assertEqual(second["mode"], REPLAN_REQUIRED)
        self.assertIn("Goal-Anchored", repair_instruction(second))

    def test_progress_only_exhaustion_replans_without_ghost_action(self):
        unit = CrossContractRepairCoordinator(
            request_id="RR1", task_epoch="EP1", goal="Goal fixed")
        calls = [progress("query_project")]
        diag = [{"reason": "invalid_progress_payload",
                 "field_issues": [{"field": "outcome"}]}]
        decisions = [unit.observe(calls, diag, declared_tools=["query_project"])
                     for _ in range(3)]
        self.assertEqual([d["mode"] for d in decisions], [
            FIELD_REPAIR, FIELD_REPAIR, REPLAN_REQUIRED,
        ])
        self.assertEqual(decisions[-1]["reconstruction_rounds"], 0)

    def test_successful_full_admission_closes_transaction(self):
        unit = CrossContractRepairCoordinator(
            request_id="RR1", task_epoch="EP1", goal="Goal fixed")
        unit.observe(self.calls, issues())
        closed = unit.admitted()
        self.assertTrue(closed["admission_passed"])
        self.assertFalse(closed["budget_reset"])
        self.assertIsNone(unit.admitted())
        next_decision = unit.observe(self.calls, issues())
        self.assertEqual(next_decision["transaction_seq"], 2)
        self.assertEqual(next_decision["field_rounds"], 1)

    def test_integrated_handoff_releases_unexecuted_field_hold(self):
        host = FakeLoop()
        first = WebAgentProtocolLoop._ccrc_rejected(host, self.calls, issues())
        self.assertEqual(first["mode"], FIELD_REPAIR)
        pending_command = command()
        host.pending_command_field_repairs["D9-DOWNLOAD"] = {
            "action": pending_command,
            "request_id": host.run_id, "task_epoch": host.task_epoch,
        }
        host.turn_id += 1
        second = WebAgentProtocolLoop._ccrc_rejected(host, self.calls, issues())
        self.assertEqual(second["mode"], FIELD_REPAIR)
        host.turn_id += 1
        third = WebAgentProtocolLoop._ccrc_rejected(host, self.calls, issues())
        self.assertEqual(third["mode"], ACTION_RECONSTRUCTION)
        self.assertNotIn("D9-DOWNLOAD", host.pending_command_field_repairs)
        self.assertEqual(
            host.deferred_action_repairs["D9-DOWNLOAD"]["status"],
            "ACTION_RECONSTRUCTION_REQUIRED",
        )
        host.pending_progress_repair_actions = [dict(pending_command)]
        host.turn_id += 1
        # A different rejected ID must not create a second orphaned
        # deferred Action during Goal Replan handoff.
        changed_id_calls = [progress(), command("D9-UNRELATED-NEW-ID"),
                            {"tool": "turn_commit", "action_count": 2}]
        fourth = WebAgentProtocolLoop._ccrc_rejected(host, changed_id_calls, issues())
        self.assertEqual(fourth["mode"], REPLAN_REQUIRED)
        self.assertNotIn("D9-UNRELATED-NEW-ID", host.deferred_action_repairs)
        self.assertEqual(host.pending_progress_repair_actions, [])
        self.assertTrue(any(name == "ccrc_unadmitted_progress_hold_released"
                            for name, _ in host.events))
        self.assertFalse(any(name == "tool_started" for name, _ in host.events))
        self.assertNotIn(pending_command["command"],
                         str([data for name, data in host.events]))
        self.assertFalse(fourth["authorization_granted"])

    def test_unrelated_errors_do_not_open_transaction(self):
        coordinator = CrossContractRepairCoordinator(
            request_id="RR1", task_epoch="EP1", goal="Goal fixed")
        observed = coordinator.observe(
            [{"tool": "final_response", "action_id": "END1"}],
            [{"reason": "stale_transport_readback"}],
        )
        self.assertIsNone(observed)
        self.assertFalse(coordinator.active)


if __name__ == "__main__":
    unittest.main(verbosity=2)
