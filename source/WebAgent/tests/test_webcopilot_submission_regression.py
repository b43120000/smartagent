"""Isolated submission regressions. No CDP attach or production conversation."""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_core import composer_operation as co


def snapshot(count=3, *, matched=False):
    return dict(
        url="https://chatgpt.com/c/offline",
        user_count=count, assistant_count=count,
        user_anchors=[["u1", "ud1"], ["u2", "ud2"]][-min(count, 2):],
        assistant_anchors=[["a1", "ad1"], ["a2", "ad2"]][-min(count, 2):],
        user_native_ids=["", ""][-min(count, 2):],
        assistant_native_ids=["a1", "a2"][-min(count, 2):],
        generation_active=False, composer_empty=not matched,
        composer_matches=matched, last_turn_status="LAST_ASSISTANT",
    )


def test_historical_three_to_one_projection_still_fails_closed():
    before, after = snapshot(), snapshot(1, matched=True)
    assert co.classify_observation(
        before, after, turn_count_tolerance=1,
    ) == "TURN_CHANGED_OR_SUBMIT_UNCERTAIN"
    assert co._snapshot_difference(before, after)["same_turns"] is False


@pytest.mark.parametrize("change", [
    {"url": "https://chatgpt.com/c/other"},
    {"generation_active": True},
    {"assistant_native_ids": ["a1", "new"]},
    {"user_count": 7},
])
def test_changed_scope_cannot_submit(change):
    assert co.classify_observation(
        snapshot(), {**snapshot(matched=True), **change},
        turn_count_tolerance=1,
    ) == "TURN_CHANGED_OR_SUBMIT_UNCERTAIN"


def test_focus_failure_is_prewrite_only():
    scraper = SimpleNamespace(_composer_locator=lambda: None)
    with patch.object(co, "_wait_for_prewrite_stability") as wait:
        result = co._prepare_write_baseline(scraper, "never typed")
    assert result["status"] == "PREWRITE_UNSTABLE"
    assert result["mutation_possible"] is False
    assert not wait.called


def test_offline_browser_focus_rerender_and_long_draft(tmp_path):
    """Real isolated browser DOM, synthetic history virtualization on focus."""
    from playwright.sync_api import sync_playwright

    executable = os.environ.get("SMARTAGENT_OFFLINE_BROWSER")
    if not executable and os.name == "nt":
        for candidate in (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ):
            if Path(candidate).is_file():
                executable = candidate
                break
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=executable)
        try:
            page = browser.new_page()
            page.set_content("""
                <textarea id="composer"></textarea>
                <script>
                window.visibleTurns = 3;
                document.querySelector('textarea').addEventListener('focus', () => {
                    window.visibleTurns = 1;
                });
                </script>
            """)
            editor = page.locator("textarea")
            scraper = SimpleNamespace(
                _page=page, _composer_locator=lambda: editor,
                _focus_composer_dom=lambda: (editor.focus(), editor)[1],
            )
            prompt = "獨立離線測試\n" + "x" * 12000
            def observe(*_):
                return snapshot(page.evaluate("visibleTurns"),
                                matched=editor.input_value() == prompt)
            with patch.object(co, "_page_snapshot", side_effect=observe), patch.object(
                co, "_scroll_conversation_to_bottom", return_value={"status": "NO_SCROLL_RANGE"},
            ):
                old_baseline = observe()
                # Reproduce the old order: snapshot, then writer focus.
                editor.focus()
                page.keyboard.insert_text(prompt)
                assert co.classify_observation(old_baseline, observe(),
                    turn_count_tolerance=1) == "TURN_CHANGED_OR_SUBMIT_UNCERTAIN"
                editor.fill("")
                editor.blur()
                page.evaluate("visibleTurns = 3")
                ready = co._prepare_write_baseline(scraper, prompt)
                assert ready["status"] == "READY"
                assert ready["baseline"]["user_count"] == 1
                page.keyboard.insert_text(prompt)
                assert editor.input_value() == prompt
                assert co.classify_observation(ready["baseline"], observe(),
                    turn_count_tolerance=1) == "VERIFIED_SAFE_TO_SUBMIT"
                page.evaluate("visibleTurns = 5")
                assert co.classify_observation(ready["baseline"], observe(),
                    turn_count_tolerance=1) == "TURN_CHANGED_OR_SUBMIT_UNCERTAIN"
        finally:
            browser.close()


