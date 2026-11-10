"""Git47 WebCopilot Tri-One readable result & DOM-only presentation contracts."""
from __future__ import annotations

import threading
from contextlib import nullcontext
from types import SimpleNamespace

from agent_core.tri_one_task_result import make_task_result_from_remote_event
from agent_core.webcopilot_result_view import (
    build_webcopilot_result_view, validated_tri_one_result,
)
from agent_core.webcopilot_result_panel import show_webcopilot_result
from WebAgent.tests.test_tri_one_egress_v10 import terminal_task


_URL = "https://chatgpt.com/c/1234abcd-1234-1234-1234-123456789abc"


class EventStore:
    def __init__(self, result, *, url=_URL, transport="WEBGPT_COPILOT"):
        self._lock = threading.RLock()
        event = SimpleNamespace(
            task_id=result["task_id"],
            request_id=result["request_id"],
            event_type="TASK_COMPLETED",
            reply_route={"transport": transport, "conversation_url": url},
            payload={"task_result_v1": result},
            created_at=9,
        )
        self.events = {"EVT-1": event}

    def process_lock(self):
        return nullcontext()

    def load(self):
        return None


def test_verified_terminal_readable_without_changing_task_result():
    task, event, _ = terminal_task("WEBGPT_COPILOT", verified=True)
    result = make_task_result_from_remote_event(
        task, event, {"summary": "已建立並確認資料夾"}
    )
    original = dict(result)
    from agent_core.conversation_identity import same_conversation
    from agent_core.tri_one_task_result import validate_task_result
    assert same_conversation(_URL, _URL), "conversation URL not normalized"
    assert validate_task_result(result)["source_interface"] == "WEBGPT_COPILOT"
    actual = validated_tri_one_result(
        dict(vars(task)), EventStore(result), _URL,
    )
    assert actual is not None, "canonical result not matched despite same event identity"
    view = build_webcopilot_result_view(dict(vars(task)), actual)
    assert view["verified"] is True
    assert view["title"] == "任務已完成"
    assert view["summary"] == "已建立並確認資料夾"
    assert view["evidence_refs"] == ["MIN-ACTION-1"]
    assert result == original


def test_missing_or_cross_conversation_canonical_cannot_claim_pass():
    task, event, _ = terminal_task("WEBGPT_COPILOT", verified=True)
    result = make_task_result_from_remote_event(
        task, event, {"summary": "完成"}
    )
    assert validated_tri_one_result(dict(vars(task)), EventStore(
        result, url="https://chatgpt.com/c/other"), _URL) is None
    assert validated_tri_one_result(dict(vars(task)), EventStore(
        result, transport="TELEGRAM"), _URL) is None
    view = build_webcopilot_result_view(dict(vars(task)), None)
    assert view["verified"] is False
    assert "尚未" in view["note"]


def test_paused_never_becomes_success():
    task, event, _ = terminal_task("WEBGPT_COPILOT", verified=False)
    result = make_task_result_from_remote_event(
        task, event, {"summary": "尚未完成"}
    )
    view = build_webcopilot_result_view(dict(vars(task)), result)
    assert view["verified"] is False
    assert view["title"] == "任務已暫停"
    assert view["outcome"] == "UNKNOWN"


def test_only_allowlisted_result_data_reaches_browser():
    class Page:
        def __init__(self):
            self.arguments = []
        def evaluate(self, script, arg):
            self.arguments.append((script, arg))
            return True
    page = Page()
    task = {
        "task_id": "TASK-1", "state": "FAILED", "error": "SECRET_PASSWORD=hi",
        "result_ledger": {"final": {
            "command": "PRIVATE-COMMAND",
            "environment": {"TOKEN": "NEVER_EXPOSE"},
            "response": "執行失敗",
        }},
    }
    view = build_webcopilot_result_view(task)
    assert "PRIVATE-COMMAND" not in repr(view)
    assert "SECRET_PASSWORD" not in repr(view)
    assert "NEVER_EXPOSE" not in repr(view)
    assert show_webcopilot_result(page, view) is True
    script, payload = page.arguments[0]
    assert "textContent" in script and "innerHTML" not in script
    assert payload["summary"] == "執行失敗"
    assert payload["verified"] is False


def test_no_fallback_to_forged_payload():
    task, event, _ = terminal_task("WEBGPT_COPILOT", verified=True)
    result = make_task_result_from_remote_event(
        task, event, {"summary": "完成"}
    )
    result["terminal_acceptance"]["status"] = "ACCEPTED"
    result["outcome"] = "FAIL"  # Contract invalid.
    checked = validated_tri_one_result(dict(vars(task)), EventStore(result), _URL)
    assert checked is None
