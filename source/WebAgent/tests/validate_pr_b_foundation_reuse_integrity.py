"""PR-B Foundation: project reuse correctness and fail-closed persistence authority.

No installation/network or mutation of shared SmartAgent Python is performed.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.prerequisite_runtime_v3 import probe_requirements, explicit_remediation_intent
from agent_core.project_environment import (
    authorize_verified_environment_write,
    environment_index_path,
    observe_project_environment_index,
    planner_environment_hint,
    publish_verified_dependency,
    read_project_environment,
)
from WebAgent.tool_context import WebAgentToolContext


class ReuseIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sa-prb-reuse-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.target = self.project / "deps"
        self.target.mkdir()
        self.index = environment_index_path(self.project)
        self.other = self.base / "workspace"
        self.other.mkdir()
        self.request = "Inspect package synthetic_pr_bcd>=1.0"
        self.environment = {"tools": {
            "python": {"status": "AVAILABLE", "version": "Python 3.11"},
            "git": {"status": "AVAILABLE", "version": "git version 2.43"},
        }}

    def save_candidate(self, version="1.0"):
        publish_verified_dependency(
            self.project, distribution="synthetic_pr_bcd", version=version,
            target=self.target, request_id="RR-FIRST", action_id="ACT-FIRST",
            evidence_ref="ACT-EVIDENCE-FIRST", authorized_project_write=True,
        )

    def probe(self, request=None):
        return probe_requirements(
            request=request or self.request, authorized_paths=[str(self.project)],
            environment_snapshot=self.environment, install_root=self.base,
            project_root=self.project,
        )

    @staticmethod
    def package(report):
        return next(row for row in report["facts"]
                    if row.get("id") == "package:synthetic_pr_bcd")

    def tool(self, workspace):
        tool = WebAgentToolContext(workspace)
        tool.begin_run("RR-TEST", self.request, [str(self.project), str(self.index)])
        return tool

    def test_project_candidate_overrides_outdated_bundled_distribution(self):
        self.save_candidate()
        before = self.index.read_bytes()
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("AVAILABLE", "0.5")), patch(
            "agent_core.prerequisite_runtime_v3._probe_project_package",
            return_value=("AVAILABLE", "1.0"),
        ) as local_probe:
            row = self.package(self.probe())
        self.assertEqual(row["state"], "SATISFIED")
        self.assertEqual(row["observed_version"], "1.0")
        self.assertEqual(row["source"], "PROJECT_ENVIRONMENT_REPROBED")
        local_probe.assert_called_once()
        self.assertEqual(before, self.index.read_bytes(), "Read-only probe wrote project index")

    def test_satisfying_bundled_distribution_skips_project_probe(self):
        self.save_candidate()
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("AVAILABLE", "1.5")), patch(
            "agent_core.prerequisite_runtime_v3._probe_project_package"
        ) as local_probe:
            row = self.package(self.probe())
        self.assertEqual(row["state"], "SATISFIED")
        self.assertEqual(row["source"], "BUNDLED_PYTHON_METADATA_PROBE")
        local_probe.assert_not_called()

    def test_invalid_or_stale_project_candidate_cannot_satisfy(self):
        self.save_candidate()
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("AVAILABLE", "0.5")):
            with patch("agent_core.prerequisite_runtime_v3._probe_project_package",
                       return_value=("UNKNOWN", "")):
                invalid = self.package(self.probe())
            with patch("agent_core.prerequisite_runtime_v3._probe_project_package",
                       return_value=("AVAILABLE", "0.7")):
                outdated = self.package(self.probe())
        self.assertEqual(invalid["state"], "MISSING")
        self.assertEqual(outdated["state"], "MISSING")
        self.assertNotEqual(invalid["source"], "PROJECT_ENVIRONMENT_REPROBED")
        self.assertEqual(outdated["observed_version"], "0.7")

    def test_tampered_external_candidate_is_never_probed(self):
        self.save_candidate()
        doc = read_project_environment(self.project)
        doc["dependencies"]["synthetic-pr-bcd"]["location"] = str(self.other)
        self.index.write_text(json.dumps(doc), encoding="utf-8")
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING", "")), patch(
            "agent_core.prerequisite_runtime_v3._probe_project_package",
            side_effect=AssertionError("External path from index must not be read"),
        ):
            row = self.package(self.probe())
        self.assertEqual(row["state"], "MISSING")
        self.assertEqual(row["source"], "BUNDLED_PYTHON_METADATA_PROBE")

    def test_scoped_prohibitions_do_not_cancel_explicit_remediation(self):
        request = (
            "請安裝 package synthetic_pr_bcd>=1.0 到 deps\n"
            "禁止其他專案路徑寫入\n"
            "不得修改 SmartAgent 共用 Python 環境"
        )
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING", "")):
            allowed = self.probe(request)
            denied = self.probe(
                "請不要安裝 package synthetic_pr_bcd>=1.0，只執行唯讀檢查"
            )
        self.assertTrue(allowed["remediation_requested"], allowed)
        self.assertTrue(allowed["remediation_eligible"])
        self.assertFalse(allowed["authorization_granted"])
        self.assertFalse(denied["remediation_requested"], denied)

    def test_readonly_goal_not_remediation_despite_installation_targets(self):
        requests = [
            "在專案執行唯讀觀測。依賴 synthetic_pr_bcd>=1.0，"
            "原始授權的安裝目標 deps，原始授權的寫入位置 environment.json。"
            "本輪禁止一切實際寫入。請列出每個 Action 的結果。",
            "在專案檢查 synthetic_pr_bcd>=1.0 的 environment.json。"
            "本次不安裝、不修復、不修改任何檔案；如果紀錄不存在回報 NOT APPLICABLE。",
            "檢查 package synthetic_pr_bcd>=1.0，請勿自行安裝或建立環境。",
        ]
        for request in requests:
            self.assertFalse(explicit_remediation_intent(request), request)
            with patch("agent_core.prerequisite_runtime_v3._probe_package",
                       return_value=("MISSING", "")):
                probe = self.probe(request)
            self.assertFalse(probe["remediation_requested"], probe)
            self.assertFalse(probe["remediation_eligible"], probe)
            self.assertFalse(probe["authorization_granted"], probe)
        self.assertTrue(explicit_remediation_intent(
            "請安裝 package synthetic_pr_bcd>=1.0 到 deps。禁止其他專案路徑寫入"
        ))

    def test_missing_index_is_valid_readonly_environment_discovery(self):
        before = set(self.project.rglob("*"))
        observation = observe_project_environment_index(self.project)
        hint = planner_environment_hint(self.project)
        self.assertEqual(observation["state"], "ABSENT")
        self.assertEqual(hint["index_status"], "ABSENT")
        self.assertEqual(hint["index_reason"], "ENVIRONMENT_INDEX_ABSENT")
        self.assertFalse(hint["dependencies"])
        self.assertEqual(before, set(self.project.rglob("*")))
        self.index.parent.mkdir(parents=True, exist_ok=True)
        self.index.write_text('{"schema":"WRONG"}', encoding="utf-8")
        observation = observe_project_environment_index(self.project)
        self.assertEqual(observation["state"], "INVALID")
        self.assertEqual(planner_environment_hint(self.project)["index_status"], "INVALID")
        self.index.write_text('{"schema":', encoding="utf-8")
        self.assertEqual(observe_project_environment_index(self.project)["state"], "INVALID")
        self.index.unlink()
        self.save_candidate()
        self.assertEqual(observe_project_environment_index(self.project)["state"], "PRESENT")
        self.assertEqual(planner_environment_hint(self.project)["index_status"], "PRESENT")

    def test_read_scoped_path_cannot_upgrade_workspace_write_acl(self):
        tool = self.tool(self.other)
        with patch("agent_core.security_context._configured_access_roots",
                   return_value=((), ())):
            allowed, reason = authorize_verified_environment_write(
                self.project, tool_context=tool,
                authorized_paths=[str(self.project), str(self.index)]
            )
        self.assertFalse(allowed)
        self.assertEqual(reason, "PROJECT_ENVIRONMENT_WRITE_ACL_DENIED")
        self.assertFalse(self.index.exists())

    def test_bound_ceiling_requires_exact_metadata_and_actual_acl(self):
        tool = self.tool(self.project)
        ceiling = {
            "active": True, "valid": True, "project_root": str(self.project),
            "metadata": str(self.index), "deps": str(self.target),
        }
        with patch("agent_core.security_context._configured_access_roots",
                   return_value=((), ())):
            yes, reason = authorize_verified_environment_write(
                self.project, tool_context=tool,
                authorized_paths=[str(self.project), str(self.index)],
                request_write_ceiling=ceiling,
            )
            omitted, why_omitted = authorize_verified_environment_write(
                self.project, tool_context=tool,
                authorized_paths=[str(self.project)], request_write_ceiling=ceiling,
            )
            invalid, why_invalid = authorize_verified_environment_write(
                self.project, tool_context=tool,
                authorized_paths=[str(self.project), str(self.index)],
                request_write_ceiling={**ceiling, "valid": False},
            )
            other, why_other = authorize_verified_environment_write(
                self.project, tool_context=tool,
                authorized_paths=[str(self.project), str(self.index)],
                request_write_ceiling={**ceiling, "metadata": str(self.project / "other.json")},
            )
        self.assertTrue(yes, reason)
        self.assertEqual(reason, "PROJECT_ENVIRONMENT_WRITE_AUTHORIZED")
        self.assertFalse(omitted)
        self.assertEqual(why_omitted, "PROJECT_ENVIRONMENT_WRITE_CEILING_SCOPE_MISMATCH")
        self.assertFalse(invalid)
        self.assertEqual(why_invalid, "PROJECT_ENVIRONMENT_WRITE_CEILING_INVALID")
        self.assertFalse(other)
        self.assertEqual(why_other, "PROJECT_ENVIRONMENT_WRITE_CEILING_SCOPE_MISMATCH")
        self.assertFalse(self.index.exists())

    def test_project_not_in_original_request_scope(self):
        tool = self.tool(self.project)
        with patch("agent_core.security_context._configured_access_roots",
                   return_value=((), ())):
            allowed, reason = authorize_verified_environment_write(
                self.project, tool_context=tool, authorized_paths=[]
            )
        self.assertFalse(allowed)
        self.assertEqual(reason, "PROJECT_ENVIRONMENT_NOT_IN_REQUEST_SCOPE")

    def test_index_only_records_candidates_not_future_authorization(self):
        self.save_candidate()
        entry = read_project_environment(self.project)["dependencies"]["synthetic-pr-bcd"]
        self.assertEqual(entry["status"], "VERIFIED")
        self.assertEqual(entry["request_id"], "RR-FIRST")
        tool = self.tool(self.other)
        with patch("agent_core.security_context._configured_access_roots",
                   return_value=((), ())):
            authorized, _ = authorize_verified_environment_write(
                self.project, tool_context=tool,
                authorized_paths=[str(self.project), str(self.index)],
            )
        self.assertFalse(authorized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
