"""R1/R3 regression: bounded GAR escalation and independently disabled L4."""
from __future__ import annotations
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_core.bounded_repair import BoundedRepairBudget
from agent_core.gar_webgpt2_settings import apply_target, ENV_KEY, GAR_FLAG
from agent_core.goal_anchored_recovery import (
    RecoveryEngine, enabled, l4_enabled
)
from agent_core.runtime_config import load_registry, resolve_features


class GARR1R3Regression(unittest.TestCase):
    def engine(self):
        return RecoveryEngine(request_id="RR-GAR-R1", task_id="TASK-GAR-R1",
                              task_epoch="EPOCH-GAR-R1", original_request="Read-only hello")

    def checkpoint(self, engine, *, pending=None, blockers=None, prerequisites=()):
        return engine.snapshot(
            identity={"request_id":engine.request_id, "task_id":engine.task_id,
                      "task_epoch":engine.task_epoch},
            verified_evidence={}, blockers=blockers or [],
            pending_operations=pending or {},
            verification_preconditions=prerequisites,
        )

    def decision(self, engine, budget, reason, *, pending=None, prerequisites=()):
        return engine.planner_rejection(
            reason=reason, snapshot=self.checkpoint(
                engine, pending=pending, prerequisites=prerequisites),
            response_fresh=True, evidence_generation="no-new-runtime-evidence",
            local_repair_budget=budget,
        )

    def test_variable_reject_codes_escalate_l1_l2_l3_l5_l4_off(self):
        engine = self.engine()
        local = BoundedRepairBudget()
        reasons = [
            "terminal_success_verification_missing",
            "invalid_progress_payload",
            "partial_not_supported_or_recoverable_work",
            "invalid_progress_payload",
            "missing_context_requires_runtime_read",
            "terminal_success_verification_missing",
            "invalid_progress_payload",
            "missing_context_requires_runtime_read",
        ]
        levels = []
        with patch.dict(os.environ, {"SMARTAGENT_GAR_L4_ENABLED":"0"}):
            for reason in reasons:
                budget = local.observe([{"reason":reason}], evidence_generation=0)
                result = self.decision(engine, budget, reason)
                levels.append((result.level, result.status, result.reason))
        self.assertEqual([x[0] for x in levels], ["L1"]*5 + ["L2","L3","L5"])
        self.assertEqual(levels[5][2], "GAR_PROGRESS_REBASE")
        self.assertEqual(levels[-1], ("L5","PAUSED","GAR_L4_DISABLED"))
        self.assertEqual(engine.rebases, 1)
        self.assertEqual(engine.replans, 1)
        self.assertEqual(engine.fresh_sessions, 0)
        self.assertEqual(local.rejected_turns, len(reasons))

    def test_l4_on_after_l2_l3_can_request_but_does_not_activate(self):
        engine = self.engine()
        local = BoundedRepairBudget()
        with patch.dict(os.environ, {"SMARTAGENT_GAR_L4_ENABLED":"1"}):
            outcomes = []
            for i in range(8):
                reason = ["P_INVALID","T_MISSING","CONTEXT_MISSING"][i%3]
                budget = local.observe([{"reason":reason}], evidence_generation=0)
                outcomes.append(self.decision(engine, budget, reason))
        self.assertEqual([x.level for x in outcomes[-3:]], ["L2","L3","L4"])
        self.assertEqual(outcomes[-1].status, "PREPARE")
        self.assertEqual(engine.fresh_sessions, 0)

    def test_unadmitted_progress_hold_never_escalates_to_l2(self):
        engine = self.engine()
        local = BoundedRepairBudget()
        with patch.dict(os.environ, {"SMARTAGENT_GAR_L4_ENABLED":"0"}):
            for i in range(6):
                reason = ["INVALID_PROGRESS","INVALID_TERMINAL"][i%2]
                budget = local.observe([{"reason":reason}], evidence_generation=0)
                result = self.decision(engine, budget, reason,
                    prerequisites=("PRESERVED_UNADMITTED_PROGRESS_REPAIR",))
        self.assertEqual((result.level,result.reason),
                         ("L5","GAR_PRESERVED_PROGRESS_REPAIR_EXHAUSTED"))
        self.assertEqual(engine.rebases,0)
        self.assertEqual(engine.replans,0)

    def test_unsafe_unknown_action_blocks_before_l2(self):
        engine = self.engine()
        local = BoundedRepairBudget()
        with patch.dict(os.environ, {"SMARTAGENT_GAR_L4_ENABLED":"0"}):
            for i in range(6):
                budget = local.observe([{"reason":"INVALID_"+str(i)}], evidence_generation=0)
                result = self.decision(engine,budget,"INVALID_"+str(i),
                                       pending={"unknown_action_outcome": True})
        self.assertEqual((result.level,result.reason),
                         ("L5","GAR_PENDING_OR_BLOCKED_OPERATION"))
        self.assertEqual(engine.rebases,0)

    def test_l4_disabled_blocks_supervisor_review_and_proof(self):
        engine = self.engine()
        with patch.dict(os.environ, {"SMARTAGENT_GAR_L4_ENABLED":"false"}):
            result = engine.request_supervisory_review(
                snapshot=self.checkpoint(engine),
                reason="semantic_stagnation:repeated_protocol_rejection")
            self.assertEqual((result.level,result.reason),("L5","GAR_L4_DISABLED"))
            self.assertEqual(engine.fresh_sessions,0)

    def test_l4_switch_and_url_independent_when_gar_configured(self):
        env = {
            ENV_KEY: "https://chatgpt.com/c/gar-test-fresh",
            "SMARTAGENT_GAR_L4_ENABLED": "0",
        }
        result = apply_target("https://chatgpt.com/c/gar-test-original", environ=env)
        self.assertTrue(result)
        self.assertEqual(env[GAR_FLAG],"1")
        self.assertTrue(enabled(env))
        self.assertFalse(l4_enabled(env))
        self.assertTrue(l4_enabled({"SMARTAGENT_GAR_L4_ENABLED":"1"}))
        self.assertFalse(l4_enabled({}))
        self.assertFalse(l4_enabled({"SMARTAGENT_GAR_L4_ENABLED":""}))

    def test_l4_flag_registered_for_remote_and_webdirect(self):
        registry = load_registry()
        spec = registry["features"]["gar_l4_enabled"]
        self.assertEqual(spec["env"],"SMARTAGENT_GAR_L4_ENABLED")
        self.assertFalse(spec["default"])
        self.assertEqual(set(spec["agents"]),{"remote_agent","web_direct"})
        root = Path(__file__).resolve().parents[2]
        with patch.dict(os.environ, {"SMARTAGENT_GAR_L4_ENABLED":"0"}):
            self.assertFalse(resolve_features("remote_agent")["flags"]["gar_l4_enabled"])
            self.assertFalse(resolve_features("web_direct")["flags"]["gar_l4_enabled"])
        remote = (root/"RemoteAgent"/"telegram_webagent_worker.py").read_text(encoding="utf-8")
        controller = (root/"WebAgent"/"controller.py").read_text(encoding="utf-8")
        self.assertIn("if gar_fresh_url and l4_enabled():", remote)
        self.assertIn("if fresh_target and l4_enabled():", controller)
        self.assertIn("gar_enabled() and l4_enabled()", controller)

    def test_default_disabled_without_url_mutation(self):
        from agent_core.runtime_config import resolve_features
        self.assertFalse(l4_enabled({}))
        self.assertFalse(resolve_features("remote_agent", environ={})["flags"]["gar_l4_enabled"])
        self.assertFalse(resolve_features("web_direct", environ={})["flags"]["gar_l4_enabled"])
        self.assertTrue(l4_enabled({"SMARTAGENT_GAR_L4_ENABLED":"1"}))
        engine = self.engine()
        budget = BoundedRepairBudget()
        with patch.dict(os.environ, {"SMARTAGENT_GAR_L4_ENABLED":"0"}):
            decisions = []
            for idx in range(8):
                reason = "PROGRESS_" + str(idx)
                state = budget.observe([{"reason":reason}], evidence_generation=0)
                decisions.append(self.decision(engine,state,reason))
        self.assertEqual([x.level for x in decisions[-3:]], ["L2","L3","L5"])
        self.assertEqual(decisions[-1].reason, "GAR_L4_DISABLED")

    def test_v9_targeted_repair_guidance_is_present(self):
        source = (Path(__file__).resolve().parents[1]/"protocol_loop.py").read_text(encoding="utf-8")
        for marker in ("[PROGRESS_TERMINAL_CONTRACT]","[TERMINAL_EVIDENCE_REPAIR]",
                       "[CONTEXT_READ_REPAIR]","[PARTIAL_REPAIR]"):
            self.assertIn(marker,source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
