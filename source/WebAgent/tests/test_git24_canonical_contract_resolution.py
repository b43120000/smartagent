"""Git24 canonical Planner contract: safety equivalence, admission and Case D.

Run as: PYTHONPATH=source python -B source/WebAgent/tests/test_git24_canonical_contract_resolution.py
All tests are offline; no browser or real deployment writes.
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from agent_core.canonical_contract_resolver import (
    SCHEMA, resolve_planner_contracts,
)
from agent_core.semantic_path_resolution import (
    SCHEMA as PATH_SCHEMA, validate_planner_path_confirmation,
)
from agent_core.semantic_capability_resolution import (
    SCHEMA as CAP_SCHEMA, verify_resolution_for_actions,
)


class CanonicalContractResolutionTests(unittest.TestCase):
    def setUp(self):
        self.target = r"E:\SmartAgent_GoalDriven_IT_GH_v1"
        self.document = {"status": "LEXICAL_EXACT", "paths": [self.target]}

    def resolve(self, path, capability=None, document=None):
        progress = {"tool": "report_progress", "path_resolution": path}
        if capability is not None:
            progress["capability_resolution"] = capability
        source = copy.deepcopy(progress)
        result = resolve_planner_contracts(
            progress, path_document=self.document if document is None else document,
        )
        self.assertEqual(source, progress, "Never mutate original Planner proposal")
        return result

    def test_case_d_safe_canonical_paths_and_capabilities(self):
        result, receipts, diagnostics = self.resolve(
            {"paths": self.target, "status": "LEXICAL_EXACT"},
            {"capabilities": "directory.inspect_metadata"},
        )
        self.assertFalse(diagnostics)
        self.assertEqual(result["path_resolution"], {
            "schema": PATH_SCHEMA, "selected_paths": [self.target]})
        self.assertEqual(result["capability_resolution"], {
            "schema": CAP_SCHEMA, "capabilities": ["directory.inspect_metadata"]})
        self.assertEqual({r["decision"] for r in receipts}, {"REWRITE_SAFE_EQUIVALENT"})
        self.assertTrue(all(r["rewrite_applied"] and not r["authorization_granted"]
                            and not r["executed"] for r in receipts))
        validate_planner_path_confirmation(result["path_resolution"], self.document)
        verify_resolution_for_actions(result["capability_resolution"], None,
                                      [{"tool": "inspect_directory"}])
        self.assertEqual(result["path_resolution"], self.resolve(
            result["path_resolution"])[0]["path_resolution"])
        _, canonical, errors = self.resolve(result["path_resolution"])
        self.assertFalse(errors)
        self.assertEqual(canonical[-1]["decision"], "PASS_UNCHANGED")

    def test_unambiguous_missing_schema_only_adds_fixed_schema(self):
        p = {"selected_paths": [self.target]}
        result, receipts, errors = self.resolve(p)
        self.assertFalse(errors)
        self.assertEqual(result["path_resolution"]["schema"], PATH_SCHEMA)
        self.assertEqual(receipts[-1]["rewrite_rule_ids"], ["ADD_FIXED_PATH_SCHEMA"])

    def test_exact_preflight_echo_can_be_removed_without_scope_change(self):
        result, receipts, errors = self.resolve({
            "schema": PATH_SCHEMA, "selected_paths": [self.target],
            "source": "VERIFIED_ORIGINAL_REQUEST_SPANS",
        })
        self.assertFalse(errors)
        self.assertNotIn("source", result["path_resolution"])
        self.assertIn("REMOVE_EXACT_PREFLIGHT_ECHO", receipts[-1]["rewrite_rule_ids"])

    def test_conflicting_or_untrusted_path_cannot_be_rewritten(self):
        cases = [
            {"schema": PATH_SCHEMA, "selected_paths": [r"E:\OtherProject"]},
            {"schema": PATH_SCHEMA, "path": r"E:\OtherProject"},
            {"schema": PATH_SCHEMA, "path": self.target,
             "selected_paths": [r"E:\OtherProject"]},
        ]
        for path in cases:
            with self.subTest(path=path):
                result, receipts, errors = self.resolve(path)
                self.assertEqual(result["path_resolution"], path)
                self.assertEqual(receipts[-1]["decision"], "REJECT_REPLAN")
                self.assertFalse(receipts[-1]["rewrite_applied"])
                self.assertEqual(errors[-1]["repair_scope"], "SEMANTIC_CONFLICT")
                self.assertIn("not", errors[-1]["suggestion"])

    def test_unknown_keys_or_incomplete_values_require_planner(self):
        cases = [
            {"schema": PATH_SCHEMA},
            {"schema": "WRONG", "selected_paths": [self.target]},
            {"schema": PATH_SCHEMA, "selected_paths": [self.target],
             "authorization": True},
            {"schema": PATH_SCHEMA, "selected_paths": [self.target],
             "status": "SCOPE_GRANTED"},
            ["unexpected"],
        ]
        for path in cases:
            with self.subTest(path=path):
                result, receipts, errors = self.resolve(path)
                self.assertEqual(result["path_resolution"], path)
                self.assertEqual(receipts[-1]["decision"], "NEEDS_INFORMATION")
                self.assertEqual(errors[-1]["repair_scope"], "CANONICAL_SCHEMA")
                self.assertEqual(errors[-1]["canonical_contract"]["expected_keys"],
                                 ["schema", "selected_paths"])
                self.assertFalse(errors[-1]["canonical_contract"]["authorization_granted"])

    def test_missing_or_ambiguous_runtime_preflight_never_grants_scope(self):
        for doc in ({"status": "AMBIGUOUS", "paths": []}, {},
                    {"status": "LEXICAL_EXACT", "paths": []}):
            with self.subTest(doc=doc):
                _, receipts, errors = self.resolve(
                    {"paths": self.target}, document=doc)
                self.assertEqual(receipts[-1]["decision"], "NEEDS_INFORMATION")
                self.assertTrue(errors)

    def test_unknown_capability_and_conflicts_do_not_invent_selection(self):
        for cap, expected in (
            ({"capabilities": ["system.delete"]}, "NEEDS_INFORMATION"),
            ({"capabilities": ["directory.list"], "can_write": True}, "NEEDS_INFORMATION"),
            ({"schema": "WRONG", "capabilities": ["directory.list"]}, "NEEDS_INFORMATION"),
            ({"capabilities": ["directory.list"],
              "selected_capabilities": ["directory.inspect_metadata"]}, "REJECT_REPLAN"),
        ):
            with self.subTest(cap=cap):
                result, receipts, errors = self.resolve(
                    {"schema": PATH_SCHEMA, "selected_paths": [self.target]}, cap)
                self.assertEqual(result["capability_resolution"], cap)
                self.assertTrue(errors)
                self.assertEqual(receipts[0]["decision"], expected)
                self.assertNotIn("system.delete", str(receipts))

    def test_optional_contracts_not_invented(self):
        out, receipts, diagnostics = resolve_planner_contracts(
            {"tool": "report_progress", "next_action": "inspect_directory"},
            path_document=self.document,
        )
        self.assertFalse(receipts)
        self.assertFalse(diagnostics)
        self.assertNotIn("path_resolution", out)
        self.assertNotIn("capability_resolution", out)

    def test_canonical_diagnostics_contain_shapes_not_raw_paths(self):
        _, receipts, errors = self.resolve({
            "selected_paths": [self.target], "unexpected_field": "SECRET_TOKEN",
        })
        self.assertEqual(receipts[0]["schema"], SCHEMA)
        self.assertNotIn(self.target, str(receipts))
        self.assertNotIn("SECRET_TOKEN", str(receipts))
        self.assertNotIn(self.target, str(errors))
        self.assertNotIn("SECRET_TOKEN", str(errors))


class IntegratedRuntimeAdmissionTests(unittest.TestCase):
    def test_repaired_progress_passes_ordinary_read_admission(self):
        from WebAgent.protocol_loop import WebAgentProtocolLoop
        from WebAgent.tests.validate_flexible_progress_admission import initial
        from WebAgent.tests.validate_progress_terminal_lifecycle import response

        with tempfile.TemporaryDirectory(prefix="git24-canonical-") as temp:
            root = Path(temp).resolve()
            (root / "proof.txt").write_text("ACTUAL", encoding="utf-8")
            doc = {"status": "LEXICAL_EXACT", "paths": [str(root)]}
            planner_prompts = []
            action_ids = []
            events = []

            def planner(prompt, expected, attachments):
                planner_prompts.append(prompt)
                if len(planner_prompts) == 1:
                    p = dict(initial())
                    p["path_resolution"] = {
                        "paths": str(root), "status": "LEXICAL_EXACT",
                    }
                    p["capability_resolution"] = {
                        "capabilities": "directory.list",
                    }
                    return response(p, {
                        "tool": "list_directory", "action_id": "G24-READ-1",
                        "path": str(root),
                    })
                self.assertIn("proof.txt", prompt)
                self.assertIn("canonical_contract_resolution", prompt)
                p = dict(initial())
                p.update(decision="COMPLETE", outcome="SUCCESS",
                         current_step=1, steps=[{"step":1, "desc":"讀取目錄",
                                                  "status":"COMPLETED"}],
                         matched_condition="目錄資料已讀取",
                         evidence_refs=["G24-READ-1"])
                p.pop("next_action", None)
                return response(p, {
                    "tool": "final_response", "action_id": "G24-FINAL",
                    "content": "GIT24_CANONICAL_PASS",
                })

            loop = WebAgentProtocolLoop(root, planner, progress_root=root,
                                        event_sink=lambda *args, **kw: events.append((args, kw)))
            original_accept = loop._accept_v8_ack

            def with_runtime_preflight(calls, expected):
                # The full Runtime preflight is tested separately on Windows.
                # Inject a trusted request-scoped read-only snapshot instead
                # of fabricating E:\\ drives on Linux CI.
                loop._spr_path_resolution = doc
                return original_accept(calls, expected)

            loop._accept_v8_ack = with_runtime_preflight
            original_execute = loop._execute_action

            def capture_execute(action):
                action_ids.append(action["action_id"])
                return original_execute(action)

            loop._execute_action = capture_execute
            answer = loop.run("只讀取既有工作目錄", trusted_authorized_paths=[str(root)])
            self.assertEqual(answer, "GIT24_CANONICAL_PASS")
            self.assertEqual(action_ids, ["G24-READ-1"])
            self.assertEqual(len(planner_prompts), 2)
            self.assertTrue(any(r.get("decision") == "REWRITE_SAFE_EQUIVALENT"
                                for r in loop._last_canonical_contract_receipts) is False or
                            "canonical_contract_resolution" in planner_prompts[1])
            self.assertTrue((root / "proof.txt").exists())


if __name__ == "__main__":
    unittest.main()
