#!/usr/bin/env python3
"""Offline regression coverage for image-only assistant start detection."""
from __future__ import annotations

import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.web_runtime import WebLLMScraper
from agent_core.web_ui.providers.chatgpt.adapter import ChatGPTUIAdapter
from agent_core.image_delivery import is_image_generation_request, plan_image_delivery
from agent_core.protocol_v9 import parse_v9_tool_transport
from agent_core.task_progress import validate_model_progress
from WebAgent.protocol_loop import WebAgentProtocolLoop


EMPTY_FP = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class FakeImage:
    def __init__(self, src: str, *, complete: bool, width: int, height: int):
        self.src = src
        self.complete = complete
        self.width = width
        self.height = height

    def evaluate(self, _script):
        return {
            "src": self.src,
            "complete": self.complete,
            "naturalWidth": self.width,
            "naturalHeight": self.height,
            "renderedWidth": self.width,
            "renderedHeight": self.height,
            "visible": True,
            "alt": "Generated image",
            "aria_label": "",
            "testid": "",
            "class_name": "",
        }


class FakeRoot:
    def __init__(self, images):
        self.images = list(images)

    def query_selector_all(self, selector):
        return list(self.images) if selector == "img" else []


def media_state_for(images) -> dict:
    adapter = ChatGPTUIAdapter.__new__(ChatGPTUIAdapter)
    return adapter.media_state(FakeRoot(images))


def make_scraper(current_state: dict):
    scraper = WebLLMScraper.__new__(WebLLMScraper)
    scraper._page = object()
    scraper._page_media_state = lambda: dict(current_state)
    scraper._generation_stall_sec = 0.01
    scraper._active_generation_emergency_sec = 0.01
    scraper._active_generation_warn_sec = 0.01
    scraper._run_control_hook = lambda: False
    scraper._check_cancel_requested = lambda _stage: None
    scraper._maybe_recover_disconnected_generation = lambda: False
    scraper._turn_elements = lambda _role: []
    scraper._response_elements = lambda: []
    scraper._is_generation_active = lambda: False
    scraper._disconnect_signature_visible = lambda: False
    scraper._logs = []
    scraper._log_stage = lambda stage, detail="": scraper._logs.append((stage, detail))
    return scraper


