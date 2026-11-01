"""PR-A requirement DAG / durable ledger regression, no live tools."""
from __future__ import annotations
import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_core.prerequisite_graph_v3 import (
    PrerequisiteGraphError, RequirementLedger, SCHEMA, _seal,
    build_requirement_graph, graph_summary,
)
from agent_core.task_requirement import assess_task_requirement


class PRAGraphRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pr-a-regression-")
        self.addCleanup(self.tmp.cleanup)
        self.store = RequirementLedger(self.tmp.name)
        self.ids = dict(task_id="TASK-PR-A-0001", request_id="RR-PR-A-0001",
                        task_epoch="epoch-001")
        self.assessment = assess_task_requirement(
            "請查詢 GitHub 上 b43120000/smartagent Repository main 最新 Commit，只做唯讀查詢"
        )
        self.graph = build_requirement_graph(
            **self.ids, assessment=self.assessment, authorized_paths=[],
        )

    def initialize(self):
        return self.store.initialize(self.graph)

    def bind(self, action="ACT-1", **kw):
        return self.store.bind_action(**self.ids, action_id=action, tool="run_command", **kw)

    def test_github_requirement_graph_is_not_permission(self):
        self.assertEqual(self.graph["schema"], SCHEMA)
        self.assertIn("source:github", self.graph["nodes"])
        self.assertIn("operation:read", self.graph["nodes"])
        self.assertIn("capability:github_repository_access", self.graph["nodes"])
        self.assertIn("source:github",
                      self.graph["nodes"]["operation:read"]["depends_on"])
        self.assertFalse(graph_summary(self.graph)["authorizes_execution"])
        self.assertEqual(self.graph["nodes"]["task:goal"]["state"], "PENDING")
        self.assertNotIn("b43120000", json.dumps(self.graph))

    def test_durable_initialize_restart_and_idempotency(self):
        self.initialize()
        self.bind()
        self.store.record_action_evidence(
            **self.ids, action_id="ACT-1", status="SATISFIED", evidence_ref="RES-1"
        )
        restarted = RequirementLedger(self.tmp.name)
        before = restarted.read(self.ids["task_id"])
        self.assertEqual(before["nodes"]["action:act-1"]["state"], "SATISFIED")
        after = restarted.initialize(self.graph)
        self.assertEqual(before["digest"], after["digest"])
        again = restarted.record_action_evidence(
            **self.ids, action_id="ACT-1", status="SATISFIED", evidence_ref="RES-1"
        )
        self.assertEqual(again["revision"], before["revision"])

    def test_scopes_are_hashed_and_non_authorizing(self):
        local = build_requirement_graph(
            **self.ids, assessment=assess_task_requirement(
                "請讀取 C:\\private\\camera\\sensor.txt"
            ), authorized_paths=["C:\\private\\camera\\sensor.txt"],
        )
        self.assertNotIn("C:\\private", json.dumps(local))
        self.assertTrue(any(k.startswith("scope:") for k in local["nodes"]))
        self.store.initialize(local)
        result = self.bind(scope_paths=["C:\\private\\new_scope.txt"])
        self.assertEqual(result["nodes"]["action:act-1"]["state"], "PENDING")
        self.assertFalse(graph_summary(result)["authorizes_execution"])
        self.assertNotIn("new_scope", json.dumps(result))

    def test_action_dependencies_are_dag_and_forward_fail(self):
        self.initialize()
        with self.assertRaisesRegex(PrerequisiteGraphError, "dependency_not_bound"):
            self.bind(action="ACT-2", depends_on=["ACT-1"])
        self.bind(action="ACT-1")
        graph = self.bind(action="ACT-2", depends_on=["ACT-1"])
        self.assertIn("action:act-1", graph["nodes"]["action:act-2"]["depends_on"])
        with self.assertRaisesRegex(PrerequisiteGraphError, "action_dependencies_invalid"):
            self.bind(action="ACT-3", depends_on=["ACT-3"])
        with self.assertRaisesRegex(PrerequisiteGraphError, "action_rebind_conflict"):
            self.bind(action="ACT-2", depends_on=[])

    def test_cycle_detection_even_when_digest_resealed(self):
        graph = json.loads(json.dumps(self.graph))
        graph["nodes"]["task:intent"]["depends_on"] = ["task:goal"]
        with self.assertRaisesRegex(PrerequisiteGraphError, "cycle"):
            _seal(graph)

    def test_tampered_and_corrupted_ledger_cannot_reset(self):
        self.initialize()
        path = self.store._path(self.ids["task_id"])
        text = path.read_text(encoding="utf-8").replace('"PENDING"', '"SATISFIED"', 1)
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(PrerequisiteGraphError):
            self.store.read(self.ids["task_id"])
        with self.assertRaises(PrerequisiteGraphError):
            self.initialize()

    def test_cross_request_and_epoch_fail_closed(self):
        self.initialize()
        with self.assertRaisesRegex(PrerequisiteGraphError, "request_epoch_mismatch"):
            self.store.bind_action(task_id=self.ids["task_id"],
                request_id="RR-OTHER", task_epoch=self.ids["task_epoch"],
                action_id="ACT-1", tool="read_file")
        mismatched = build_requirement_graph(
            task_id=self.ids["task_id"], request_id="RR-OTHER",
            task_epoch=self.ids["task_epoch"], assessment=self.assessment)
        with self.assertRaisesRegex(PrerequisiteGraphError, "initial_identity_conflict"):
            self.store.initialize(mismatched)

    def test_evidence_requires_result_and_cannot_change(self):
        self.initialize()
        self.bind()
        with self.assertRaisesRegex(PrerequisiteGraphError, "success_requires_evidence"):
            self.store.record_action_evidence(**self.ids, action_id="ACT-1",
                                              status="SATISFIED")
        self.store.record_action_evidence(
            **self.ids, action_id="ACT-1", status="UNKNOWN", evidence_ref="RES-A")
        with self.assertRaisesRegex(PrerequisiteGraphError, "evidence_conflict"):
            self.store.record_action_evidence(
                **self.ids, action_id="ACT-1", status="SATISFIED", evidence_ref="RES-B")
        saved = json.dumps(self.store.read(self.ids["task_id"]))
        self.assertNotIn("RES-A", saved)
        self.assertIn(hashlib.sha256(b"RES-A").hexdigest(), saved)

    def test_concurrent_actions_have_unique_revision(self):
        self.initialize()
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(self.bind, f"ACT-{i}") for i in range(1, 13)]
            for future in futures:
                future.result(timeout=30)
        graph = self.store.read(self.ids["task_id"])
        self.assertEqual(len([k for k in graph["nodes"] if k.startswith("action:")]), 12)
        self.assertEqual(graph["revision"], len(graph["events"]))
        self.assertEqual(len(set(event["seq"] for event in graph["events"])),
                         graph["revision"])

    def test_invalid_unknown_scope_and_identity(self):
        with self.assertRaises(PrerequisiteGraphError):
            build_requirement_graph(task_id="../bad", request_id="RR-1",
                                    task_epoch="epoch-1", assessment=self.assessment)
        with self.assertRaises(PrerequisiteGraphError):
            build_requirement_graph(**self.ids,
                assessment={**self.assessment, "required_capabilities": ["BAD/INJECTION"]})
        self.initialize()
        with self.assertRaisesRegex(PrerequisiteGraphError, "action_unknown"):
            self.store.record_action_evidence(
                **self.ids, action_id="ACT-MISSING", status="UNKNOWN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
