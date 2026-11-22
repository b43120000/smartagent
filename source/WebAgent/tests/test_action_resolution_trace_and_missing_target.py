"""Action resolution regression: missing deployment CWD and 3-way provenance."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core.action_resolution_trace import (
    SCHEMA, PREREQ_SCHEMA,
    bounded_action_trace_projection, confirmed_target_absence,
    deployment_path_prerequisites, exact_remote_git_read_repository,
    executed_trace, propose_equivalent_bound_cwd_rewrite, rejection_traces,
    registered_remote_git_read,
)
from agent_core.action_dependencies import command_is_proven_observation
from agent_core.runtime_fact_ledger import build_action_provenance
from agent_core.tool_result_contract import attach_runtime_result
from agent_core.security_context import SecurityContext
from agent_core.tools import preview_tool_scope, resolve_command_working_directory

REPO = "b43120000/test"
PROBE = "git ls-remote https://github.com/b43120000/test.git HEAD"


class ActionResolutionTraceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name) / "existing"
        self.workspace.mkdir()
        self.target = self.workspace / "deployment"  # deliberately absent
        self.catalog = {
            "artifact_references": [{
                "kind": "REMOTE_ARTIFACT_REFERENCE",
                "repository": REPO,
                "candidate_id": "ART-TEST",
            }]
        }
        self.contract = {
            "binding_status": "BOUND", "requested_target": str(self.target),
        }
        self.gaps = {
            "active_gaps": [{
                "path": str(self.target), "status": "UNRESOLVED",
                "observation_status": "CONFIRMED", "observed_value": False,
                "observed_fact_ref": "ACT-OBSERVED-ABSENT",
            }]
        }
        self.action = {
            "tool": "run_command", "operation": "GIT_INSPECT",
            "action_id": "ACT-GIT-1", "command": PROBE,
            "working_directory": str(self.target),
        }
        self.agent = SimpleNamespace(
            workspace_root=self.workspace,
            _authorized_local_paths=[str(self.target)],
            _registered_remote_artifact_view=self.catalog,
        )

    def test_absence_is_first_class_prerequisite_but_not_write_grant(self):
        view = deployment_path_prerequisites(self.contract, self.gaps)
        self.assertEqual(view["schema"], PREREQ_SCHEMA)
        self.assertEqual(view["target_observation_status"], "CONFIRMED_ABSENT")
        self.assertTrue(view["missing_target_is_not_valid_command_cwd"])
        self.assertTrue(view["git_clone_destination_may_be_absent"])
        self.assertTrue(view["git_clone_requires_existing_authorized_writable_parent"])
        self.assertFalse(view["absence_does_not_authorize_creating_target"] is False)
        self.assertEqual(
            confirmed_target_absence(self.gaps, str(self.target)),
            "ACT-OBSERVED-ABSENT",
        )
        self.assertEqual(
            confirmed_target_absence({"active_gaps": []}, str(self.target)), "",
        )

    def test_git_read_cwd_rewrite_only_after_scope_preview_and_absence_evidence(self):
        rejected = preview_tool_scope(self.action, self.agent)
        self.assertFalse(rejected["allowed"])
        self.assertIn("command_working_directory_not_directory", rejected["error"])
        proposal = propose_equivalent_bound_cwd_rewrite(
            self.action, original_cwd_error=rejected["error"],
            bound_workspace=str(self.workspace),
            target_contract=self.contract, goal_gaps=self.gaps,
            artifact_view=self.catalog,
        )
        self.assertIsNotNone(proposal)
        effective = proposal["candidate_action"]
        self.assertEqual(effective["command"], self.action["command"])
        self.assertEqual(effective["operation"], self.action["operation"])
        self.assertEqual(effective["working_directory"], str(self.workspace))
        self.assertEqual(proposal["rewrite_changed_fields"], ["working_directory"])
        self.assertFalse(proposal["authorization_granted"])
        admitted = preview_tool_scope(effective, self.agent)
        self.assertTrue(admitted["allowed"], admitted)
        resolved_cwd = resolve_command_working_directory(
            str(self.workspace),
            SecurityContext.for_workspace(
                self.workspace, read_roots=(self.target,)
            ),
            self.agent, operation="GIT_INSPECT", command=PROBE,
        )
        # Windows GitHub runners can spell the same folder via 8.3
        # (RUNNER~1) and long (runneradmin) forms.
        self.assertTrue(resolved_cwd.samefile(self.workspace))
        self.assertFalse(self.target.exists(), "rewrite must never create destination")

    def test_live_shape_parent_drive_request_scope_blocker(self):
        target = r"E:\\SmartAgent_GoalDriven_IT_GH_v1"
        contract = {"binding_status":"BOUND", "requested_target":target}
        gaps = {"active_gaps":[{
            "path":target,"status":"UNRESOLVED",
            "observation_status":"CONFIRMED","observed_value":False,
            "observed_fact_ref":"ACT-OBSERVED-TARGET-ABSENT",
        }]}
        attempted = {
            **self.action,
            "working_directory": r"E:\\",
        }
        candidate = propose_equivalent_bound_cwd_rewrite(
            attempted,
            original_cwd_error=(
                "command_working_directory_outside_request_scope:E:\\\\"
            ),
            bound_workspace=str(self.workspace),
            target_contract=contract, goal_gaps=gaps,
            artifact_view=self.catalog,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["rewrite_reason_code"],
                         "DEPLOYMENT_PARENT_OUTSIDE_COMMAND_REQUEST_SCOPE")
        self.assertEqual(candidate["candidate_action"]["command"], PROBE)
        self.assertEqual(candidate["candidate_action"]["working_directory"],
                         str(self.workspace))
        self.assertFalse(candidate["authorization_granted"])

    def test_unknown_git_url_or_unverified_absence_never_rewrites_scope(self):
        self.assertTrue(registered_remote_git_read(self.action, self.catalog))
        self.assertEqual(exact_remote_git_read_repository(PROBE), REPO)
        self.assertTrue(command_is_proven_observation(self.action))
        rejected = preview_tool_scope(self.action, self.agent)
        cases = [
            {**self.action, "command": PROBE + "; whoami"},
            {**self.action, "command": PROBE.replace(REPO, "other/repo")},
            {**self.action, "operation": "GIT_MUTATE",
             "command": "git clone https://github.com/b43120000/test.git"},
        ]
        for row in cases:
            self.assertIsNone(propose_equivalent_bound_cwd_rewrite(
                row, original_cwd_error=rejected["error"],
                bound_workspace=str(self.workspace),
                target_contract=self.contract, goal_gaps=self.gaps,
                artifact_view=self.catalog,
            ))
        self.assertIsNone(propose_equivalent_bound_cwd_rewrite(
            self.action, original_cwd_error=rejected["error"],
            bound_workspace=str(self.workspace),
            target_contract=self.contract,
            goal_gaps={"active_gaps":[]},
            artifact_view=self.catalog,
        ))
        self.assertFalse(preview_tool_scope(
            {**self.action, "command": PROBE.replace(REPO, "other/repo"),
             "working_directory": str(self.workspace)}, self.agent,
        )["allowed"])

    def test_rejected_proposal_has_admission_evidence_not_execution_evidence(self):
        rows = rejection_traces(
            [self.action],
            [{"reason": "run_command_working_directory_rejected",
              "detail": "command_working_directory_not_directory:" + str(self.target)}],
            original_actions={"ACT-GIT-1": self.action},
            request_id="R1", task_epoch="E1", round_id=2,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schema"], SCHEMA)
        self.assertEqual(rows[0]["runtime_resolved"]["disposition"],
                         "REJECTED_PREEXECUTION")
        self.assertIsNone(rows[0]["execution"]["dispatched_action"])
        self.assertFalse(rows[0]["execution"]["executed"])
        self.assertFalse(rows[0]["runtime_resolved"]["authorization_granted"])
        self.assertTrue(rows[0]["execution"]["admission_diagnostic_ref"])
        self.assertFalse(rows[0]["execution"]["execution_evidence_ref"])

    def test_rejected_rewrite_candidate_not_mislabeled_as_dispatched(self):
        new = {**self.action, "working_directory": str(self.workspace)}
        rows = rejection_traces(
            [new], [{"reason": "command_security_rejected"}],
            original_actions={"ACT-GIT-1": self.action},
            rewrite_notes={"ACT-GIT-1": {
                "rewrite_reason_code": "KNOWN_ABSENT_DEPLOYMENT_TARGET_NOT_COMMAND_CWD",
                "rewrite_changed_fields": ["working_directory"],
            }},
            request_id="R1", task_epoch="E1", round_id=2,
        )
        self.assertEqual(rows[0]["runtime_resolved"]["disposition"],
                         "REWRITE_CANDIDATE_NOT_ADMITTED")
        self.assertEqual(rows[0]["runtime_resolved"]["changed_fields"],
                         ["working_directory"])
        self.assertIsNone(rows[0]["runtime_resolved"]["effective_action"])
        self.assertEqual(rows[0]["execution"]["status"], "NOT_EXECUTED")

    def test_real_planner_context_includes_undispatched_action_and_known_absence(self):
        from agent_core.planner_context_snapshot import build_planner_context_snapshot
        from WebAgent.tests.validate_planner_context_bridge_it import FakeRuntime
        from WebAgent.tests.test_goal_grounded_gap_p0_p3 import (
            directory_record,
        )
        from agent_core.target_path_contract import extract_target_contract
        root = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
        goal = (
            "請完成 Case D 部署。\n預期部署根目錄：\n" + root
            + "\n預期包含：case_a、case_b、case_c、fixture_manifest.json、check_results.py。"
            + "\n若缺少則補齊，不得覆蓋。"
        )
        fake = FakeRuntime()
        fake.task_request = goal
        fake.progress_ledger.goal = goal
        fake._target_path_contract = extract_target_contract(goal)
        fake.run_id = "RR-CASE-D"
        fake.task_epoch = "EPOCH-D"
        action_row, result_row = directory_record("ACT-CONFIRMED-ABSENT", exists=False)
        result_row["request_id"] = fake.run_id
        result_row["task_epoch"] = fake.task_epoch
        fake.action_ledger["ACT-CONFIRMED-ABSENT"] = action_row
        fake.action_result_ledger["ACT-CONFIRMED-ABSENT"] = result_row
        fake._rejected_action_resolution_traces = rejection_traces(
            [self.action],
            [{"reason": "run_command_working_directory_rejected",
              "detail": "command_working_directory_not_directory:" + str(self.target)}],
            original_actions={"ACT-GIT-1": self.action},
            request_id=fake.run_id, task_epoch=fake.task_epoch,
            round_id=3,
        )
        snapshot = build_planner_context_snapshot(fake)
        self.assertEqual(
            snapshot["deployment_path_prerequisites"]["target_observation_status"],
            "CONFIRMED_ABSENT",
        )
        self.assertEqual(
            snapshot["deployment_path_prerequisites"]["target_absence_evidence_ref"],
            "ACT-CONFIRMED-ABSENT",
        )
        self.assertEqual(
            snapshot["action_resolution_trace"][-1]["execution"]["status"],
            "NOT_EXECUTED",
        )
        self.assertEqual(
            snapshot["action_resolution_trace"][-1]["planner_requested"]["command"],
            PROBE,
        )
        self.assertEqual(
            snapshot["action_resolution_trace"][-1]["runtime_resolved"]["reason_codes"],
            ["run_command_working_directory_rejected"],
        )

    def test_executed_provenance_preserves_exact_source_and_effective_action(self):
        effective = {**self.action, "working_directory": str(self.workspace)}
        actual = attach_runtime_result(
            "Git remote read completed", "SUCCEEDED", "COMMAND_EXITED",
            tool="run_command", executed=True,
            data={"exit_code":0,"process_state":"EXITED"},
        )
        provenance = build_action_provenance(
            self.action, effective, effective, actual,
            request_id="R1", task_epoch="E1", round_id=3,
        )
        view = executed_trace(provenance, rewrite_info={
            "rewrite_changed_fields":["working_directory"],
            "rewrite_reason_code":"KNOWN_ABSENT_DEPLOYMENT_TARGET_NOT_COMMAND_CWD",
        }, request_id="R1", task_epoch="E1")
        self.assertEqual(view["runtime_resolved"]["disposition"],
                         "REWRITTEN_AND_DISPATCHED")
        self.assertEqual(view["planner_requested"]["working_directory"], str(self.target))
        self.assertEqual(view["execution"]["dispatched_action"]["working_directory"],
                         str(self.workspace))
        self.assertEqual(view["execution"]["evidence_type"], "ACTUAL_TOOL_RESULT")
        self.assertTrue(view["execution"]["executed"])
        self.assertEqual(view["execution"]["status"], "SUCCEEDED")
        projected = bounded_action_trace_projection(
            [], {"ACT-GIT-1": {"provenance": provenance}},
            rewrite_ledger={"ACT-GIT-1": {
                "rewrite_changed_fields":["working_directory"],
                "rewrite_reason_code":"KNOWN_ABSENT_DEPLOYMENT_TARGET_NOT_COMMAND_CWD",
            }},
            request_id="R1", task_epoch="E1",
        )
        self.assertEqual(projected[0]["action_id"], "ACT-GIT-1")
        self.assertEqual(bounded_action_trace_projection(
            [], {"ACT-GIT-1": {"provenance": provenance}},
            rewrite_ledger={}, request_id="R1", task_epoch="wrong",
        ), [])


if __name__ == "__main__":
    unittest.main()