@pytest.mark.parametrize("timed_out", [False, True])
def test_parent_quarantines_uncertain_draft_without_rewrite(tmp_path, timed_out):
    import subprocess

    calls = []
    def helper(payload, *_):
        calls.append(payload["mode"])
        if payload["mode"] == "write":
            record = co.read_json_retry(Path(payload["journal"]))
            record["baseline"] = snapshot()
            co.write_json_atomic(Path(payload["journal"]), record)
            if timed_out:
                raise subprocess.TimeoutExpired("isolated", 1)
        if payload["mode"] == "recover":
            return {"status": "SUBMIT_OUTCOME_UNKNOWN", "observation": snapshot(1)}
        return {"status": "TURN_CHANGED_OR_SUBMIT_UNCERTAIN",
                "observation": snapshot(1, matched=True)}
    args = dict(endpoint="http://localhost:1234", url=snapshot()["url"],
                prompt="WUS-" + "A" * 32 + "\n" + "x" * 12000,
                journal_root=tmp_path)
    with patch.object(co, "run_helper", side_effect=helper):
        with pytest.raises(co.ComposerOperationError):
            co.prepare_composer(**args)
        with pytest.raises(co.ComposerOperationError):
            co.prepare_composer(**args)
    assert calls == ["write", "observe", "recover"]
    record = co.read_json_retry(next(tmp_path.glob("*.json")))
    assert record["state"] == "QUARANTINED"
    assert record["reason"] == ("WRITER_TIMEOUT" if timed_out
                                else "TURN_CHANGED_OR_SUBMIT_UNCERTAIN")


def test_recovery_duplicates_require_reconciliation():
    marker = "WUS-" + "A" * 32
    scraper = SimpleNamespace(_web_ui_adapter=lambda: SimpleNamespace(
            observation_turns=lambda _: [SimpleNamespace(raw_text=(
                "[WEBUI_SUBMIT_ID]\n" + marker + "\n[/WEBUI_SUBMIT_ID]"
            ))] * 2))
    with patch.object(co, "_page_snapshot", return_value=snapshot()), patch.object(
        co.time, "sleep",
    ), patch.object(co, "_atomic_clear_owned") as clear:
        result = co._recover_page(scraper, {"submit_id": marker})
    assert result["status"] == "DELIVERY_AMBIGUOUS"
    assert result["sent_count"] == 2
    assert not clear.called


@pytest.mark.parametrize("submit_attempted", [False, True])
def test_submit_timeout_never_replays_without_delivery_proof(tmp_path, submit_attempted):
    calls = []
    def helper(payload, *_):
        calls.append(payload["mode"])
        if payload["mode"] == "write":
            record = co.read_json_retry(Path(payload["journal"]))
            record["baseline"] = snapshot()
            co.write_json_atomic(Path(payload["journal"]), record)
        if payload["mode"] == "recover":
            return {"status": "SUBMIT_OUTCOME_UNKNOWN", "observation": snapshot()}
        return {"status": "VERIFIED_SAFE_TO_SUBMIT",
                "observation": snapshot(matched=True)}
    args = dict(endpoint="http://localhost:1234", url=snapshot()["url"],
                prompt="WUS-" + "A" * 32 + "\nrequest", journal_root=tmp_path)
    with patch.object(co, "run_helper", side_effect=helper):
        result = co.prepare_composer(**args)
        co.verify_prepared_composer(**args, operation_id=result["operation_id"])
        if submit_attempted:
            co.mark_composer_submitted(endpoint=args["endpoint"], url=args["url"],
                journal_root=tmp_path, operation_id=result["operation_id"])
        # A request after a lost submit receipt may only reconcile.
        with pytest.raises(co.ComposerOperationError):
            co.prepare_composer(**args)
    assert calls == ["write", "observe", "observe", "recover"]


def test_unique_delivery_marker_recovers_without_mutation():
    marker = "WUS-" + "B" * 32
    scraper = SimpleNamespace(_web_ui_adapter=lambda: SimpleNamespace(
        observation_turns=lambda _: [SimpleNamespace(raw_text=(
            "[WEBUI_SUBMIT_ID]\n" + marker + "\n[/WEBUI_SUBMIT_ID]"
        ))]))
    with patch.object(co, "_page_snapshot", return_value=snapshot()), patch.object(
        co.time, "sleep",
    ), patch.object(co, "_atomic_clear_owned") as clear:
        result = co._recover_page(scraper, {
            "state": "SUBMIT_ATTEMPTED", "submit_id": marker,
        })
    assert result["status"] == "ALREADY_DELIVERED"
    assert result["sent_count"] == 1
    assert not clear.called
