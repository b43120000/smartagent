"""Git57 tests for guarded Planner Finish injection and immutable Evidence."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_core.git57_live_finish_injector import TestOnlyFirstFinishInjector
from agent_core.tool_result_contract import encode_result


def _fixture(*, status="SUCCEEDED", exists=False, persisted=True):
    ref = "MIN-EV-" + "a" * 24
    receipt = encode_result(
        status, "DIRECTORY_INSPECTED", tool="inspect_directory", executed=True,
        data={"observed_exists": exists, "observation_status": "CONFIRMED"},
    )
    loop = SimpleNamespace(
        turn_id=2, task_id="TASK-GIT57-UNIT", run_id="RR-GIT57-UNIT",
        task_epoch="EPOCH-GIT57-UNIT", progress_root="/unused", workspace="/unused",
        action_result_ledger={"action-1": {
            "tool": "inspect_directory", "result": receipt,
            "evidence_ref": ref,
            "evidence_store": {
                "evidence_ref": ref,
                "store_status": (
                    "PERSISTED_AND_READBACK_VERIFIED" if persisted else
                    "PERSIST_FAILED"
                ),
            },
        }},
    )
    response = json.dumps({
        "type": "finish", "summary": "已觀察資料夾存在性",
        "expected_runtime": [{
            "evidence_ref": ref, "fields": {
                "status": status, "data.observed_exists": exists,
            },
        }],
    })
    return loop, response


def test_one_shot_typed_flip_uses_real_claim_and_logs_hashes(monkeypatch):
    loop, response = _fixture()
    events = []
    monkeypatch.setattr(
        "agent_core.git57_live_finish_injector.append_executor_event",
        lambda root, actual_loop, event: events.append(dict(event)),
    )
    inj = TestOnlyFirstFinishInjector(lambda *_: response, loop)
    changed = json.loads(inj("prompt", None, []))
    assert changed["expected_runtime"][0]["fields"]["data.observed_exists"] is True
    assert inj.injected is True
    assert len(events) == 1
    assert events[0]["response_sha256_before"] != events[0]["response_sha256_after"]
    assert events[0]["authorization_granted"] is False
    assert events[0]["executed"] is False
    assert json.loads(inj("prompt", None, []))["expected_runtime"][0]["fields"]["data.observed_exists"] is False
    assert len(events) == 1
    assert loop.action_result_ledger["action-1"]["result"].startswith("[TOOL_RESULT] ")


@pytest.mark.parametrize("status,persisted", [
    ("FAILED", True), ("SUCCEEDED", False),
])
def test_no_injection_without_confirmed_persisted_execution(
    monkeypatch, status, persisted,
):
    loop, response = _fixture(status=status, persisted=persisted)
    monkeypatch.setattr(
        "agent_core.git57_live_finish_injector.append_executor_event",
        lambda *args, **kwargs: pytest.fail("injection event must not be written"),
    )
    inj = TestOnlyFirstFinishInjector(lambda *_: response, loop)
    assert inj("", None, []) == response
    assert inj.injected is False


def test_no_injection_when_model_claim_is_already_false(monkeypatch):
    loop, response = _fixture()
    raw = json.loads(response)
    raw["expected_runtime"][0]["fields"]["data.observed_exists"] = True
    wrong = json.dumps(raw)
    monkeypatch.setattr(
        "agent_core.git57_live_finish_injector.append_executor_event",
        lambda *args, **kwargs: pytest.fail("must not modify an already invalid Finish"),
    )
    inj = TestOnlyFirstFinishInjector(lambda *_: wrong, loop)
    assert inj("", None, []) == wrong
    assert inj.injected is False


def test_invalid_fields_never_injected():
    loop, response = _fixture()
    with pytest.raises(ValueError, match="ALLOWLISTED"):
        TestOnlyFirstFinishInjector(lambda *_: response, loop, field="data.exit_code")


def test_never_inject_model_toolcall_or_arbitrary_text():
    loop, _ = _fixture()
    tool = json.dumps({
        "type": "tool_call", "name": "inspect_directory",
        "arguments": {"paths": ["X:\\example"], "recursive": False},
    })
    inj = TestOnlyFirstFinishInjector(lambda *_: tool, loop)
    assert inj("", None, []) == tool
    assert inj.injected is False
