"""Offline regression for temporary WebAgent initialization-only composer reset."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "source"))
from agent_core import composer_operation as co
from WebAgent.browser_client import WebAgentBrowserClient


def snapshot(*, empty=True, generation=False, user_count=2):
    return dict(url="https://chatgpt.com/c/probe", user_count=user_count,
                assistant_count=2, user_anchors=[["u1", "d1"]],
                assistant_anchors=[["a1", "e1"]],
                user_native_ids=["u1"], assistant_native_ids=["a1"],
                generation_active=generation, composer_empty=empty)


def verify_init_page_reset():
    editor = NS(is_visible=lambda: True, is_enabled=lambda: True, is_editable=lambda: True)
    fake = NS(_composer_locator=lambda: editor,
              _read_composer_text_candidates=lambda: ["manual draft"])
    stable = snapshot(empty=True)
    dirty = snapshot(empty=False)
    with patch.object(co, "_page_snapshot", side_effect=[stable, stable, stable]), patch.object(
            co, "_atomic_clear_owned") as clear:
        out = co._reset_initialized_page(fake, sleep=lambda _: None)
        assert out == {"status": "INIT_RESET_CONFIRMED", "cleared": False}, out
        clear.assert_not_called()
    with patch.object(co, "_page_snapshot", side_effect=[dirty, dirty, stable]), patch.object(
            co, "_atomic_clear_owned", return_value=True) as clear:
        out = co._reset_initialized_page(fake, sleep=lambda _: None)
        assert out == {"status": "INIT_RESET_CONFIRMED", "cleared": True}, out
        clear.assert_called_once_with(fake, "manual draft")
    with patch.object(co, "_page_snapshot", side_effect=[dirty, snapshot(user_count=3)]), patch.object(
            co, "_atomic_clear_owned") as clear:
        assert co._reset_initialized_page(fake, sleep=lambda _: None)["status"] == "INIT_RESET_PAGE_NOT_STABLE"
        clear.assert_not_called()
    with patch.object(co, "_page_snapshot", side_effect=[snapshot(generation=True)] * 2), patch.object(
            co, "_atomic_clear_owned") as clear:
        assert co._reset_initialized_page(fake, sleep=lambda _: None)["status"] == "INIT_RESET_PAGE_NOT_STABLE"
        clear.assert_not_called()
    readonly = NS(_composer_locator=lambda: NS(
        is_visible=lambda: True, is_enabled=lambda: True, is_editable=lambda: False),
        _read_composer_text_candidates=lambda: ["manual"])
    with patch.object(co, "_page_snapshot", side_effect=[dirty, dirty]), patch.object(
            co, "_atomic_clear_owned") as clear:
        assert co._reset_initialized_page(readonly, sleep=lambda _: None)["status"] == "INIT_RESET_PAGE_NOT_STABLE"
        clear.assert_not_called()
    with patch.object(co, "_page_snapshot", side_effect=[dirty, dirty]), patch.object(
            co, "_atomic_clear_owned", return_value=False):
        assert co._reset_initialized_page(fake, sleep=lambda _: None)["status"] == "INIT_RESET_CLEAR_FAILED"
    for final, reason in (
        (dirty, "COMPOSER_NOT_EMPTY"),
        (snapshot(generation=True), "GENERATION_ACTIVE"),
        (snapshot(user_count=3), "TURN_CHANGED"),
        (snapshot(empty=False, generation=True, user_count=3),
         "COMPOSER_NOT_EMPTY,GENERATION_ACTIVE,TURN_CHANGED"),
    ):
        with patch.object(co, "_page_snapshot", side_effect=[dirty, dirty, final]), patch.object(
                co, "_atomic_clear_owned", return_value=True):
            out = co._reset_initialized_page(fake, sleep=lambda _: None)
        assert out["status"] == "INIT_RESET_UNCONFIRMED", out
        assert out["cleared"] is True and out["reason_code"] == reason, out


def verify_journal_retirement():
    endpoint, url = "http://127.0.0.1:1272", "https://chatgpt.com/c/probe"
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / (co.page_key(endpoint, url) + ".json")
        original = dict(operation_id="stale1", state="QUARANTINED",
                        url=url, reason="SCOPE_CHANGED_OR_BUSY", baseline={"url": "old"})
        co.write_json_atomic(path, original)
        with patch.object(co, "run_helper", return_value={
                "status": "INIT_RESET_CONFIRMED", "cleared": False}) as helper:
            result = co.reset_initialization_composer(
                endpoint=endpoint, url=url, journal_root=root)
        assert result["status"] == "INIT_RESET_CONFIRMED", result
        assert result["prior_state"] == "QUARANTINED", result
        assert co.read_json_retry(path)["state"] == "RECOVERED"
        assert (path.with_name(path.stem + ".history") / "stale1.json").is_file()
        assert helper.call_args.args[0]["mode"] == "initialization_reset"
        co.write_json_atomic(path, original)
        with patch.object(co, "run_helper", return_value={
                "status": "INIT_RESET_PAGE_NOT_STABLE", "cleared": False}):
            try:
                co.reset_initialization_composer(endpoint=endpoint, url=url, journal_root=root)
            except co.ComposerOperationError as exc:
                assert "INIT_RESET_PAGE_NOT_STABLE" in str(exc)
            else:
                raise AssertionError("unstable page allowed initialization")
        assert co.read_json_retry(path)["state"] == "QUARANTINED"
    with tempfile.TemporaryDirectory() as root:
        with patch.object(co, "run_helper", return_value={
                "status": "INIT_RESET_CONFIRMED", "cleared": True}):
            co.reset_initialization_composer(endpoint=endpoint, url=url, journal_root=root)
        path = Path(root) / (co.page_key(endpoint, url) + ".json")
        assert co.read_json_retry(path)["state"] == "RECOVERED"


def verify_browser_client_stage_scope():
    seen = []
    lease = NS(release=lambda: None)
    gov = NS(acquire=lambda **kw: lease, record_success=lambda: None)
    scraper = NS(_page=NS(url="https://chatgpt.com/c/probe"),
                 _rate_submit_lease=None, _request_state="READY_IDLE",
                 _last_web_stage="idle")
    scraper.ask = lambda prompt, **kwargs: seen.append(kwargs) or "ack"
    client = WebAgentBrowserClient(scraper, governor=gov, event_sink=lambda *a, **kw: None)
    for stage in ("協議初始化", "協議初始化格式修復",
                  "協議初始化精簡身分確認", "協議附著確認"):
        assert client.ask("hello", stage=stage) == "ack"
        assert seen[-1]["initialization_composer_reset"] is True, stage
    assert client.ask("hello", stage="執行任務") == "ack"
    assert seen[-1]["initialization_composer_reset"] is False


def run():
    verify_init_page_reset()
    verify_journal_retirement()
    verify_browser_client_stage_scope()
    print("COMPOSER_INIT_RESET_OK: empty/dirty/unstable/busy/read-only/race/journal/stages")


if __name__ == "__main__":
    run()
