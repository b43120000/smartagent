#!/usr/bin/env python3
"""IT: Case D Scope Preflight → Action Readiness → context_request repair.

Offline, read-only, isolated temp paths. No GitHub, downloading, or mutations
outside TemporaryDirectory; tests are advisory until run on Windows.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_core.action_readiness import (
    SCHEMA, classify_scope_preflight, current_action_readiness,
    tool_readiness_contract,
)
from agent_core.context_acquisition import (
    context_request_repair_diagnostic, normalize_context_request,
)
from agent_core.progress_recovery import repair_diagnostic
from WebAgent.tool_context import WebAgentToolContext


class ScopeContextTransitionIT(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scope-context-it-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / "not_yet_deployed"
        self.tools = WebAgentToolContext(self.root)

    def test_unavailable_project_workspace_is_not_missing_fact(self):
        self.tools.begin_run("RR-SCOPE-MISSING", "read requested scope",
                             [str(self.target)])
        result = self.tools.preview_tool_scope({
            "tool": "inspect_project_scope", "action_id": "SCOPE-001",
            "workspace": str(self.target),
        })
        self.assertFalse(result["allowed"])
        diag = result["scope_diagnostic"]
        self.assertEqual(diag["schema"], SCHEMA)
        self.assertEqual(diag["reason_code"], "workspace_not_found_or_not_directory")
        self.assertEqual(diag["category"], "PROJECT_WORKSPACE_UNAVAILABLE_OR_NOT_DIRECTORY")
        self.assertEqual(diag["path_role"], "PROJECT_WORKSPACE")
        self.assertEqual(diag["eligible_transition"], "EVIDENCE_ACQUISITION")
        self.assertFalse(diag["filesystem_absence_verified"])
        self.assertFalse(diag["authorization_granted"])
        self.assertNotIn(str(self.target), str(diag))
        self.assertEqual(result["scope_source"], "workspace")
        self.assertEqual(result["requested_paths"], [str(self.target)])

        # Alternate read only passes ordinary scope checks, NOT a write grant.
        inspection = self.tools.preview_tool_scope({
            "tool": "list_directory", "action_id": "SCOPED-READ",
            "path": str(self.target),
        })
        self.assertTrue(inspection["allowed"], inspection)
        self.assertFalse(inspection["scope_diagnostic"]["authorization_granted"])

    def test_missing_required_workspace_field_is_schema_repair(self):
        self.tools.begin_run("RR-SCOPE-NOFIELD", "read explicit request",
                             [str(self.target)])
        result = self.tools.preview_tool_scope({
            "tool": "inspect_project_scope", "action_id": "SCOPE-002",
        })
        self.assertFalse(result["allowed"])
        diag = result["scope_diagnostic"]
        self.assertEqual(diag["reason_code"], "explicit_path_scope_requires_workspace")
        self.assertEqual(diag["required_field"], "workspace")
        self.assertEqual(diag["category"], "ACTION_FIELD_INVALID")
        self.assertEqual(diag["eligible_transition"], "ACTION_REPAIR")

    def test_non_directory_must_not_be_treated_as_verified_absence(self):
        target = self.root / "not_a_folder"
        target.write_text("not a directory", encoding="utf-8")
        self.tools.begin_run("RR-NONDIR", "read explicit request", [str(target)])
        result = self.tools.preview_tool_scope({
            "tool": "inspect_project_scope", "action_id": "SCOPE-NONDIR",
            "workspace": str(target),
        })
        self.assertFalse(result["allowed"])
        self.assertFalse(result["scope_diagnostic"]["filesystem_absence_verified"])

    def test_outside_scope_remains_denied(self):
        allowed = self.root / "allowed"
        allowed.mkdir()
        self.tools.begin_run("RR-SCOPE-DENIED", "read authorized folder",
                             [str(allowed)])
        result = self.tools.preview_tool_scope({
            "tool": "list_directory", "action_id": "SCOPE-OUT",
            "path": str(self.root),
        })
        self.assertFalse(result["allowed"])
        self.assertEqual(result["scope_diagnostic"]["category"],
                         "AUTHORIZATION_OR_SCOPE_BLOCKED")
        self.assertFalse(result["scope_diagnostic"]["can_expand_authorized_scope"])

    def test_tool_schema_is_explicit_without_preemptive_admission(self):
        scope = tool_readiness_contract("inspect_project_scope")
        self.assertEqual(scope["required_action_fields_when_explicit_scope"],
                         ["workspace"])
        self.assertTrue(scope["existing_project_workspace_required"])
        self.assertTrue(scope["tool_admission_still_required"])
        self.assertEqual(
            tool_readiness_contract("inspect_directory")["required_action_fields_when_explicit_scope"],
            ["paths"],
        )

    def test_current_readiness_uses_only_bounded_runtime_diagnostics(self):
        diag = classify_scope_preflight(
            "inspect_project_scope", "workspace_not_found_or_not_directory:SECRET_PATH",
            allowed=False,
        )
        proof = {
            "SCOPE-01": {
                "scope_diagnostic": diag,
                "result_status": "REJECTED", "executed": False,
                "evidence_status": "ACTIVE",
            },
        }
        result = current_action_readiness(proof, {})
        self.assertEqual(result[0]["evidence_ref"], "SCOPE-01")
        self.assertNotIn("SECRET_PATH", repr(result))
        proof["SCOPE-01"]["evidence_status"] = "STALE"
        self.assertEqual(current_action_readiness(proof, {}), [])

    def test_context_subfields_code_repair_and_unknown_blocker(self):
        submitted = {
            "kind": "BLOCKED", "detail": "",
            "extra_command": "DO_NOT_ECHO_SECRET",
        }
        detail = context_request_repair_diagnostic(
            submitted, objective_blocker_ids=("SCOPE-01",),
        )
        codes = {(x["field"], x["code"]) for x in detail["field_issues"]}
        self.assertIn(("context_request", "unsupported_keys"), codes)
        self.assertIn(("context_request.detail", "detail_required"), codes)
        self.assertIn(("context_request.blocking_action_id",
                       "objective_blocker_ref_required"), codes)
        self.assertNotIn("DO_NOT_ECHO_SECRET", repr(detail))
        self.assertEqual(detail["objective_blocker_ids"], ["SCOPE-01"])
        repaired = {
            "kind": "BLOCKED", "detail": "Authorized read was denied",
            "blocking_action_id": "SCOPE-01",
        }
        self.assertEqual(normalize_context_request(repaired), repaired)
        with self.assertRaisesRegex(
            ValueError, "context_request_blocking_action_id_required"
        ):
            normalize_context_request({
                "kind": "BLOCKED", "detail": "Scoped read was refused",
            })

    def test_progress_repair_includes_bounded_context_subfield(self):
        payload = {
            "decision": "INTERRUPT", "outcome": "UNKNOWN",
            "context_request": {
                "kind": "BLOCKED", "detail": "",
                "irrelevant": "private",
            },
            "evidence_refs": ["SCOPE-01"],
        }
        result = repair_diagnostic(
            payload, None, ValueError("context_request_schema_invalid"),
            {"SCOPE-01"}, objective_blocker_ids=["SCOPE-01"],
        )
        self.assertEqual(result["repair_scope"], "CONTEXT_REQUEST_SCHEMA")
        self.assertTrue(result["context_request_repair"]["field_issues"])
        self.assertTrue(any(x["field"] == "context_request.detail"
                            for x in result["field_issues"]))
        self.assertNotIn("private", repr(result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
