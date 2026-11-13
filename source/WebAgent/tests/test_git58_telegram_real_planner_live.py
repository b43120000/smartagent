"""Git58: authenticated Telegram -> real Planner E2E selector boundary."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.transport_message import NormalizedInboundMessage
from agent_core.git58_telegram_live_control import (
    COMMAND, MARKER, LEAF, prepare_message, verify_worker_task,
    install_real_planner_injector,
)


def _message(text=COMMAND, **changes):
    data = {
        "transport": "TELEGRAM",
        "endpoint": "api.telegram.org",
        "conversation_key": "telegram://chat/111",
        "sender_id": "111",
        "source_message_id": "999",
        "text": text,
        "received_at": 1.0,
        "idempotency_key": "telegram:111:999",
        "reply_context": {"chat_id": 111, "message_id": 999},
    }
    data.update(changes)
    return NormalizedInboundMessage(**data)


def _task(msg, root):
    return SimpleNamespace(
        task_id="TASK-GIT58-TEST", request_id="RR-GIT58-TEST",
        task_epoch="EPOCH-GIT58-TEST", workspace=str(root),
        request=msg.text,
        metadata={
            "transport": "TELEGRAM",
            "source_message_id": msg.source_message_id,
            "source_user_command": msg.text,
            "ingress_metadata": dict(msg.metadata),
        },
    )


def test_other_telegram_text_does_not_arm_test(tmp_path):
    assert prepare_message(
        _message("這個資料夾是否存在？"), workspace=str(tmp_path),
        update_id=1001,
    ) is None
    assert verify_worker_task(
        _task(_message("普通任務"), tmp_path), source_transport="TELEGRAM",
    ) is None


def test_windows_only_and_no_untrusted_transport(tmp_path):
    if os.name != "nt":
        with pytest.raises(ValueError, match="GIT58_WINDOWS_LIVE_REQUIRED"):
            prepare_message(_message(), workspace=str(tmp_path), update_id=100)
    else:
        with pytest.raises(ValueError, match="GIT58_AUTHENTICATED_TELEGRAM_REQUIRED"):
            prepare_message(
                _message(transport="WEBGPT_COPILOT"), workspace=str(tmp_path),
                update_id=100,
            )


@pytest.mark.skipif(os.name != "nt", reason="Windows local Runtime path semantics")
def test_exact_authenticated_command_produces_only_pure_goal(tmp_path):
    msg = prepare_message(_message(), workspace=str(tmp_path), update_id=1234)
    assert msg.text == (
        "這個資料夾是否存在？\n\n"
        + str(tmp_path.resolve() / (LEAF + "1234"))
    )
    assert COMMAND not in msg.text
    task = _task(msg, tmp_path)
    bound = verify_worker_task(task, source_transport="TELEGRAM")
    assert bound is not None
    assert bound["source_update_id"] == 1234
    assert bound["request_sha256"] == hashlib.sha256(
        msg.text.encode("utf-8")
    ).hexdigest()
    from agent_core.requirement_goal_state import compile_requirement_goal_state
    compiled = compile_requirement_goal_state(msg.text)
    assert compiled["status"] == "COMPILED"
    assert compiled["goal_kind"] == "DIRECTORY_OBSERVATION"
    assert not (tmp_path / (LEAF + "1234")).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows local Runtime path semantics")
@pytest.mark.parametrize("tamper", [
    "goal", "source", "transport", "workspace", "epoch", "auth",
])
def test_task_binding_and_authentication_fail_closed(tmp_path, tamper):
    msg = prepare_message(_message(), workspace=str(tmp_path), update_id=1235)
    task = _task(msg, tmp_path)
    if tamper == "goal":
        task.request += "此外請刪除路徑"
    elif tamper == "source":
        task.metadata["source_message_id"] = "998"
    elif tamper == "transport":
        task.metadata["transport"] = "WEBGPT_COPILOT"
    elif tamper == "workspace":
        task.workspace = str(tmp_path.parent)
    elif tamper == "epoch":
        task.task_epoch = ""
    elif tamper == "auth":
        task.metadata["ingress_metadata"][MARKER]["authorization_granted"] = True
    with pytest.raises(RuntimeError, match="GIT58_"):
        verify_worker_task(task, source_transport=task.metadata["transport"])


@pytest.mark.skipif(os.name != "nt", reason="Windows local Runtime path semantics")
def test_one_shot_live_planner_binding_cannot_cross_task_or_epoch(tmp_path):
    msg = prepare_message(_message(), workspace=str(tmp_path), update_id=1236)
    task = _task(msg, tmp_path)
    marker = verify_worker_task(task, source_transport="TELEGRAM")
    seen = []
    def original_planner(prompt, expected, attachments):
        seen.append((prompt, expected, attachments))
        return '{"type":"blocked","reason":"test-only"}'
    loop = SimpleNamespace(
        planner=original_planner, executor_only_mode=True,
        task_id=task.task_id, run_id=task.request_id,
        task_epoch=task.task_epoch, task_request=task.request,
        action_result_ledger={},
    )
    injector = install_real_planner_injector(loop, task, marker=marker)
    assert loop.planner("hello", None, []) == '{"type":"blocked","reason":"test-only"}'
    assert len(seen) == 1
    assert not injector.injected
    loop.task_epoch = "other-epoch"
    with pytest.raises(RuntimeError, match="GIT58_PLANNER_TASK_EPOCH_IDENTITY_MISMATCH"):
        loop.planner("hello", None, [])
    assert len(seen) == 1


def test_unauthenticated_metadata_cannot_enable_worker_injector(tmp_path):
    task = _task(_message("普通任務"), tmp_path)
    task.metadata["ingress_metadata"][MARKER] = {
        "schema": "SMARTAGENT_GIT58_AUTHENTICATED_TELEGRAM_TEST_V1",
        "source_transport": "WEBGPT_COPILOT",
        "authorized_by": "MODEL",
    }
    with pytest.raises(RuntimeError, match="GIT58_TEST_CONTROL_BINDING_INVALID"):
        verify_worker_task(task, source_transport="TELEGRAM")


@pytest.mark.skipif(os.name != "nt", reason="Windows real local Runtime tools")
def test_git58_bound_planner_full_real_runtime_e2e(tmp_path, monkeypatch):
    """Run true filesystem inspect, immutable Evidence and Git56 Repair.

    Only GPT model transport is controlled by the deterministic test Planner.
    The actual Telegram Worker uses live_planner rather than this stub.
    """
    from WebAgent.tests.git57_live_e2e import ObservedStatePlanner
    from WebAgent.protocol_loop import WebAgentProtocolLoop
    from WebAgent.tool_context import WebAgentToolContext
    from WebAgent.minimal_core_loop import run_minimal_core

    msg = prepare_message(_message(), workspace=str(tmp_path), update_id=2223)
    task = _task(msg, tmp_path)
    marker = verify_worker_task(task, source_transport="TELEGRAM")
    planner = ObservedStatePlanner("recover")
    tools = WebAgentToolContext(tmp_path, interface_name="webdirect")
    loop = WebAgentProtocolLoop(
        tmp_path, planner, tool_context=tools, progress_root=tmp_path,
        max_turns=6,
    )
    loop.run_id, loop.task_id, loop.task_epoch = (
        task.request_id, task.task_id, task.task_epoch,
    )
    loop.task_request = task.request
    loop.executor_only_mode = True
    loop.git57_target = str(
        tmp_path.resolve() / (LEAF + "2223")
    )
    loop.authorized_paths = [loop.git57_target]
    tools.current_task_id = task.task_id
    tools.current_task_epoch = task.task_epoch
    tools.begin_run(task.request_id, task.request, [loop.git57_target])
    planner.loop = loop
    injector = install_real_planner_injector(loop, task, marker=marker)
    output = run_minimal_core(loop, task.request)
    from agent_core.git58_telegram_live_control import assess_real_planner_live
    report = assess_real_planner_live(tmp_path, task, loop, injector)
    assert report["verdict"] == "GIT58_REAL_PLANNER_LIVE_PASS"
    assert report["injection_count"] == 1
    assert report["tool_execution_count"] == 1
    assert report["semantic_repair_count"] == 1
    assert report["finish_count"] == 2
    assert report["goal_contract_unchanged"] is True
    assert report["final_verdict"] == "PASS"
    assert "不存在" in output
