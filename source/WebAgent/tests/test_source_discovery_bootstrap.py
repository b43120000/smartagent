#!/usr/bin/env python3
"""Source Discovery Bootstrap: pinned artifacts without implicit access."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_core.source_discovery_bootstrap import (
    bootstrap_source_discovery,
    register_source_artifact,
)
from agent_core.goal_prerequisite_resolution import (
    resolve_goal_prerequisites, premature_source_interrupt_diagnostic,
)
from agent_core.target_path_contract import extract_target_contract

ROOT = Path(__file__).resolve().parents[3]
CATALOG = ROOT / "config" / "source_discovery_catalog.json"
TARGET = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
ENTRIES = ["case_a", "case_b", "case_c", "fixture_manifest.json", "check_results.py"]
GOAL = (
    "驗證並完成 Case D 的部署目標。"
    + "\n預期部署根目錄：\n" + TARGET
    + "\n預期包含：" + "、".join(ENTRIES) + "。"
    + "\n先確認來源；若不存在，從可信來源取得內容，經授權後部署。"
)
EXPECTED = extract_target_contract(GOAL)


def candidate_record(target=TARGET):
    return {
        "requested_target": target,
        "required_top_level_entries": ENTRIES,
        "provenance": "user-provided-project-artifact-manifest",
        "artifact": {
            "provider": "github",
            "repository": "example-user/private-fixtures",
            "commit": "a" * 40,
            "path": "fixtures/release-v1.zip",
            "archive_root": "release-v1",
            "sha256": "b" * 64,
            "git_blob_sha": "c" * 40,
            "manifest_path": "fixtures/manifest.json",
        },
    }


class SourceDiscoveryBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = str(self.root / "workspace")
        Path(self.work).mkdir()

    def _bootstrap(self, contract=EXPECTED, **extra):
        return bootstrap_source_discovery(
            self.root, contract,
            owner="telegram:one:one", instance="agent-instance",
            workspace=self.work, installation_root=self.root, **extra,
        )

    def _package_catalog(self):
        target = self.root / "config"
        target.mkdir()
        (target / "source_discovery_catalog.json").write_text(
            CATALOG.read_text(encoding="utf-8"), encoding="utf-8",
        )

    def test_required_entries_keep_last_filename_before_chinese_period(self):
        self.assertEqual(EXPECTED["required_top_level_entries"], ENTRIES)

    def test_telegram_worker_state_root_reads_real_installed_catalog(self):
        # Production Telegram worker uses source/ as its Task state root, not
        # the application installation directory. No fake catalog is created.
        worker_source = (ROOT / "source" / "RemoteAgent"
                         / "telegram_webagent_worker.py").read_text(encoding="utf-8")
        self.assertIn("ROOT = Path(__file__).resolve().parents[1]", worker_source)
        worker_state_root = ROOT / "source"
        found = bootstrap_source_discovery(
            worker_state_root, EXPECTED,
            owner="telegram:one:one", instance="agent-instance",
            workspace=self.work,
        )
        self.assertEqual(found["catalog_path_authority"], "INSTALLATION_ROOT")
        self.assertEqual(found["registration_path_authority"], "TASK_STATE_ROOT")
        self.assertEqual(found["catalog_status"], "READY")
        self.assertEqual(found["catalog_records_examined"], 1)
        self.assertEqual(found["catalog_matched_count"], 1)
        self.assertEqual(found["catalog_goal_mismatch_count"], 0)
        self.assertEqual(found["state"], "ARTIFACT_REFERENCE_FOUND")
        self.assertEqual(found["artifact_count"], 1)
        self.assertFalse(found["network_access_attempted"])
        self.assertFalse(found["new_authorization_granted"])

    def test_catalog_path_is_never_derived_from_task_state_or_workspace(self):
        # Explicit fake package root and separate Task metadata root.
        # A decoy catalog in state_root/config must never win.
        from agent_core.paths import runtime_root
        state = self.root / "state-root"
        installed = self.root / "installed-root"
        state.mkdir()
        installed.mkdir()
        packaged = installed / "config"
        packaged.mkdir()
        (packaged / "source_discovery_catalog.json").write_text(
            CATALOG.read_text(encoding="utf-8"), encoding="utf-8",
        )
        decoy = state / "config"
        decoy.mkdir()
        (decoy / "source_discovery_catalog.json").write_text(
            '{"schema":"SMARTAGENT_SOURCE_DISCOVERY_CATALOG_V1","records":[]}',
            encoding="utf-8",
        )
        found = bootstrap_source_discovery(
            state, EXPECTED, owner="telegram:one:one",
            instance="agent-instance", workspace=self.work,
            installation_root=installed,
        )
        self.assertEqual(found["artifact_count"], 1)
        self.assertEqual(found["catalog_matched_count"], 1)
        self.assertEqual(found["registration_status"], "NOT_FOUND")
        self.assertFalse((runtime_root(state) / "source_artifact_registrations_v1.json").exists())
        # Wrong installed catalog must not fall back to valid Task-state decoy.
        missing = bootstrap_source_discovery(
            state, EXPECTED, owner="telegram:one:one",
            instance="agent-instance", workspace=self.work,
            installation_root=self.root / "nonexistent-install",
        )
        self.assertEqual(missing["catalog_status"], "NOT_FOUND")
        self.assertEqual(missing["artifact_count"], 0)

    def test_matching_diagnostics_distinguish_invalid_from_unmatched_goal(self):
        self._package_catalog()
        wrong_target = extract_target_contract(
            GOAL.replace(TARGET, r"E:\NotTheExpectedTarget")
        )
        mismatched = self._bootstrap(wrong_target)
        self.assertEqual(mismatched["catalog_status"], "READY")
        self.assertEqual(mismatched["catalog_records_examined"], 1)
        self.assertEqual(mismatched["catalog_goal_mismatch_count"], 1)
        self.assertEqual(mismatched["catalog_matched_count"], 0)
        self.assertEqual(mismatched["artifact_count"], 0)

    def test_cold_start_case_d_finds_pinned_fixture_reference_without_history(self):
        self._package_catalog()
        got = self._bootstrap()
        self.assertEqual(got["state"], "ARTIFACT_REFERENCE_FOUND")
        self.assertEqual(got["catalog_status"], "READY")
        self.assertEqual(got["artifact_count"], 1)
        artifact = got["artifact_references"][0]
        self.assertEqual(artifact["repository"], "b43120000/test")
        self.assertEqual(
            artifact["artifact_path"],
            "goal-driven-replan/v1/SmartAgent_GoalDriven_IT_fixture.zip",
        )
        self.assertEqual(
            artifact["expected_sha256"],
            "c38d634188e23868bd384ad8b0cd12bbe20d778eea8b67338f4ec2bbf747ef62",
        )
        self.assertEqual(artifact["evidence_level"], "E1")
        self.assertEqual(artifact["repository_access"], "NOT_CHECKED")
        self.assertFalse(artifact["artifact_retrieved"])
        self.assertFalse(artifact["archive_hash_verified"])
        self.assertFalse(artifact["source_is_deployment_ready"])
        self.assertFalse(got["network_access_attempted"])
        self.assertFalse(got["disk_source_scan_performed"])
        self.assertFalse(got["new_authorization_granted"])

    def test_goal_prerequisite_moves_to_artifact_acquisition_not_missing_path(self):
        self._package_catalog()
        artifact = self._bootstrap()
        gap = {
            "goal_policy": {"intent": "MUTATION_ALLOWED"},
            "source_requirement": "REQUIRED",
            "active_gaps": [{
                "gap_id": "G1", "path": TARGET,
                "status": "UNRESOLVED", "observation_status": "CONFIRMED",
                "observed_value": False, "desired_value": True,
                "observed_fact_ref": "ACT-TARGET-ABSENT",
            }],
        }
        outcome = resolve_goal_prerequisites(gap, {"candidate_reads":[]},
                                             {"candidates":[]}, {"sources":[]}, artifact)
        self.assertEqual(outcome["next_category"], "RESOLVE_REGISTERED_SOURCE_ARTIFACT")
        self.assertEqual(outcome["registered_artifact_count"], 1)
        self.assertFalse(outcome["remote_access_authorized"])
        self.assertFalse(outcome["artifact_integrity_verified"])
        self.assertFalse(outcome["source_integrity_verified"])
        self.assertFalse(outcome["write_authorization_granted"])

    def test_known_remote_artifact_requires_precise_access_not_generic_source_path(self):
        self._package_catalog()
        view = self._bootstrap()
        gap = {
            "goal_policy": {"intent": "MUTATION_ALLOWED"},
            "source_requirement": "REQUIRED",
            "active_gaps": [{
                "gap_id": "TARGET-ABSENT", "path": TARGET,
                "status": "UNRESOLVED", "observation_status": "CONFIRMED",
                "observed_value": False, "desired_value": True,
                "observed_fact_ref": "ACT-OBSERVE",
            }],
        }
        resolution = resolve_goal_prerequisites(
            gap, {"candidate_reads": []}, {"candidates": []}, {"sources": []}, view,
        )
        diag = premature_source_interrupt_diagnostic({
            "kind": "USER_INPUT",
            "detail": "請提供先前部署來源的完整路徑與唯讀授權。",
        }, resolution)
        self.assertEqual(
            diag["reason"],
            "remote_artifact_reference_grounded_no_source_path_request",
        )
        self.assertIsNone(premature_source_interrupt_diagnostic({
            "kind": "USER_INPUT",
            "detail": "請授權 GitHub 私人儲存庫存取，以取得來源 ZIP。",
        }, resolution))

    def test_unrelated_goal_and_required_entries_not_matched(self):
        self._package_catalog()
        self.assertEqual(self._bootstrap(
            extract_target_contract(GOAL.replace(TARGET, r"E:\OtherDeployment"))
        )["artifact_count"], 0)
        self.assertEqual(self._bootstrap(
            extract_target_contract(GOAL.replace("check_results.py", "different.py"))
        )["artifact_count"], 0)

    def test_no_catalog_is_a_distinct_state_not_source_absence(self):
        got = self._bootstrap()
        self.assertEqual(got["state"], "NO_REGISTERED_ARTIFACT")
        self.assertEqual(got["catalog_status"], "NOT_FOUND")
        self.assertFalse(got["source_location_known_as_reference"])

    def test_corrupt_catalog_fails_closed(self):
        dest = self.root / "config"
        dest.mkdir()
        (dest / "source_discovery_catalog.json").write_text(
            '{"schema": "NOT_THE_EXPECTED_SCHEMA", "records": []}', encoding="utf-8",
        )
        got = self._bootstrap()
        self.assertEqual(got["state"], "CATALOG_INVALID_OR_UNREADABLE")
        self.assertEqual(got["catalog_status"], "INVALID")
        self.assertEqual(got["artifact_count"], 0)

    def test_register_requires_explicit_user_approval(self):
        record = candidate_record()
        blocked = register_source_artifact(
            self.root, owner="telegram:one:one", instance="agent-instance",
            workspace=self.work, record=record,
        )
        self.assertEqual(blocked["status"], "REJECTED")
        self.assertEqual(self._bootstrap()["artifact_count"], 0)
        recorded = register_source_artifact(
            self.root, owner="telegram:one:one", instance="agent-instance",
            workspace=self.work, record=record, explicitly_authorized=True,
        )
        self.assertEqual(recorded["status"], "RECORDED")
        self.assertFalse(recorded["grants_access"])
        self.assertFalse(recorded["grants_write"])
        got = self._bootstrap()
        self.assertEqual(got["artifact_count"], 1)
        self.assertEqual(got["artifact_references"][0]["registration_origin"], "OWNER_REGISTRY")

    def test_registered_records_do_not_cross_owner_workspace(self):
        self.assertEqual(register_source_artifact(
            self.root, owner="telegram:one:one", instance="agent-instance",
            workspace=self.work, record=candidate_record(),
            explicitly_authorized=True,
        )["status"], "RECORDED")
        other = bootstrap_source_discovery(
            self.root, EXPECTED,
            owner="telegram:another:chat", instance="agent-instance",
            workspace=self.work, installation_root=self.root,
        )
        self.assertEqual(other["artifact_count"], 0)
        otherwork = bootstrap_source_discovery(
            self.root, EXPECTED,
            owner="telegram:one:one", instance="agent-instance",
            workspace=str(self.root / "different-workspace"), installation_root=self.root,
        )
        self.assertEqual(otherwork["artifact_count"], 0)

    def test_invalid_relative_paths_or_moving_ref_rejected(self):
        variants = [
            {"commit": "main"},
            {"path": "../unsafe.zip"},
            {"path": "/root/unsafe.zip"},
            {"path": "fixtures\\archive.zip"},
            {"path": "fixtures/file.exe"},
            {"git_blob_sha": "not-sha"},
        ]
        for updates in variants:
            with self.subTest(updates=updates):
                r = candidate_record()
                r["artifact"].update(updates)
                got = register_source_artifact(
                    self.root, owner="telegram:one:one", instance="agent-instance",
                    workspace=self.work, record=r, explicitly_authorized=True,
                )
                self.assertEqual(got["status"], "REJECTED")

    def test_untrusted_catalog_values_are_not_tool_actions(self):
        self._package_catalog()
        candidate = self._bootstrap()["artifact_references"][0]
        self.assertNotIn("command", candidate)
        self.assertNotIn("local_path", candidate)
        self.assertNotIn("tool", candidate)
        self.assertEqual(candidate["next_prerequisite"],
                         "CHECK_REMOTE_REPOSITORY_ACCESS_AND_AUTHORIZATION")


if __name__ == "__main__":
    unittest.main(verbosity=2)