def run() -> dict:
    exact_request = (
        "生成一張柴犬衝浪，然後存在這個路徑下"
        "C:\\Users\\Example\\Desktop\\workspace\\images\n"
        "就叫5.jpg 然後也把圖上傳到telegram"
    )
    assert is_image_generation_request(exact_request)
    assert is_image_generation_request("把這張圖上面的文字拿掉")
    plan = plan_image_delivery(
        exact_request,
        workspace=r"C:\Users\Example\Desktop\workspace\images",
        interface_name="remote",
        source_tag="REMOTEAGENT_TELEGRAM",
        request_id="RR-TEST-IMAGE",
    )
    assert plan.get("delivery") == "telegram"
    assert plan.get("output_path") == r"C:\Users\Example\Desktop\workspace\images\5.jpg"
    assert plan.get("expected_filename") == "5.jpg"
    telegram_only = plan_image_delivery(
        "生成一張圖片並傳回 Telegram",
        workspace=r"C:\Users\Example\Desktop\workspace",
        interface_name="remote",
        source_tag="REMOTEAGENT_TELEGRAM",
        request_id="RR-TEST-ONLY",
    )
    assert telegram_only["output_path"].endswith(
        str(Path(".agents") / "generated_image_outbound" / "RR-TEST-ONLY" / "generated_image.png")
    )
    assert not is_image_generation_request("生成一張表格，列出檔案大小")

    before = media_state_for([])
    ready = media_state_for([
        FakeImage("https://example.test/new-image.png", complete=True, width=1024, height=1024)
    ])
    pending = media_state_for([
        FakeImage("https://example.test/new-image.png", complete=False, width=0, height=0)
    ])

    assert before["ready_image_fingerprint"] == EMPTY_FP
    assert ready["image_ready"] == 1
    assert ready["image_pending"] == 0
    assert ready["response_kind"] == "image"
    assert ready["ready_image_fingerprint"] != before["ready_image_fingerprint"]
    assert pending["image_ready"] == 0

    snapshot = {
        "assistant_count": 0,
        "response_count": 0,
        "last_assistant_fp": "",
        "page_media_before": before,
    }
    scraper = make_scraper(ready)
    fresh = scraper._fresh_ready_page_image_state(snapshot)
    assert fresh is not None
    assert fresh["ready_image_fingerprint"] == ready["ready_image_fingerprint"]
    assert fresh["response_kind"] == "image"
    assert scraper._wait_for_new_assistant_turn(snapshot) is None
    assert any(stage == "assistant_started_by_fresh_image" for stage, _ in scraper._logs)

    stale = make_scraper(before)
    assert stale._fresh_ready_page_image_state(snapshot) is None
    incomplete = make_scraper(pending)
    assert incomplete._fresh_ready_page_image_state(snapshot) is None
    assert WebLLMScraper._fresh_ready_page_image_state(
        make_scraper(ready),
        {"assistant_count": 0},
    ) is None

    loop = WebAgentProtocolLoop(APP_ROOT, lambda *_args: "")
    loop.run_id = "RR-TEST-IMAGE"
    loop.image_delivery_plan = {
        "delivery": "local",
        "output_path": r"C:\Users\Example\Desktop\workspace\images\5.jpg",
        "expected_filename": "5.jpg",
    }
    expected_delivery = loop._new_commit()
    assert expected_delivery["artifact_save_expected"] is True
    assert expected_delivery["artifact_kind"] == "image"
    assert expected_delivery["runtime_image_progress"]["current_step"] == 1
    bridged = WebLLMScraper._runtime_fresh_image_delivery_response(
        expected_delivery,
        fresh_artifact_seen=True,
    )
    calls, errors = parse_v9_tool_transport(bridged)
    assert not errors and len(calls) == 3
    assert calls[0]["tool"] == "report_progress"
    validate_model_progress(calls[0])
    assert calls[1]["tool"] == "download_artifact"
    assert calls[1]["output_path"] == expected_delivery["artifact_output_path"]
    assert calls[1]["expected_filename"] == "5.jpg"
    assert calls[1]["timeout"] == 12
    assert calls[2] == {"tool": "turn_commit", "action_count": 2}
    loop.task_id = "TASK-TEST-IMAGE"
    loop.task_epoch = "EPOCH-TEST-IMAGE"
    loop.intent_digest = "intent-test-image"
    loop.turn_id = expected_delivery["turn_id"]
    accepted, diagnostics = loop._accept_v8_ack(calls, expected_delivery)
    assert not diagnostics and [item["tool"] for item in accepted] == [
        "report_progress", "download_artifact"
    ]
    assert not WebLLMScraper._runtime_fresh_image_delivery_response(
        expected_delivery,
        fresh_artifact_seen=False,
    )
    assert not WebLLMScraper._runtime_fresh_image_delivery_response(
        {**expected_delivery, "artifact_save_expected": False},
        fresh_artifact_seen=True,
    )
    assert not WebLLMScraper._runtime_fresh_image_delivery_response(
        {**expected_delivery, "artifact_kind": "file"},
        fresh_artifact_seen=True,
    )
    assert not WebLLMScraper._runtime_fresh_image_delivery_response(
        {**expected_delivery, "artifact_output_path": ""},
        fresh_artifact_seen=True,
    )

    return {
        "colloquial_image_intent_detected": True,
        "image_edit_intent_detected": True,
        "telegram_and_requested_local_path_preserved": True,
        "telegram_fallback_is_explicit_file": True,
        "table_classifier_not_misdetected": True,
        "new_completed_image_crosses_start_gate": True,
        "stale_image_rejected": True,
        "pending_image_rejected": True,
        "missing_baseline_fails_closed": True,
        "fresh_image_runtime_delivery_bridge": True,
        "runtime_delivery_bridge_is_image_save_scoped": True,
    }


if __name__ == "__main__":
    print(run())
