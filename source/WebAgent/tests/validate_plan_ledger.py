#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from agent_core.agent_gateway import AgentIngressGateway
from agent_core.plan_ledger import (
    PlanLedger,
    is_planning_request,
    parse_execute_plan_command,
    publish_completed_plan,
)
from agent_core.task_progress import TaskProgressLedger
from agent_core.task_state import RemoteTaskQueue, TaskStateStore
from agent_core.transport_message import NormalizedInboundMessage
from agent_core.transport_sessions import TransportSessionRouter
from RemoteAgent.telegram_delivery import TelegramDeliveryAdapter


class Task:
    task_id = "TASK-0123456789ABCDEF0123"
    request_id = "RR-TELEGRAM-0123456789ABCDEF"
    request = "先盤點現況並提供改動規劃，只規劃不改"
    workspace = ""
    conversation_url = "telegram://chat/1"
    session_id = "SESSION-TEST"


def message(text: str, message_id: str) -> NormalizedInboundMessage:
    return NormalizedInboundMessage(
        transport="telegram",
        endpoint="bot",
        conversation_key="telegram://chat/1",
        sender_id="1",
        source_message_id=message_id,
        text=text,
        received_at=time.time(),
        idempotency_key=f"telegram:1:{message_id}",
        reply_context={"chat_id": 1},
    )


def main() -> None:
    assert is_planning_request("先評估不修改")
    assert is_planning_request("盤點與改動規劃")
    assert not is_planning_request("開始修改")
    assert not is_planning_request("執行 PLAN-0123456789ABCDEF0123")
    assert not is_planning_request(
        "[SMARTAGENT_EXECUTE_SAVED_PLAN]\n執行既有規劃\n[/SMARTAGENT_EXECUTE_SAVED_PLAN]"
    )
    assert parse_execute_plan_command("請執行 PLAN-0123456789abcdef0123") == "PLAN-0123456789ABCDEF0123"

    with tempfile.TemporaryDirectory(prefix="plan-ledger-") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        workspace.mkdir()
        task_store_path = root / "runtime" / "remote_tasks.json"
        ledger = PlanLedger.from_task_store(task_store_path)
        task = Task()
        task.workspace = str(workspace)
        progress = TaskProgressLedger(
            task_id=task.task_id,
            request_id=task.request_id,
            goal=task.request,
            base_evaluation="已完成盤點",
            total_steps=2,
            current_step=2,
            steps=[
                {"step": 1, "desc": "盤點", "status": "COMPLETED"},
                {"step": 2, "desc": "提出規劃", "status": "COMPLETED"},
            ],
            completion_contract={
                "success": ["規劃已完成"],
                "failure": ["規劃失敗"],
                "in_progress": ["仍在規劃"],
                "interrupted": ["無法取得必要資訊"],
            },
            decision="COMPLETE",
            outcome="SUCCESS",
            matched_condition="規劃已完成",
        )
        rendered, plan_id = publish_completed_plan(
            ledger=ledger,
            task=task,
            final_summary="修改規劃：先改 A，再驗證 B。",
            progress=progress,
        )
        assert plan_id.startswith("PLAN-") and len(plan_id) == 25
        assert f"執行 {plan_id}" in rendered
        record = ledger.load(plan_id)
        assert record["final_summary"] == "修改規劃：先改 A，再驗證 B。"
        assert record["progress"]["decision"] == "COMPLETE"

        failed_progress = TaskProgressLedger.from_dict(
            {**progress.to_dict(), "outcome": "FAILED"}
        )
        unchanged, failed_plan_id = publish_completed_plan(
            ledger=ledger,
            task=task,
            final_summary="規劃失敗",
            progress=failed_progress,
        )
        assert unchanged == "規劃失敗" and failed_plan_id == ""

        queue = RemoteTaskQueue(TaskStateStore(task_store_path))
        router = TransportSessionRouter(root / "runtime" / "sessions.json")
        gateway = AgentIngressGateway(task_queue=queue, session_router=router)
        accepted = gateway.accept(message(f"執行 {plan_id}", "2"), workspace=str(workspace))
        assert accepted.created and accepted.task is not None
        assert "[SMARTAGENT_EXECUTE_SAVED_PLAN]" in accepted.task.request
        assert "修改規劃：先改 A，再驗證 B。" in accepted.task.request
        assert accepted.task.metadata["source_plan_id"] == plan_id
        assert accepted.task.metadata["source_user_command"] == f"執行 {plan_id}"

        missing = gateway.accept(
            message("執行 PLAN-FFFFFFFFFFFFFFFFFFFF", "3"),
            workspace=str(workspace),
        )
        assert missing.task is None and "找不到規劃" in missing.response

        other_workspace = root / "other"
        other_workspace.mkdir()
        mismatch = gateway.accept(
            message(f"執行 {plan_id}", "4"), workspace=str(other_workspace)
        )
        assert mismatch.task is None and "其他 Workspace" in mismatch.response

        delivered = TelegramDeliveryAdapter.render(
            {
                "event_type": "TASK_COMPLETED",
                "event_id": "EVT-1",
                "request_id": task.request_id,
                "task_id": task.task_id,
                "status": "COMPLETED",
                "payload": {"summary": "已準備附件。", "plan_id": plan_id},
            }
        )
        assert f"plan_id: {plan_id}" in delivered
        assert f"執行 {plan_id}" in delivered

    print("SMARTAGENT_PLAN_LEDGER_OK")


if __name__ == "__main__":
    main()
