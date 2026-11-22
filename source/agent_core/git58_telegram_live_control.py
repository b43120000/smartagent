"""Git58 authenticated one-message Telegram Real Planner E2E control.

A single exact /git58_live_repair command is consumed by the *authorized*
Telegram receiver, never by a Planner. The task entering the Goal compiler is
only a normal, read-only directory existence question. A one-shot response
fault injector is then bound by the Worker to the exact task/request/epoch.
No global injection flag, Telegram Host CMD or scope/ACL override exists.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from dataclasses import replace
from typing import Any, Mapping

COMMAND = "/git58_live_repair"
SCHEMA = "SMARTAGENT_GIT58_AUTHENTICATED_TELEGRAM_TEST_V1"
LEAF = "GIT58_LIVE_NONEXISTENT_"
MARKER = "git58_real_planner_live"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def prepare_message(message, *, workspace: str, update_id: int):
    """Called only AFTER Telegram private-chat identity authorization."""
    if str(message.text or "").strip() != COMMAND:
        return None
    if os.name != "nt":
        raise ValueError("GIT58_WINDOWS_LIVE_REQUIRED")
    if message.transport != "TELEGRAM" or not str(message.sender_id).isdigit():
        raise ValueError("GIT58_AUTHENTICATED_TELEGRAM_REQUIRED")
    if message.attachments:
        raise ValueError("GIT58_ATTACHMENTS_FORBIDDEN")
    if type(update_id) is not int or update_id < 0:
        raise ValueError("GIT58_UPDATE_ID_INVALID")
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("GIT58_WORKSPACE_UNAVAILABLE")
    # Unique for every real Telegram Bot API update. The test does not create
    # or modify this path; a present target makes the test fail-closed.
    target = root / f"{LEAF}{update_id}"
    if target.exists() or target.is_symlink():
        raise ValueError("GIT58_TARGET_NOT_ABSENT")
    request = "這個資料夾是否存在？\n\n" + str(target)
    metadata = {
        **dict(message.metadata or {}),
        MARKER: {
            "schema": SCHEMA,
            "source_transport": "TELEGRAM",
            "source_message_id": str(message.source_message_id),
            "source_update_id": update_id,
            "request_sha256": _sha(request),
            "target": str(target),
            "planner_injection": "FIRST_VALID_FINISH_BOOLEAN_FLIP_ONCE",
            "authorized_by": "AUTHENTICATED_TELEGRAM_CONTROL",
            "authorization_granted": False,
        },
    }
    return replace(message, text=request, metadata=metadata)


def verify_worker_task(task, *, source_transport: str):
    """Return marker only for a trusted, request-bound control handoff.

    This is a testing selector, NEVER tool execution authorization. Every
    proposed ToolCall goes through ordinary Runtime security and OS policy.
    """
    ingress = dict((task.metadata or {}).get("ingress_metadata") or {})
    marker = ingress.get(MARKER)
    if marker is None:
        return None
    if (source_transport != "TELEGRAM"
            or not isinstance(marker, Mapping)
            or marker.get("schema") != SCHEMA
            or marker.get("source_transport") != source_transport
            or marker.get("authorized_by") != "AUTHENTICATED_TELEGRAM_CONTROL"
            or marker.get("planner_injection") != "FIRST_VALID_FINISH_BOOLEAN_FLIP_ONCE"
            or marker.get("authorization_granted") is not False):
        raise RuntimeError("GIT58_TEST_CONTROL_BINDING_INVALID")
    if (not isinstance(marker.get("source_update_id"), int)
            or marker["source_update_id"] < 0
            or str(marker.get("source_message_id") or "")
            != str((task.metadata or {}).get("source_message_id") or "")):
        raise RuntimeError("GIT58_TEST_CONTROL_SOURCE_IDENTITY_MISMATCH")
    target = str(marker.get("target") or "")
    if not re.fullmatch(r".+[\\/]GIT58_LIVE_NONEXISTENT_[0-9]+", target):
        raise RuntimeError("GIT58_TEST_CONTROL_TARGET_INVALID")
    if (not str(target).endswith(LEAF + str(marker["source_update_id"]))
            or Path(target).parent.resolve() != Path(task.workspace).resolve()
            or Path(target).exists() or Path(target).is_symlink()):
        raise RuntimeError("GIT58_TEST_CONTROL_WORKSPACE_TARGET_MISMATCH")
    request = "這個資料夾是否存在？\n\n" + target
    if (str(task.request) != request
            or _sha(request) != marker.get("request_sha256")
            or str((task.metadata or {}).get("source_user_command") or "")
            != request):
        raise RuntimeError("GIT58_TEST_CONTROL_GOAL_BINDING_MISMATCH")
    if not all((str(getattr(task, key, "") or "") for key in (
            "task_id", "request_id", "task_epoch"))):
        raise RuntimeError("GIT58_TEST_CONTROL_TASK_IDENTITY_MISSING")
    return dict(marker)


def install_real_planner_injector(loop, task, *, marker):
    """Wrap exactly this Telegram Worker's live DOM GPT Planner callable."""
    if not marker:
        raise RuntimeError("GIT58_TEST_CONTROL_NOT_ARMED")
    if not getattr(loop, "executor_only_mode", False):
        raise RuntimeError("GIT58_EXECUTOR_ONLY_REQUIRED")
    from agent_core.git57_live_finish_injector import TestOnlyFirstFinishInjector

    original = loop.planner
    identity = (
        str(task.task_id), str(task.request_id), str(task.task_epoch),
        _sha(str(task.request)),
    )

    def bound_real_planner(prompt, expected, attachments):
        observed = (
            str(getattr(loop, "task_id", "") or ""),
            str(getattr(loop, "run_id", "") or ""),
            str(getattr(loop, "task_epoch", "") or ""),
            _sha(str(getattr(loop, "task_request", "") or "")),
        )
        if observed != identity:
            raise RuntimeError("GIT58_PLANNER_TASK_EPOCH_IDENTITY_MISMATCH")
        return original(prompt, expected, attachments)

    injector = TestOnlyFirstFinishInjector(bound_real_planner, loop)
    loop.planner = injector
    return injector


