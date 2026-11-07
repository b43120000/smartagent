"""Git26: current-epoch prerequisite evidence gates Progress/INTERRUPT.

Offline, no GitHub request, no E: drive access, no mutations or approval grants.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.prerequisite_transition_gate import (
    SCHEMA, build_prerequisite_transition, premature_interrupt_diagnostic,
)
from agent_core.goal_prerequisite_resolution import (
    resolve_goal_prerequisites, premature_source_interrupt_diagnostic,
)
from agent_core.remote_artifact_access_readiness import (
    RECEIPT_SCHEMA, resolve_remote_artifact_access,
)
from agent_core.unified_contract_transaction import build_transaction

ARTIFACT = {"artifact_references": [{
    "kind": "REMOTE_ARTIFACT_REFERENCE",
    "repository": "b43120000/test",
    "candidate_id": "E1-CASE-D",
}]}
GAP = {
    "source_requirement": "REQUIRED",
    "goal_policy": {"intent": "CONDITIONAL_REMEDIATION"},
    "active_gaps": [{
        "gap_id": "GOAL-D", "path": r"E:\SmartAgent_GoalDriven_IT_GH_v1",
        "status": "UNRESOLVED", "observation_status": "CONFIRMED",
        "observed_value": False, "desired_value": True,
        "observed_fact_ref": "D3-INSPECT-ROOT",
    }],
}
ENV = {"tools": {"git": {"status": "AVAILABLE", "version": "2.56.0"}}}
REQ = "RR-G26-CASE-D"
EPOCH = "EPOCH-G26"
USER_REQ = {"kind": "USER_INPUT", "detail":
            "需要 GitHub 私人儲存庫唯讀連線授權，以及現有 working_directory 路徑"}


def setup_view(receipt=None, env=None):
    remote = resolve_remote_artifact_access(
        ARTIFACT, environment=ENV if env is None else env,
        runtime_receipt=receipt, request_id=REQ, task_epoch=EPOCH,
    )
    prereq = resolve_goal_prerequisites(
        GAP, {"candidate_reads": []}, {"candidates": []}, {"sources": []},
        ARTIFACT, remote,
    )
    return remote, prereq


def fixed_scope_preview(action):
    assert action["tool"] == "run_command"
    assert action["operation"] == "GIT_INSPECT"
    assert action["command"] == (
        "git ls-remote https://github.com/b43120000/test.git HEAD"
    )
    return {"allowed": True,
            "resolved_paths": [action["working_directory"]]}


def transition(*, remote=None, prereq=None, preview=fixed_scope_preview,
               guidance=False, actions=None, evidence=None, epoch=EPOCH):
    if remote is None or prereq is None:
        remote, prereq = setup_view()
    return build_prerequisite_transition(
        artifact_view=ARTIFACT, prerequisite_view=prereq, remote_view=remote,
        request_id=REQ, task_epoch=epoch,
        bound_workspace="/verified/agent/workspace",
        tool_scope_preview=preview,
        action_ledger=actions, result_ledger=evidence,
        guidance_issued=guidance,
    )


class PrerequisiteTransitionGateTests(unittest.TestCase):
    def test_case_d_known_absence_known_artifact_known_git_scope_pass_requires_probe(self):
        view = transition()
        self.assertEqual(view["schema"], SCHEMA)
        self.assertTrue(view["required"])
        self.assertEqual(view["stage"], "READY_FOR_TOOL_ADMISSION")
        self.assertEqual(view["scope_preflight_status"], "PASS")
        self.assertEqual(view["access_state"], "UNKNOWN")
        self.assertEqual(view["progress_transition"],
                         "CONTINUE_PROPOSE_EXACT_GIT_READ_ACTION")
        self.assertEqual(view["probe_action"]["working_directory"],
                         "/verified/agent/workspace")
        self.assertEqual(view["probe_action"]["operation"], "GIT_INSPECT")
        self.assertFalse(view["probe_evidence_present"])
        self.assertFalse(view["authorization_granted"])
        self.assertFalse(view["action_dispatched"])
        self.assertFalse(view["scope_preflight_is_network_authorization"])
        diagnostic = premature_interrupt_diagnostic(USER_REQ, view)
        self.assertEqual(diagnostic["reason"],
                         "remote_prerequisite_probe_not_executed")
        self.assertEqual(diagnostic["prerequisite_transition"]["stage"],
                         "READY_FOR_TOOL_ADMISSION")
        self.assertIn("decision=CONTINUE", diagnostic["suggestion"])
        self.assertIn("full", diagnostic["suggestion"].lower())

    def test_issued_guidance_is_never_probe_evidence_round2_to_round4(self):
        remote, prereq = setup_view()
        original = premature_source_interrupt_diagnostic(USER_REQ, prereq)
        self.assertEqual(original["reason"],
                         "remote_access_unknown_git_capability_probe_required")
        prereq["remote_access_probe_guidance_already_issued"] = True
        self.assertIsNone(premature_source_interrupt_diagnostic(USER_REQ, prereq))
        # This was the Case D Git25 bypass: the one-time hint stopped but
        # NOTHING had been executed. The new hard transition must persist.
        for round_number in (2, 3, 4):
            with self.subTest(round_number=round_number):
                view = transition(remote=remote, prereq=prereq, guidance=True)
                self.assertEqual(view["stage"], "READY_FOR_TOOL_ADMISSION")
                self.assertFalse(view["guidance_is_evidence"])
                self.assertEqual(
                    premature_interrupt_diagnostic(USER_REQ, view)["reason"],
                    "remote_prerequisite_probe_not_executed")

    def test_denied_cwd_preflight_routes_to_precise_user_input_not_forged_access(self):
        def denied(_action):
            return {"allowed": False, "resolved_paths": [],
                    "error": "command_working_directory_outside_request_scope"}
        view = transition(preview=denied)
        self.assertEqual(view["stage"], "WORKSPACE_SCOPE_REJECTED")
        self.assertEqual(view["scope_preflight_reason_code"],
                         "command_working_directory_outside_request_scope")
        self.assertFalse(view["probe_evidence_present"])
        self.assertEqual(view["probe_action"], {})
        self.assertIsNone(premature_interrupt_diagnostic(USER_REQ, view))
        self.assertEqual(view["progress_transition"],
                         "REQUEST_EXACT_MISSING_SCOPE_OR_REPLAN")
        self.assertEqual(view["access_state"], "UNKNOWN")

    def test_verified_denial_and_one_time_approval_keep_normal_safety_gates(self):
        for decision, flags, expected_stage in (
            ("DENIED", {"policy_denial_confirmed": True},
             "POLICY_DENIAL_VERIFIED"),
            ("NEEDS_USER_APPROVAL", {"approval_required_confirmed": True},
             "APPROVAL_REQUIREMENT_VERIFIED"),
            ("AUTHORIZED", {"network_authorized": True,
                            "repository_read_authorized": True},
             "ACCESS_VERIFIED"),
        ):
            with self.subTest(decision=decision):
                receipt = dict(
                    schema=RECEIPT_SCHEMA,
                    authority="RUNTIME_SECURITY_PREFLIGHT",
                    request_id=REQ, task_epoch=EPOCH,
                    decision=decision, candidate_id="E1-CASE-D",
                    repository="b43120000/test", evidence_ref="AUTH-RUNTIME-1",
                    **flags,
                )
                remote, prereq = setup_view(receipt)
                view = transition(remote=remote, prereq=prereq)
                # Authorized source can shift the next_category away from
                # RESOLVE_REGISTERED_SOURCE_ARTIFACT. The remote permission
                # remains real and no extra probe can be fabricated.
                if view["required"]:
                    self.assertEqual(view["stage"], expected_stage)
                    self.assertIsNone(premature_interrupt_diagnostic(USER_REQ, view))
                self.assertFalse(view["authorization_granted"])
                self.assertFalse(view["evidence_created"])

    def test_current_epoch_actual_attempt_changes_readiness_not_auth(self):
        action = {
            "tool": "run_command", "operation": "GIT_INSPECT",
            "command": "git ls-remote https://github.com/b43120000/test.git HEAD",
        }
        actions = {"GIT-PROBE-A": {"action": action}}
        evidence = {"GIT-PROBE-A": {
            "request_id": REQ, "task_epoch": EPOCH,
            "evidence_status": "ACTIVE", "result_status": "FAILED",
            "execution_status": "SUCCEEDED",
        }}
        result = transition(actions=actions, evidence=evidence)
        self.assertEqual(result["stage"], "PROBE_EVIDENCE_AVAILABLE_UNRESOLVED")
        self.assertEqual(result["actual_probe_attempts"][0]["action_id"],
                         "GIT-PROBE-A")
        self.assertTrue(result["probe_evidence_present"])
        self.assertEqual(result["access_state"], "UNKNOWN")
        self.assertEqual(result["probe_action"], {})
        self.assertIsNone(premature_interrupt_diagnostic(USER_REQ, result))
        stale = {**evidence["GIT-PROBE-A"], "task_epoch": "OTHER_EPOCH"}
        result = transition(actions=actions,
                            evidence={"GIT-PROBE-A": stale})
        self.assertEqual(result["stage"], "READY_FOR_TOOL_ADMISSION")
        forged = {"action": {**action,
                            "command": action["command"] + "; whoami"}}
        result = transition(actions={"GIT-PROBE-A": forged}, evidence=evidence)
        self.assertEqual(result["stage"], "READY_FOR_TOOL_ADMISSION")

    def test_unknown_or_unavailable_preflight_not_claimed_verified(self):
        def unavailable(_action):
            raise OSError("blocked in fixture")
        record = transition(preview=unavailable)
        self.assertEqual(record["stage"], "WORKSPACE_SCOPE_UNVERIFIED")
        self.assertIsNone(premature_interrupt_diagnostic(USER_REQ, record))
        missing_git = {"tools": {"git": {"status": "MISSING"}}}
        remote, prereq = setup_view(env=missing_git)
        result = transition(remote=remote, prereq=prereq)
        self.assertEqual(result["stage"], "GIT_CAPABILITY_UNVERIFIED")
        self.assertIsNone(premature_interrupt_diagnostic(USER_REQ, result))

    def test_unrelated_goals_and_unsafe_artifact_candidates_not_auto_probed(self):
        remote, prereq = setup_view()
        no_source = {**prereq, "source_requirement": "NOT_REQUIRED"}
        view = transition(remote=remote, prereq=no_source)
        self.assertEqual(view["stage"], "NOT_APPLICABLE")
        self.assertIsNone(premature_interrupt_diagnostic(USER_REQ, view))
        for bad in ("b43120000/test;rm -rf", "another/repo/extra"):
            with self.subTest(repo=bad):
                artifact = {"artifact_references": [
                    {**ARTIFACT["artifact_references"][0], "repository": bad}]}
                result = build_prerequisite_transition(
                    artifact_view=artifact, prerequisite_view=prereq,
                    remote_view=remote, request_id=REQ, task_epoch=EPOCH,
                    bound_workspace="/verified/agent/workspace",
                    tool_scope_preview=fixed_scope_preview,
                )
                self.assertEqual(result["stage"], "NOT_APPLICABLE")
                self.assertEqual(result["probe_action"], {})

    def test_unified_progress_and_replan_share_same_readiness(self):
        view = transition()
        shared = build_transaction(
            request_id=REQ, task_epoch=EPOCH,
            original_goal="Case D", round_id=3,
            phase="REJECTED",
            diagnostics=[{"reason": "remote_prerequisite_probe_not_executed"}],
            submitted=[{"tool": "report_progress", "action_id": "P-G26"}],
            prerequisite_transition=view,
        )
        row = shared["prerequisite_transition"]
        self.assertEqual(row["stage"], "READY_FOR_TOOL_ADMISSION")
        self.assertEqual(row["progress_transition"],
                         "CONTINUE_PROPOSE_EXACT_GIT_READ_ACTION")
        self.assertFalse(row["guidance_is_evidence"])
        self.assertFalse(row["authorization_granted"])


if __name__ == "__main__":
    unittest.main()
