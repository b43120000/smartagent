"""GAR Progress held-Action handoff; no browser, network or real command execution.

This tests the exact safety boundary from Telegram TASK-93F821E340E6E0D97DB3:
progress evidence_refs=[] -> unadmitted action preserved -> bounded L1
instead of false UNKNOWN execution. Actual V9 admission and tool dispatch
remain independent and mandatory.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from WebAgent.protocol_loop import WebAgentProtocolLoop
from agent_core.goal_anchored_recovery import RecoveryEngine
from agent_core.task_continuation import TaskContinuationStore


GOAL = ("請查詢 GitHub 上 b43120000/smartagent Repository 的 main branch 最新 Commit，"
        "僅做唯讀查詢，不要修改或推送任何資料。")
RR = "RR-GAR-PROGRESS-1"
TASK = "TASK-GAR-PROGRESS-1"
EPOCH = "EPOCH-GAR-PROGRESS-1"


def unadmitted_command(root):
    return {
        "tool": "run_command",
        "action_id": "git-inspect-main-r001",
        "operation": "GIT_INSPECT",
        "command": "git ls-remote https://github.com/b43120000/smartagent.git refs/heads/main",
        "working_directory": str(root),
    }


def make_loop(root):
    loop = WebAgentProtocolLoop(
        root, lambda _p, _e, _a: "",
        progress_root=root,
        max_turns=2,
    )
    loop.run_id = RR
    loop.task_id = TASK
    loop.task_epoch = EPOCH
    loop.task_request = GOAL
    loop.turn_id = 1
    loop._gar = RecoveryEngine(
        request_id=RR, task_id=TASK, task_epoch=EPOCH,
        original_request=GOAL,
    )
    loop.pending_progress_repair_actions = [unadmitted_command(root)]
    return loop


class GARUnadmittedProgressHoldIT(unittest.TestCase):
    def test_first_round_unadmitted_progress_hold_is_l1_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            loop = make_loop(root)
            snap = loop._gar_snapshot()
            self.assertEqual(snap["pending_operations"], [])
            self.assertEqual(snap["blockers"], [])
            self.assertIn("PRESERVED_UNADMITTED_PROGRESS_REPAIR",
                          snap["verification_preconditions"])
            decision = loop._gar.planner_rejection(
                reason="invalid_progress_payload",
                snapshot=snap,
                response_fresh=True,
                evidence_generation="no-runtime-evidence",
                local_repair_budget={
                    "status": "REPAIR_ALLOWED",
                    "rejected_turns_total": 1,
                    "max_total": 16,
                },
            )
            self.assertEqual((decision.level, decision.status),
                             ("L1", "REPAIR"))
            self.assertEqual(decision.reason, "GAR_PRESERVED_PROGRESS_REPAIR_LOCAL")
            self.assertEqual(loop.pending_progress_repair_actions[0]["action_id"],
                             "git-inspect-main-r001")
            self.assertEqual(loop.action_ledger, {})
            self.assertEqual(loop.action_result_ledger, {})
            self.assertEqual(loop._gar.rebases, 0)
            self.assertEqual(loop._gar.replans, 0)

    def test_no_unbounded_replan_if_local_progress_repair_exhausted(self):
        with tempfile.TemporaryDirectory() as temp:
            loop = make_loop(Path(temp))
            snap = loop._gar_snapshot()
            decision = loop._gar.planner_rejection(
                reason="invalid_progress_payload", snapshot=snap,
                response_fresh=True, evidence_generation="none",
                local_repair_budget={
                    "status": "EXHAUSTED",
                    "rejected_turns_total": 1,
                    "max_total": 16,
                },
            )
            self.assertEqual((decision.level, decision.status), ("L5", "PAUSED"))
            self.assertEqual(decision.reason, "GAR_PRESERVED_PROGRESS_REPAIR_EXHAUSTED")
            self.assertEqual(loop._gar.rebases, 0)
            self.assertEqual(loop._gar.replans, 0)

    def test_uncertain_or_prepared_operation_still_l5(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            loop = make_loop(root)
            loop.action_ledger["earlier-run"] = {
                "execution_status": "UNKNOWN",
                "executed": True,
            }
            snap = loop._gar_snapshot()
            self.assertTrue(snap["pending_operations"])
            verdict = loop._gar.planner_rejection(
                reason="invalid_progress_payload", snapshot=snap,
                response_fresh=True, evidence_generation="none",
            )
            self.assertEqual((verdict.level, verdict.status, verdict.reason),
                             ("L5", "PAUSED", "GAR_PENDING_OR_BLOCKED_OPERATION"))

            loop.action_ledger.clear()
            loop.continuation_store = TaskContinuationStore(root)
            loop.continuation_store.register(
                task_id=TASK, request_id=RR, epoch=EPOCH, goal=GOAL,
                owner="gar-test", workspace=root,
            )
            loop.continuation_store.prepare_action(TASK, {
                "tool": "write_file", "action_id": "prepared-write",
                "path": str(root / "mutated.txt"), "content": "danger",
            })
            snap = loop._gar_snapshot()
            self.assertNotIn("PRESERVED_UNADMITTED_PROGRESS_REPAIR",
                             snap["verification_preconditions"])
            self.assertTrue(snap["pending_operations"])

    def test_request_or_epoch_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            loop = make_loop(root)
            store = TaskContinuationStore(root)
            store.register(task_id=TASK, request_id=RR,
                           epoch="OLD-EPOCH", goal=GOAL,
                           owner="gar-test", workspace=root)
            loop.continuation_store = store
            snap = loop._gar_snapshot()
            self.assertNotIn("PRESERVED_UNADMITTED_PROGRESS_REPAIR",
                             snap["verification_preconditions"])
            self.assertTrue(snap["pending_operations"])

    def test_crash_metadata_is_not_replayed_in_new_epoch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = TaskContinuationStore(root)
            store.register(task_id=TASK, request_id=RR, epoch=EPOCH,
                           goal=GOAL, owner="gar-test", workspace=root)
            loop = make_loop(root)
            loop.continuation_store = store
            store.snapshot(loop)
            old = store.get(TASK)
            hold = old["pending"]["unadmitted_progress_repair_hold"]
            self.assertEqual(hold["status"], "INVALIDATE_ON_CONTINUATION")
            self.assertEqual(hold["action_ids"], ["git-inspect-main-r001"])
            self.assertEqual(hold["request_id"], RR)
            self.assertEqual(hold["task_epoch"], EPOCH)
            self.assertEqual(len(hold["content_sha256"]), 64)
            self.assertNotIn("command", str(hold))
            self.assertFalse(old["actions"])
            self.assertFalse(old["prepared"])
            self.assertFalse(old["results"])

            events = []
            resumed = WebAgentProtocolLoop(
                root, lambda _p, _e, _a: "",
                progress_root=root,
                event_sink=lambda name, **kw: events.append((name, kw)),
            )
            resumed.task_id = "TASK-NEW-REQUEST"
            resumed.run_id = "RR-NEW-REQUEST"
            resumed.task_epoch = "EPOCH-NEW"
            resumed.task_request = GOAL
            resumed.continuation_source = old
            resumed.continuation_mode = "RESUME"
            resumed._restore_continuation()
            self.assertEqual(resumed.pending_progress_repair_actions, [])
            self.assertTrue(any(
                name == "progress_repair_hold_invalidated_on_continuation"
                for name, _ in events
            ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