def assess_real_planner_live(root, task, loop, injector) -> dict[str, Any]:
    """Require actual Runtime receipts; do not trust the Planner summary."""
    from agent_core.executor_run_blackboard import (
        read_executor_run, read_executor_event,
    )
    from agent_core.requirement_goal_state import (
        compile_requirement_goal_state, validate_frozen_goal_contract,
    )
    from agent_core.runtime_evidence_store import read_runtime_evidence
    from agent_core.tool_result_contract import decode_result

    board = read_executor_run(
        root, task_id=task.task_id, request_id=task.request_id,
        task_epoch=task.task_epoch,
    )
    if not isinstance(board, dict):
        raise RuntimeError("GIT58_BLACKBOARD_MISSING")
    events = [
        read_executor_event(
            root, task_id=task.task_id, request_id=task.request_id,
            task_epoch=task.task_epoch, sequence=sequence,
        )
        for sequence in range(1, 1 + int(board.get("event_count_total", 0)))
    ]
    tools = [x for x in events if x.get("kind") == "TOOL_RESULT"]
    injects = [x for x in events if x.get("kind") == "GIT57_TEST_FINISH_INJECTION"]
    repairs = [x for x in events if x.get("kind") == "MODEL_FINISH_SEMANTIC_REPAIR"]
    finishes = [x for x in events if x.get("kind") == "MODEL_FINISH"]
    ledger = list(getattr(loop, "action_result_ledger", {}).values())
    accepted = dict(getattr(loop, "terminal_acceptance", {}) or {})
    contract = board.get("goal_state_contract") or {}
    reason = "GIT58_REAL_PLANNER_LIVE_VALIDATED"
    check = (
        injector.injected is True
        and len(injector.events) == len(injects) == 1
        and len(tools) == len(ledger) == 1
        and len(repairs) == len(finishes) == 1
        and injects[0].get("target_evidence_ref")
            == ledger[0].get("evidence_ref")
        and repairs[0].get("reason_code") == "MODEL_RUNTIME_MISMATCH"
        and repairs[0].get("failure_origin") == "PLANNER_ASSERTION_MISMATCH"
        and repairs[0].get("repair_only") is True
        and accepted.get("final_verdict") == "PASS"
        and accepted.get("comparison", {}).get("reason_code")
            == "FROZEN_GOAL_STATE_SATISFIED"
        and accepted.get("goal_coverage_basis")
            == "RUNTIME_FROZEN_GOAL_CONTRACT"
        and contract.get("status") == "COMPILED"
        and contract.get("goal_kind") == "DIRECTORY_OBSERVATION"
        and validate_frozen_goal_contract(contract, task.request)
        and contract == compile_requirement_goal_state(task.request)
    )
    ref = str(ledger[0].get("evidence_ref") or "") if ledger else ""
    try:
        receipt = decode_result(ledger[0]["result"]) if ledger else {}
        data = (receipt or {}).get("data") or {}
        evidence = read_runtime_evidence(
            root, task_id=task.task_id, request_id=task.request_id,
            task_epoch=task.task_epoch, evidence_ref=ref,
            section="raw_result", max_chars=2400,
        )
        real_result_ok = bool(
            (receipt or {}).get("status") == "SUCCEEDED"
            and (receipt or {}).get("executed") is True
            and ledger[0].get("tool") == "inspect_directory"
            and data.get("observation_status") == "CONFIRMED"
            and data.get("observed_exists") is False
            and (ledger[0].get("evidence_store") or {}).get("store_status")
                == "PERSISTED_AND_READBACK_VERIFIED"
            and evidence.get("task_bound") is True
            and evidence.get("source_result_sha256")
                == (ledger[0].get("evidence_store") or {}).get("source_sha256")
        )
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        real_result_ok = False
    result = {
        "schema": "SMARTAGENT_GIT58_REAL_PLANNER_LIVE_REPORT_V1",
        "task_id": task.task_id,
        "request_id": task.request_id,
        "task_epoch": task.task_epoch,
        "tool_execution_count": len(tools),
        "injection_count": len(injects),
        "semantic_repair_count": len(repairs),
        "finish_count": len(finishes) + len(repairs),
        "observed_exists": False if real_result_ok else None,
        "evidence_ref": ref,
        "goal_contract_status": contract.get("status"),
        "goal_contract_unchanged": bool(
            validate_frozen_goal_contract(contract, task.request)
            and contract == compile_requirement_goal_state(task.request)
        ),
        "comparator_cause": accepted.get("comparison", {}).get("reason_code", ""),
        "final_verdict": accepted.get("final_verdict", "NOT_ADMITTED"),
        "dom_real_planner": True,
        "verdict": "GIT58_REAL_PLANNER_LIVE_PASS"
            if check and real_result_ok else "GIT58_REAL_PLANNER_LIVE_FAIL",
    }
    return result


__all__ = [
    "COMMAND", "MARKER", "SCHEMA", "prepare_message",
    "verify_worker_task", "install_real_planner_injector",
    "assess_real_planner_live",
]
