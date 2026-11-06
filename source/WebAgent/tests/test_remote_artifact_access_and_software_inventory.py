#!/usr/bin/env python3
"""Remote E1 artifact readiness must not become invented permission evidence."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.remote_artifact_access_readiness import (
    RECEIPT_SCHEMA, resolve_remote_artifact_access,
)
from agent_core.runtime_software_inventory import build_runtime_software_inventory
from agent_core.goal_prerequisite_resolution import (
    resolve_goal_prerequisites, premature_source_interrupt_diagnostic,
)
from agent_core.project_environment import planner_environment_hint, SCHEMA as PROJECT_SCHEMA
from agent_core.runtime_environment import planner_environment_snapshot
from agent_core.target_path_contract import extract_target_contract
from agent_core.source_discovery_bootstrap import bootstrap_source_discovery
import json

ROOT = Path(__file__).resolve().parents[3]
REPO = "b43120000/test"
REQUEST = "RR-TEST-001"
EPOCH = "epoch-test-01"
TARGET = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
GOAL = ("驗證並完成 Case D 的部署目標。 預期部署根目錄：" + TARGET
        + " 預期包含：case_a、case_b、case_c、fixture_manifest.json、check_results.py。")


class RemoteAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.artifact = bootstrap_source_discovery(
            self.root, extract_target_contract(GOAL),
            owner="test", instance="instance", workspace=str(self.root),
            installation_root=ROOT,
        )
        self.assertEqual(self.artifact["artifact_count"], 1)
        self.candidate = self.artifact["artifact_references"][0]
        self.env = {"tools": {"git": {"status": "AVAILABLE",
                                      "path": r"C:\smartagent\.venv\git\cmd\git.exe",
                                      "version": "git version 2.56.0"}}}

    def resolve(self, receipt=None, env=None):
        return resolve_remote_artifact_access(
            self.artifact, environment=env if env is not None else self.env,
            runtime_receipt=receipt, request_id=REQUEST, task_epoch=EPOCH,
        )

    def receipt(self, decision, **flags):
        return dict(
            schema=RECEIPT_SCHEMA, authority="RUNTIME_SECURITY_PREFLIGHT",
            decision=decision,
            request_id=REQUEST, task_epoch=EPOCH,
            repository=REPO, candidate_id=self.candidate["candidate_id"],
            evidence_ref="EVIDENCE-ACCESS-1", **flags,
        )

    def test_unknown_is_not_denied_even_with_bundled_git(self):
        got = self.resolve()
        self.assertEqual(got["authorization_state"], "UNKNOWN")
        self.assertEqual(got["git_executable_status"], "AVAILABLE")
        self.assertEqual(got["next_verification"],
                         "CHECK_GIT_REPOSITORY_NETWORK_AND_SCOPE_ADMISSION")
        self.assertFalse(got["authorization_inferred_from_git_installation"])
        self.assertFalse(got["network_request_attempted"])
        self.assertEqual(got["git_tool_route"], {
            "tool": "run_command", "operation": "GIT_INSPECT",
        })
        self.assertIsNone(self.candidate["download_authorization"])
        self.assertEqual(self.candidate["download_authorization_state"], "UNKNOWN")

    def test_pinned_attestation_with_both_network_and_repo_access_authorizes(self):
        good = self.receipt("AUTHORIZED", network_authorized=True,
                            repository_read_authorized=True)
        self.assertEqual(self.resolve(good)["authorization_state"], "AUTHORIZED")
        self.assertEqual(self.resolve(good)["authorization_evidence_ref"], "EVIDENCE-ACCESS-1")
        fail = self.receipt("AUTHORIZED", network_authorized=True,
                            repository_read_authorized=False)
        self.assertEqual(self.resolve(fail)["authorization_state"], "UNKNOWN")

    def test_runtime_confirmed_denial_and_approval_are_distinct(self):
        denied = self.receipt("DENIED", policy_denial_confirmed=True)
        approval = self.receipt("NEEDS_USER_APPROVAL", approval_required_confirmed=True)
        self.assertEqual(self.resolve(denied)["authorization_state"], "DENIED")
        self.assertEqual(self.resolve(approval)["authorization_state"], "NEEDS_USER_APPROVAL")
        self.assertEqual(self.resolve(self.receipt("DENIED"))["authorization_state"], "UNKNOWN")
        self.assertEqual(self.resolve(self.receipt("NEEDS_USER_APPROVAL"))["authorization_state"], "UNKNOWN")

    def test_wrong_request_epoch_repo_or_candidate_cannot_grant(self):
        for key, val in (("request_id", "RR-OLD"), ("task_epoch", "old-epoch"),
                         ("repository", "somebody/other"), ("candidate_id", "ART-OTHER"),
                         ("authority", "PLANNER"), ("evidence_ref", "")):
            receipt = self.receipt("AUTHORIZED", network_authorized=True,
                                   repository_read_authorized=True)
            receipt[key] = val
            self.assertEqual(self.resolve(receipt)["authorization_state"], "UNKNOWN", key)

    def test_missing_git_does_not_prove_network_denial(self):
        e = {"tools": {"git": {"status": "MISSING"}}}
        got = self.resolve(env=e)
        self.assertEqual(got["authorization_state"], "UNKNOWN")
        self.assertEqual(got["next_verification"], "VERIFY_LOCAL_GIT_CAPABILITY")

    def test_prerequisite_preserves_unknown_and_probe_route(self):
        a = self.resolve()
        gap = {"goal_policy": {"intent": "MUTATION_ALLOWED"},
               "source_requirement": "REQUIRED",
               "active_gaps": [{
                   "gap_id": "G1", "path": TARGET,
                   "status": "UNRESOLVED", "observation_status": "CONFIRMED",
                   "observed_value": False, "desired_value": True,
                   "observed_fact_ref": "CURRENT-EVIDENCE"
               }]}
        view = resolve_goal_prerequisites(
            gap, {"candidate_reads": []}, {"candidates": []}, {"sources": []},
            self.artifact, a,
        )
        self.assertEqual(view["next_category"], "RESOLVE_REGISTERED_SOURCE_ARTIFACT")
        self.assertEqual(view["remote_access_authorization_state"], "UNKNOWN")
        self.assertIsNone(view["remote_access_authorized"])
        self.assertEqual(view["git_executable_status"], "AVAILABLE")
        request = {"kind": "USER_INPUT",
                   "detail": "需要授權 GitHub 私人儲存庫存取才可以取得壓縮檔"}
        diag = premature_source_interrupt_diagnostic(request, view)
        self.assertEqual(diag["reason"], "remote_access_unknown_git_capability_probe_required")
        view["remote_access_probe_guidance_already_issued"] = True
        self.assertIsNone(premature_source_interrupt_diagnostic(request, view))

    def test_denied_authorization_bool_only_when_confirmed(self):
        denied = self.resolve(self.receipt("DENIED", policy_denial_confirmed=True))
        approval = self.resolve(self.receipt("NEEDS_USER_APPROVAL", approval_required_confirmed=True))
        gap = {"source_requirement": "REQUIRED", "active_gaps": [],
               "goal_policy": {"intent": "MUTATION_ALLOWED"}}
        d = resolve_goal_prerequisites(gap, artifact_view=self.artifact, remote_access_view=denied)
        u = resolve_goal_prerequisites(gap, artifact_view=self.artifact, remote_access_view=approval)
        self.assertIs(d["remote_access_authorized"], False)
        self.assertIsNone(u["remote_access_authorized"])


class SoftwareInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.install = Path(self.temp.name) / "app"
        self.venv = self.install / ".venv"
        scripts = self.venv / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "cmake.exe").write_bytes(b"FAKE-ONLY")
        self.project = Path(self.temp.name) / "project"
        metadata = self.project / ".agents" / "project_context"
        metadata.mkdir(parents=True)
        (metadata / "environment.json").write_text(json.dumps({
            "schema": PROJECT_SCHEMA,
            "dependencies": {"opencv-python": {"status": "VERIFIED",
                                                 "version": "4.10",
                                                 "location": str(self.project / "deps"),
                                                 "evidence_ref": "HIST-DEP"}},
            "tools": {"cmake": {"status": "VERIFIED",
                               "version": "3.31",
                               "path": str(self.project / "cmake.exe"),
                               "evidence_ref": "HIST-TOOL"}},
        }), encoding="utf-8")

    def test_probed_bundled_and_recorded_project_tools_are_not_equivalent(self):
        env = {"tools": {
            "python": {"status": "AVAILABLE", "path": str(self.venv / "Scripts" / "python.exe"),
                       "version": "Python 3.13", "probe_exit_code": 0},
            "git": {"status": "AVAILABLE", "path": str(self.venv / "git" / "cmd" / "git.exe"),
                    "version": "git version 2.56", "probe_exit_code": 0},
        }}
        hint = planner_environment_hint(self.project)
        got = build_runtime_software_inventory(env, hint, installation_root=self.install)
        self.assertEqual(len(got["bundled_tool_probes"]), 2)
        self.assertEqual(got["bundled_tool_probes"][0]["availability"], "CURRENT_AVAILABLE")
        self.assertEqual(got["project_environment_index_status"], "PRESENT")
        self.assertEqual(got["project_recorded_tool_candidates"][0]["availability"],
                         "RECORDED_REPROBE_REQUIRED")
        self.assertEqual(got["project_recorded_dependency_candidates"][0]["availability"],
                         "RECORDED_REPROBE_REQUIRED")
        self.assertTrue(any(a["name"] == "cmake.exe"
                            for a in got["bundled_other_executable_candidates"]))
        self.assertTrue(got["selection_policy"]["explicit_user_path_overrides_default"])
        self.assertTrue(got["selection_policy"]["shared_venv_mutation_requires_separate_authorization"])
        self.assertFalse(got["inventory_grants_installation_or_network_authorization"])

    def test_absent_index_and_unprobed_tool_are_unknown_not_available(self):
        missing = build_runtime_software_inventory(
            {"tools": {"git": {"status": "UNKNOWN"}}},
            {"index_status": "ABSENT"}, installation_root=self.install,
        )
        self.assertEqual(missing["bundled_tool_probes"][0]["availability"], "UNKNOWN")
        self.assertEqual(missing["project_recorded_tool_candidates"], [])


if __name__ == "__main__":
    unittest.main()
