#!/usr/bin/env python3
"""Cold-start historical source recovery; no external filesystem read/writes."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core.historical_source_resolver import bootstrap_historical_sources
from agent_core.source_goal_identity import goal_identity, context_identity
from agent_core.source_reference_index import (
    index_path, read_references, publish_reference,
)
from agent_core.source_candidate_ledger import build_source_candidate_ledger
from agent_core.interrupt_readiness import interrupt_readiness
from agent_core.goal_prerequisite_resolution import resolve_goal_prerequisites
from agent_core.target_path_contract import extract_target_contract
from agent_core.task_continuation import TaskContinuationStore
from agent_core.tool_result_contract import encode_result

TARGET = "E:\\SmartAgent_GoalDriven_IT_GH_v1"
SOURCE = "E:\\PriorDeploy\\SmartAgent_GoalDriven_IT"
GOAL = (
    "驗證並完成 Case D 的部署目標。\n"
    "預期部署根目錄：\n" + TARGET + "\n"
    "預期包含：case_a、case_b、case_c、fixture_manifest.json、check_results.py。\n"
    "請先確認既有部署來源及內容完整性。"
)
REQUIRED = ["case_a", "case_b", "case_c", "fixture_manifest.json", "check_results.py"]
CONTRACT = extract_target_contract(GOAL)


def source_receipt(source=SOURCE, *, status="SUCCEEDED"):
    return encode_result(
        status,
        "DIRECTORY_LISTED" if status == "SUCCEEDED" else "DIRECTORY_IO_ERROR",
        executed=status == "SUCCEEDED",
        requested_path=source, tool="list_directory",
        data={"entries": [
            {"name": name, "kind": "FILE" if "." in name else "DIRECTORY"}
            for name in REQUIRED
        ]},
    )


class Fixture:
    def __init__(self, work: Path, store: TaskContinuationStore):
        self.workspace, self.store = work, store
        self.owner, self.instance = "telegram:owner:chat", store.instance

    def prior(self, *, goal=GOAL, result=None, owner=None, task="TASK-OLD"):
        owner = self.owner if owner is None else owner
        self.store.register(
            task_id=task, request_id="RR-OLD-" + task, epoch="epoch-old",
            goal=goal, workspace=self.workspace, owner=owner,
        )
        with self.store.lock.process_lock():
            doc = self.store._load()
            row = doc["records"][task]
            row["state"] = "INTERRUPTED"
            if result is not None:
                row["actions"] = {
                    "ACT-SOURCE-LIST": {
                        "action": {"tool": "list_directory", "path": SOURCE},
                        "result": result,
                    }
                }
                row["results"] = {
                    "ACT-SOURCE-LIST": {
                        "tool": "list_directory",
                        "result_status": "SUCCEEDED" if "[TOOL_RESULT]" in result and
                          '"status":"SUCCEEDED"' in result else "FAILED",
                        "executed": '"status":"SUCCEEDED"' in result,
                        "request_id": "RR-OLD-" + task,
                        "task_epoch": "epoch-old",
                        "evidence_status": "ACTIVE",
                    }
                }
            self.store._save(doc)

    def bootstrap(self, *, owner=None, contract=None, task_store=None, project_events=None):
        return bootstrap_historical_sources(
            agent_root=self.store.root, workspace=self.workspace,
            owner=self.owner if owner is None else owner,
            instance=self.instance,
            target_contract=CONTRACT if contract is None else contract,
            task_store=self.store if task_store is None else task_store,
            exclude_task_id="TASK-NEW",
            project_events=project_events,
        )


class HistoricalSourceBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        agent = root / "agent"
        agent.mkdir()
        work = root / "workspace"
        work.mkdir()
        self.fixture = Fixture(work, TaskContinuationStore(agent))

    def test_cold_start_recovers_only_verified_old_receipt_then_index_hit(self):
        fx = self.fixture
        fx.prior(result=source_receipt())
        first = fx.bootstrap()
        self.assertEqual(first["index_status"], "INDEX_NOT_FOUND")
        self.assertEqual(first["bootstrap_state"], "RECOVERED_FROM_HISTORY")
        self.assertEqual(first["recovered_count"], 1)
        lead = first["candidates"][0]
        self.assertEqual(lead["path"], SOURCE)
        self.assertEqual(lead["origin"], "HISTORICAL_RUNTIME_LIST_RECEIPT")
        self.assertEqual(lead["evidence_level"], "E2")
        self.assertEqual(lead["current_existence"], "UNKNOWN")
        self.assertFalse(first["authorization_granted"])
        second = fx.bootstrap()
        self.assertEqual(second["bootstrap_state"], "INDEX_HIT")
        self.assertEqual(second["task_records_examined"], 0)
        self.assertEqual(second["candidates"][0]["path"], SOURCE)

    def test_injected_history_requires_new_scope_and_current_observation(self):
        fx = self.fixture
        fx.prior(result=source_receipt())
        history = fx.bootstrap()
        rt = SimpleNamespace(
            progress_ledger=SimpleNamespace(goal=GOAL),
            run_id="RR-NEW", task_epoch="epoch-new",
            authorized_paths=[TARGET],
            action_ledger={}, action_result_ledger={},
            _target_path_contract=CONTRACT,
            _source_candidate_history=history,
        )
        gaps = {"known_absences": [{"path": TARGET}], "active_gaps": []}
        view = build_source_candidate_ledger(rt, gaps)
        self.assertEqual(view["candidate_count"], 1)
        self.assertEqual(view["candidates"][0]["evidence_level"], "E2")
        self.assertEqual(view["candidates"][0]["existence"], "UNKNOWN")
        self.assertEqual(view["candidates"][0]["scope_status"], "OUTSIDE_DECLARED_PATHS")
        available = interrupt_readiness(
            gaps, {"authorized_paths": [TARGET],
                   "read_tools": ["inspect_directory", "list_directory"]},
            source_candidates=view,
        )
        self.assertEqual(available["candidate_count"], 0)
        # User grants matching source path in a NEW request: now it is only a
        # candidate for normal Tool Admission; no admission is claimed.
        rt.authorized_paths = [TARGET, SOURCE]
        view = build_source_candidate_ledger(rt, gaps)
        available = interrupt_readiness(
            gaps, {"authorized_paths": rt.authorized_paths,
                   "read_tools": ["inspect_directory", "list_directory"]},
            source_candidates=view,
        )
        self.assertEqual(available["candidate_reads"][0]["paths"], [SOURCE])
        self.assertFalse(available["candidate_reads"][0]["authorization_granted"])

    def test_historical_original_user_source_label_is_weak_e1(self):
        fx = self.fixture
        fx.prior(goal=GOAL + "\n既有部署來源路徑：" + SOURCE)
        boot = fx.bootstrap()
        self.assertEqual(boot["recovered_count"], 1)
        self.assertEqual(boot["candidates"][0]["evidence_level"], "E1")
        self.assertEqual(boot["candidates"][0]["current_integrity"], "UNKNOWN")

    def test_cross_owner_and_workspace_are_isolated(self):
        fx = self.fixture
        fx.prior(result=source_receipt())
        other_owner = fx.bootstrap(owner="telegram:other:chat")
        self.assertEqual(other_owner["candidates"], [])
        agent = fx.store.root
        work2 = Path(self.temp.name) / "unrelated"
        work2.mkdir()
        other = bootstrap_historical_sources(
            agent_root=agent, workspace=work2,
            owner=fx.owner, instance=fx.instance,
            target_contract=CONTRACT, task_store=fx.store,
        )
        self.assertEqual(other["candidates"], [])

    def test_goal_identity_rejects_unrelated_deployment(self):
        fx = self.fixture
        fx.prior(result=source_receipt(),
                 goal=GOAL.replace(TARGET, "E:\\DifferentTarget"))
        boot = fx.bootstrap()
        self.assertEqual(boot["recovered_count"], 0)
        self.assertEqual(boot["bootstrap_state"], "NO_MATCHING_STRUCTURED_SOURCE")

    def test_failed_or_stale_receipt_cannot_be_indexed(self):
        fx = self.fixture
        fx.prior(result=source_receipt(status="FAILED"))
        boot = fx.bootstrap()
        self.assertEqual(boot["recovered_count"], 0)
        self.assertEqual(boot["candidates"], [])
        self.assertFalse(index_path(fx.store.root).exists())

    def test_no_source_in_old_case_d_does_not_invent_a_location(self):
        fx = self.fixture
        fx.prior()
        boot = fx.bootstrap()
        self.assertEqual(boot["candidates"], [])
        self.assertEqual(boot["bootstrap_state"], "NO_MATCHING_STRUCTURED_SOURCE")
        self.assertFalse(boot["new_source_filesystem_scan"])

    def test_invalid_index_is_not_reported_as_empty(self):
        fx = self.fixture
        ix = index_path(fx.store.root)
        ix.parent.mkdir(parents=True, exist_ok=True)
        ix.write_text("NOT JSON", encoding="utf-8")
        boot = fx.bootstrap()
        self.assertEqual(boot["index_status"], "INDEX_INVALID")
        self.assertEqual(boot["candidates"], [])

    def test_index_is_bound_by_goal_owner_workspace_and_instance(self):
        fx = self.fixture
        ident = goal_identity(CONTRACT)
        ok = publish_reference(
            fx.store.root, owner=fx.owner, instance=fx.instance,
            workspace=str(fx.workspace), goal_digest=ident["digest"],
            path=SOURCE, provenance_ref="ACT1", origin="PREVIOUS_TASK_RECEIPT",
            evidence_level="E2",
        )
        self.assertTrue(ok)
        other = read_references(
            fx.store.root, owner="other", instance=fx.instance,
            workspace=str(fx.workspace), goal_digest=ident["digest"],
        )
        self.assertEqual(other["candidates"], [])
        self.assertEqual(fx.bootstrap()["bootstrap_state"], "INDEX_HIT")
        # Weaker followup must not downgrade a structurally witnessed hint.
        self.assertTrue(publish_reference(
            fx.store.root, owner=fx.owner, instance=fx.instance,
            workspace=str(fx.workspace), goal_digest=ident["digest"],
            path=SOURCE, provenance_ref="USER-LABEL", origin="HISTORICAL_USER_SOURCE_LABEL",
            evidence_level="E1",
        ))
        self.assertEqual(fx.bootstrap()["candidates"][0]["evidence_level"], "E2")

    def test_project_ledger_event_needs_context_and_goal_identity(self):
        fx = self.fixture
        ident = goal_identity(CONTRACT)
        details = {
            "context_digest": context_identity(fx.owner, fx.instance, str(fx.workspace)),
            "goal_digest": ident["digest"], "source_path": SOURCE,
        }
        rejected = fx.bootstrap(project_events=[
            {"event_type": "source_reference_verified", "event_id": "EVT-X",
             "details": {**details, "context_digest": "wrong"}},
        ])
        self.assertEqual(rejected["candidates"], [])
        good = fx.bootstrap(project_events=[
            {"event_type": "source_reference_verified", "event_id": "EVT-X",
             "details": details},
        ])
        self.assertEqual(good["candidates"][0]["origin"], "PROJECT_LEDGER")
        self.assertEqual(good["candidates"][0]["evidence_level"], "E1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
