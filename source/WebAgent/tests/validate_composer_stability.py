"""Composer pre-write stability and recovery delta offline regression."""
from __future__ import annotations

import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from agent_core import composer_operation as co


def snap(digest="a", *, busy=False, empty=True, match=False):
    return dict(
        url="https://chatgpt.com/c/example", user_count=2, assistant_count=2,
        user_anchors=[["u0","u0d"],["u1","u1d"]],
        assistant_anchors=[["a0",digest],["a1",digest]],
        user_native_ids=["u0","u1"], assistant_native_ids=["a0","a1"],
        generation_active=busy, composer_empty=empty, composer_matches=match,
    )


class Clock:
    def __init__(self): self.value=0.0
    def now(self): return self.value
    def wait(self, duration): self.value+=duration


class Editor:
    def is_visible(self): return True
    def is_enabled(self): return True
    def is_editable(self): return True


class ComposerStabilityTest(unittest.TestCase):
    def probe(self, series, *, timeout=2.0, editor=None):
        clock=Clock()
        it=iter(series)
        seen=[]
        def observe(*_args):
            item=next(it,series[-1])
            seen.append(item)
            return item
        scraper=NS(_composer_locator=lambda:Editor() if editor is None else editor)
        with patch.object(co,"_page_snapshot",side_effect=observe):
            result=co._wait_for_prewrite_stability(
                scraper,"prompt",samples=4,interval_sec=.3,
                timeout_sec=timeout,clock=clock.now,sleep=clock.wait)
        return result,len(seen)

    def test_four_matching_observations(self):
        result,count=self.probe([snap()]*4)
        self.assertEqual(result["status"],"READY")
        self.assertEqual(count,4)

    def test_digest_change_resets_stability_window(self):
        result,count=self.probe([snap("old")]+[snap("new")]*4)
        self.assertEqual(result["status"],"READY")
        self.assertEqual(count,5)

    def test_changing_assistant_digests_never_admitted(self):
        result,_=self.probe([snap(str(x)) for x in range(12)],timeout=1.25)
        self.assertEqual(result["status"],"PREWRITE_UNSTABLE")
        self.assertNotIn("baseline",result)

    def test_generation_active_never_admitted(self):
        result,_=self.probe([snap(busy=True)],timeout=1.25)
        self.assertEqual(result["reason_code"],"GENERATION_ACTIVE")

    def test_foreign_draft_immediately_rejected(self):
        result,count=self.probe([snap(empty=False,match=False)])
        self.assertEqual(result["status"],"PREWRITE_FOREIGN_DRAFT")
        self.assertEqual(count,1)

    def test_non_interactive_editor_fails_closed(self):
        result,_=self.probe([snap()],timeout=1.0,
                            editor=NS(is_visible=lambda:False))
        self.assertEqual(result["status"],"PREWRITE_UNSTABLE")
        self.assertEqual(result["reason_code"],"COMPOSER_NOT_INTERACTIVE")

    def test_assistant_digest_only_diagnostic(self):
        diff=co._snapshot_difference(snap("old"),snap("new"))
        self.assertEqual(diff["assistant_digest_changed_positions"],[0,1])
        self.assertEqual(diff["assistant_structural_changed_positions"],[])
        self.assertTrue(diff["assistant_native_ids_equal"])
        self.assertFalse(diff["same_turns"])
        self.assertEqual(diff["user_digest_changed_positions"],[])
        self.assertNotIn("https://",str(diff))

    def test_historical_digest_mismatch_keeps_quarantine(self):
        before,after=snap("old"),snap("new")
        record=dict(baseline=before,state="QUARANTINED",
                    submit_id="",prompt_sha256="not-sent")
        scraper=NS(_web_ui_adapter=lambda:NS(observation_turns=lambda role:[]))
        with patch.object(co,"_page_snapshot",return_value=after),\
             patch.object(co.time,"sleep"):
            result=co._recover_page(scraper,record)
        self.assertEqual(result["status"],"SCOPE_CHANGED_OR_BUSY")
        detail=result["comparison"]
        self.assertTrue(detail["recent_observations"]["same_turns"])
        self.assertEqual(detail["historical_baseline"]["assistant_digest_changed_positions"],[0,1])
        self.assertFalse(detail["delivery_confirmed"])


if __name__=="__main__":
    unittest.main(verbosity=2)
