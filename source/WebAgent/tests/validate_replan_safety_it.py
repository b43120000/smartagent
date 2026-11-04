#!/usr/bin/env python3
"""P1-P3 negative safety regressions for evidence-gated replanning.

All receipts here are deterministic Runtime-stand-ins. No external network,
real file mutation, or model-generated authority is required.
"""
from __future__ import annotations

import unittest

from agent_core.replan_safety import (
    ReplanSafetyError, assess_recovery_routes, admit_strategy_revision,
)


GOAL = "a" * 64
ROUTES = ["url.direct", "url.https_alternate", "url.proxy_allowed"]


def receipt(route, result="FAILED", ref="EV-1", **extra):
    row = dict(route_id=route, result=result, verified=True,
               evidence_ref=ref, executed=(result != "BLOCKED"),
               reason_code="CONNECTION_TIMEOUT")
    row.update(extra)
    return row


def assess(rows, *, routes=ROUTES, requested=ROUTES, **kwargs):
    return assess_recovery_routes(
        original_goal_digest=GOAL, proposal_goal_digest=GOAL,
        authorized_routes=routes, requested_routes=requested,
        runtime_receipts=rows, **kwargs,
    )


def revise(rows, route, *, prior=(), revision=1, **kwargs):
    return admit_strategy_revision(
        original_goal_digest=GOAL, proposal_goal_digest=GOAL,
        authorized_routes=ROUTES, requested_route=route,
        runtime_receipts=rows, previous_revision=revision,
        proposed_revision=revision + 1,
        prior_revision_routes=prior, **kwargs,
    )


class EvidenceGatedReplanTests(unittest.TestCase):
    def test_one_timeout_cannot_claim_site_unreachable(self):
        d = assess({"A1": receipt("url.direct")})
        self.assertEqual(d["decision"], "REPLAN")
        self.assertEqual(d["remaining_routes"], ROUTES[1:])
        self.assertFalse(d["goal_satisfied"])

    def test_all_authorized_methods_failed_have_traceable_terminal_evidence(self):
        rows = {
            "A1": receipt("url.direct", ref="EV-DNS", reason_code="DNS_FAILURE"),
            "A2": receipt("url.https_alternate", ref="EV-TLS",
                          reason_code="TLS_HANDSHAKE_FAILED"),
            "A3": receipt("url.proxy_allowed", ref="EV-403",
                          reason_code="HTTP_ACCESS_DENIED"),
        }
        d = assess(rows)
        self.assertEqual(d["decision"], "EXHAUSTED")
        self.assertEqual(d["reason"], "AUTHORIZED_ROUTES_EXHAUSTED")
        self.assertEqual(d["evidence_refs"], ["EV-DNS", "EV-TLS", "EV-403"])
        self.assertFalse(d["goal_satisfied"])
        self.assertEqual([x["action_ids"][0] for x in d["attempts"]],
                         ["A1", "A2", "A3"])

    def test_bounded_authorization_denial_does_not_claim_actual_network_attempt(self):
        rows = {
            "A1": receipt("url.direct", result="BLOCKED",
                          executed=False, reason_code="SCOPE_DENIED"),
            "A2": receipt("url.https_alternate", result="BLOCKED",
                          executed=False, reason_code="SCOPE_DENIED"),
            "A3": receipt("url.proxy_allowed", result="BLOCKED",
                          executed=False, reason_code="SCOPE_DENIED"),
        }
        d = assess(rows)
        self.assertEqual(d["decision"], "EXHAUSTED")
        self.assertTrue(all(not x["executed"] for x in d["attempts"]))

    def test_unknown_outcome_must_pause_not_claim_exhaustion(self):
        rows = {
            "A1": receipt("url.direct"),
            "A2": receipt("url.https_alternate"),
            "A3": receipt("url.proxy_allowed", result="UNKNOWN"),
        }
        self.assertEqual(assess(rows)["decision"], "PAUSE")

    def test_unverified_claim_must_pause_even_if_all_fail(self):
        rows = {
            "A1": receipt("url.direct"),
            "A2": receipt("url.https_alternate"),
            "A3": receipt("url.proxy_allowed", verified=False),
        }
        self.assertEqual(assess(rows)["decision"], "PAUSE")

    def test_pending_effects_must_pause_before_any_new_replan(self):
        d = assess({}, pending_effects=True)
        self.assertEqual(d["decision"], "PAUSE")
        self.assertEqual(d["reason"], "PENDING_EFFECTS")

    def test_replan_cannot_change_original_goal(self):
        with self.assertRaisesRegex(ReplanSafetyError, "replan_original_goal_changed"):
            assess_recovery_routes(
                original_goal_digest=GOAL, proposal_goal_digest="b" * 64,
                authorized_routes=ROUTES, requested_routes=ROUTES,
                runtime_receipts={},
            )

    def test_replan_cannot_add_unauthorized_proxy_or_scope(self):
        with self.assertRaisesRegex(ReplanSafetyError, "replan_route_scope_expansion"):
            assess({}, requested=ROUTES + ["url.unapproved_browser"])
        with self.assertRaisesRegex(ReplanSafetyError, "replan_route_scope_expansion"):
            revise({}, "url.unapproved_browser")

    def test_new_action_id_does_not_make_same_strategy_new(self):
        one = {"ACTION-1": receipt("url.direct", ref="EV-1")}
        self.assertEqual(revise(one, "url.direct")["reason"],
                         "REPEATED_STRATEGY_NO_NEW_EVIDENCE")
        two = {**one, "ACTION-2": receipt("url.direct", ref="EV-1")}
        self.assertEqual(revise(two, "url.direct")["decision"], "PAUSE")
        self.assertEqual(revise(one, "url.https_alternate")["decision"], "REPLAN")

    def test_new_progress_revision_cannot_reset_prior_route_or_hard_budget(self):
        self.assertEqual(revise({}, "url.direct", prior=["url.direct"])["decision"],
                         "PAUSE")
        exhausted = revise({}, "url.direct", revision=12)
        self.assertEqual(exhausted["reason"], "REPLAN_BUDGET_EXHAUSTED")
        with self.assertRaisesRegex(ReplanSafetyError, "replan_revision_sequence_invalid"):
            admit_strategy_revision(
                original_goal_digest=GOAL, proposal_goal_digest=GOAL,
                authorized_routes=ROUTES, requested_route="url.direct",
                runtime_receipts={}, previous_revision=2, proposed_revision=2,
            )

    def test_successful_connection_is_not_goal_success(self):
        d = assess({"A1": receipt("url.direct", result="REACHABLE")})
        self.assertEqual(d["decision"], "CONTINUE")
        self.assertFalse(d["goal_satisfied"])

    def test_receipt_claim_from_unregistered_route_is_not_authority(self):
        with self.assertRaisesRegex(ReplanSafetyError, "replan_receipt_route_untrusted"):
            assess({"FAKE": receipt("url.new_private_host")})

    def test_conflicting_probes_cannot_be_silently_overwritten(self):
        rows = {
            "A1": receipt("url.direct", result="FAILED", ref="EV-1"),
            "A2": receipt("url.direct", result="REACHABLE", ref="EV-2"),
        }
        d = assess(rows)
        self.assertEqual(d["decision"], "PAUSE")
        self.assertEqual(d["reason"], "UNVERIFIED_OR_CONFLICTING_ROUTE_RESULT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
