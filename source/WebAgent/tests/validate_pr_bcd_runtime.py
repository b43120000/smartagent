"""Offline PR-B/C/D requirements probe, authorization, durable recovery and refusal tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.prerequisite_runtime_v3 import (
    SCHEMA, PrerequisiteRecoveryError, RecoveryLedger,
    probe_requirements, summary,
)


class PRBCDTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pr-bcd-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.authorized = self.root / "allowed"
        self.authorized.mkdir()
        self.env = {"tools":{
            "python":{"status":"AVAILABLE","version":"Python 3.11.7"},
            "git":{"status":"AVAILABLE","version":"git version 2.43.1"},
        }}
        self.ids = dict(task_id="TASK-BCD-01", request_id="RR-BCD-01",
                        task_epoch="EPOCH-BCD-01")
        self.intent = "a"*64
        self.ledger = RecoveryLedger(self.root)

    def probe(self, request=None, paths=None):
        return probe_requirements(
            request=request if request is not None else
            "Please install package synthetic_pr_bcd>=1.0 using the authorized offline wheel",
            authorized_paths=[str(self.authorized)] if paths is None else paths,
            environment_snapshot=self.env, install_root=self.root,
        )

    def init(self, probe):
        return self.ledger.initialize(
            **self.ids, intent_sha256=self.intent, probe=probe,
            graph_digest="b"*64,
        )

    def mutate(self, action_id="MUT-1"):
        return {"tool":"run_command","operation":"MUTATE",
                "action_id":action_id,
                "command":"python -m pip install --no-index --target deps synthetic_pr_bcd.whl"}

    def action(self, obj, status="SUCCEEDED", executed=True, verify="PASS", ref="RES-1"):
        return self.ledger.action_result(
            **self.ids,action=obj,result={
                "result_status":status,"executed":executed,
                "effective_verification_status":verify,
            },evidence_ref=ref,
        )

    def test_b_probes_exact_authorized_paths_without_enumeration(self):
        missing = self.authorized/"not-yet-existing.txt"
        facts = self.probe("Read file", paths=[str(self.authorized),str(missing)])
        ids = {x["id"]:x for x in facts["facts"]}
        self.assertEqual(ids["path:0"]["state"],"SATISFIED")
        self.assertEqual(ids["path:1"]["state"],"MISSING")
        self.assertEqual(facts["missing"],["path:1"])
        self.assertFalse(facts["remediation_requested"])
        self.assertNotIn(str(self.authorized), json.dumps(facts))

    def test_b_versions_are_numeric_and_fail_closed(self):
        found = self.probe("python>=3.11 and git>=2.99",paths=[])
        ids = {x["id"]:x for x in found["facts"]}
        self.assertEqual(ids["version:python"]["state"],"SATISFIED")
        self.assertEqual(ids["version:git"]["state"],"MISSING")
        self.assertNotIn("path:0",ids)
        self.assertFalse(found["remediation_requested"])

    def test_c_missing_output_file_not_reclassified_as_environment_remediation(self):
        target=self.authorized/"result.txt"
        fact=self.probe("Create result.txt within the allowed directory",
                        paths=[str(target)])
        self.assertEqual(fact["missing"],["path:0"])
        self.assertFalse(fact["remediation_requested"])
        self.init(fact)
        state=self.ledger.before_action(
            **self.ids,action={"tool":"run_command","operation":"MUTATE",
                               "action_id":"MUT-OUTPUT","command":"echo write result.txt"},
            scope_allowed=True)
        self.assertEqual(state["phase"],"MISSING")
        self.assertEqual(state["remediation_action"],"")

    def test_b_bare_distribution_constraint_probed_from_actual_live_wording(self):
        request = "確認 smartagent-pr-bcd-live>=1.0 是否缺失，禁止任何補救。"
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING", "")) as probe:
            result = self.probe(request, paths=[str(self.authorized)])
        self.assertEqual(probe.call_count, 1)
        fact = next(x for x in result["facts"]
                    if x["id"] == "package:smartagent-pr-bcd-live")
        self.assertEqual(fact["state"], "MISSING")
        self.assertFalse(result["remediation_requested"])

    def test_b_unspecified_wheel_request_stays_unknown_and_cannot_install(self):
        request = ("使用 E:\\SmartAgent_PR_BCD_LIVE 已準備的 Wheel，"
                   "依序完成 INSPECT、MUTATE、獨立 VERIFY、重新 INSPECT。")
        probe = self.probe(request, paths=[str(self.authorized)])
        self.assertIn("package:unspecified", probe["unknown"])
        self.assertFalse(probe["remediation_eligible"])
        self.init(probe)
        with self.assertRaisesRegex(
            PrerequisiteRecoveryError, "pr_c_missing_probe_required"
        ):
            self.ledger.before_action(
                **self.ids, action=self.mutate(), scope_allowed=True,
            )

    def test_b_distribution_is_probed_with_bundled_python_only(self):
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING","")) as probe:
            row = self.probe()
        self.assertEqual(probe.call_count,1)
        ids = {x["id"]:x for x in row["facts"]}
        self.assertEqual(ids["package:synthetic_pr_bcd"]["state"],"MISSING")
        self.assertTrue(row["remediation_requested"])
        self.assertFalse(row["authorization_granted"])

    def test_c_d_durable_four_stage_handoff(self):
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING","")):
            probe = self.probe()
        doc = self.init(probe)
        self.assertEqual(doc["phase"],"MISSING")
        self.assertEqual(self.ledger.read(self.ids["task_id"])["revision"],1)
        before = self.ledger.before_action(
            **self.ids,action=self.mutate(),scope_allowed=True)
        self.assertEqual(before["phase"],"REMEDIATING")
        self.assertFalse(self.ledger.terminal_ready(self.ids["task_id"])[0])
        installed = self.action(self.mutate(),ref="RES-MUT")
        self.assertEqual(installed["phase"],"REMEDIATED_PENDING_VERIFY")
        verify = {"tool":"run_command","operation":"VERIFY",
                  "action_id":"VERIFY-1","verifies_action_id":"MUT-1"}
        independent = self.action(verify,ref="RES-VERIFY")
        self.assertEqual(independent["phase"],"VERIFIED")
        self.assertFalse(self.ledger.terminal_ready(self.ids["task_id"])[0])
        retry = {"tool":"run_command","operation":"INSPECT","action_id":"RETRY-1",
                 "depends_on":["VERIFY-1"]}
        resumed = self.action(retry,ref="RES-RETRY")
        self.assertEqual(resumed["phase"],"RESUMED")
        restarted = RecoveryLedger(self.root)
        self.assertTrue(restarted.terminal_ready(self.ids["task_id"])[0])
        report = summary(restarted.read(self.ids["task_id"]))
        self.assertTrue(report["verified"])
        self.assertFalse(report["task_result_issued"])
        self.assertNotIn("RES-VERIFY",json.dumps(resumed))
        self.assertEqual(len(resumed["events"]),5)

    def test_c_requires_user_positive_intent_and_scope(self):
        p = self.probe("Only inspect package synthetic_pr_bcd>=1.0")
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING","")):
            p = self.probe("Only inspect package synthetic_pr_bcd>=1.0")
        self.assertFalse(p["remediation_requested"])
        self.init(p)
        with self.assertRaisesRegex(PrerequisiteRecoveryError, "authorization_required"):
            self.ledger.before_action(**self.ids,action=self.mutate(),scope_allowed=True)
        self.assertEqual(self.ledger.read(self.ids["task_id"])["phase"],"MISSING")

    def test_c_negative_authorization_is_not_remediation_permission(self):
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING","")):
            for text in (
                "Do not install package synthetic_pr_bcd>=1.0",
                "Please inspect package synthetic_pr_bcd>=1.0 without installation",
                "不要安裝 package synthetic_pr_bcd>=1.0",
            ):
                with self.subTest(text=text):
                    fact = self.probe(text)
                    self.assertTrue(fact["missing"])
                    self.assertFalse(fact["remediation_requested"])
                    self.assertFalse(fact["authorization_granted"])

    def test_c_denied_scope_no_remediation_tracking(self):
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING","")):
            self.init(self.probe())
        with self.assertRaisesRegex(PrerequisiteRecoveryError, "authorization_required"):
            self.ledger.before_action(**self.ids,action=self.mutate(),scope_allowed=False)
        self.assertEqual(self.ledger.read(self.ids["task_id"])["phase"],"MISSING")

    def test_c_mutation_failure_blocks_synthetic_success(self):
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING","")):
            self.init(self.probe())
        self.ledger.before_action(**self.ids,action=self.mutate(),scope_allowed=True)
        failed = self.action(self.mutate(),executed=False,status="REJECTED")
        self.assertEqual(failed["phase"],"BLOCKED")
        self.assertFalse(self.ledger.terminal_ready(self.ids["task_id"])[0])

    def test_c_duplicate_remediation_replay_is_rejected(self):
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING","")):
            self.init(self.probe())
        self.ledger.before_action(**self.ids,action=self.mutate(),scope_allowed=True)
        with self.assertRaisesRegex(PrerequisiteRecoveryError,"replay_blocked"):
            self.ledger.before_action(**self.ids,action=self.mutate("MUT-NEW"),scope_allowed=True)
        self.assertEqual(self.ledger.read(self.ids["task_id"])["phase"],"REMEDIATING")

    def test_d_unrelated_verify_cannot_release(self):
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING","")):
            self.init(self.probe())
        self.ledger.before_action(**self.ids,action=self.mutate(),scope_allowed=True)
        self.action(self.mutate())
        bad = {"tool":"run_command","operation":"VERIFY",
               "action_id":"VERIFY-X","verifies_action_id":"OTHER"}
        result = self.action(bad)
        self.assertEqual(result["phase"],"REMEDIATED_PENDING_VERIFY")
        self.assertFalse(self.ledger.terminal_ready(self.ids["task_id"])[0])

    def test_d_verifier_unknown_or_fail_fails_closed(self):
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING","")):
            self.init(self.probe())
        self.ledger.before_action(**self.ids,action=self.mutate(),scope_allowed=True)
        self.action(self.mutate())
        verify = {"tool":"run_command","operation":"VERIFY",
                  "action_id":"VERIFY-X","verifies_action_id":"MUT-1"}
        result = self.action(verify,verify="UNKNOWN")
        self.assertEqual(result["phase"],"BLOCKED")
        self.assertFalse(self.ledger.terminal_ready(self.ids["task_id"])[0])

    def test_d_retry_requires_fresh_id_and_evidence_pass(self):
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING","")):
            self.init(self.probe())
        self.ledger.before_action(**self.ids,action=self.mutate(),scope_allowed=True)
        self.action(self.mutate())
        self.action({"tool":"run_command","operation":"VERIFY",
                     "action_id":"VERIFY-1","verifies_action_id":"MUT-1"})
        old = self.action({"tool":"run_command","operation":"INSPECT",
                           "action_id":"MUT-1"})
        self.assertEqual(old["phase"],"VERIFIED")
        bad = self.action({"tool":"read_file","action_id":"RETRY-1"},verify="UNKNOWN")
        self.assertEqual(bad["phase"],"VERIFIED")

    def test_repeated_init_preserves_mutations_and_rejects_epoch(self):
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING","")):
            p=self.probe()
        doc=self.init(p)
        self.assertEqual(self.init(p)["digest"],doc["digest"])
        with self.assertRaises(PrerequisiteRecoveryError):
            self.ledger.initialize(task_id=self.ids["task_id"],
                request_id="RR-FOREIGN",task_epoch=self.ids["task_epoch"],
                intent_sha256=self.intent,probe=p,graph_digest="b"*64)

    def test_d_tamper_detected(self):
        with patch("agent_core.prerequisite_runtime_v3._probe_package",
                   return_value=("MISSING","")):
            self.init(self.probe())
        path=self.ledger._path(self.ids["task_id"])
        contents=path.read_text(encoding="utf-8")
        path.write_text(contents.replace('"phase": "MISSING"',
                                        '"phase": "RESUMED"',1),encoding="utf-8")
        with self.assertRaises(PrerequisiteRecoveryError):
            self.ledger.read(self.ids["task_id"])

if __name__=="__main__":
    unittest.main(verbosity=2)
