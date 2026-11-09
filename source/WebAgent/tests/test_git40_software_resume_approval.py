"""Git40: software pre-resume approval, Telegram/WebCopilot, once-only matching."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.task_continuation import TaskContinuationStore, task_owner
from agent_core.resume_command_approval import (
    ResumeCommandGate, ResumeApprovalError, format_resume_approval_prompt,
)
from agent_core.webcopilot_resume_dialog import (
    show_resume_dialog, poll_resume_dialog, close_resume_dialog,
)
from agent_core.security_approval import SecurityApprovalLedger
from agent_core.paths import remote_events_path
from RemoteAgent.remote_feature_catalog import security_approval_inline_keyboard


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def setup_source(root: Path, owner: str, workspace: Path, *,
                 cmd="python check.py", with_secure=True,
                 approval_id="APR-" + "A"*20):
    store = TaskContinuationStore(root)
    source_id = "TASK-" + "A"*20
    request_id = "RR-TELEGRAM-" + "B"*16
    store.register(task_id=source_id, request_id=request_id, epoch="epoch-source",
                   goal="執行並驗證文件", workspace=workspace, owner=owner)
    action = {
        "tool": "run_command", "action_id": "MIN-TEST",
        "command": cmd, "operation": "GENERAL",
        "working_directory": str(workspace),
        "success_criteria": "exit code zero",
        "verify": [{"action": "file_exists", "path": str(workspace / "result.json"),
                    "expect": True}],
    }
    pending = {
        "tool": "run_command", "action_id": "MIN-TEST",
        "approval_required": True, "approval_kind": "UNKNOWN_COMMAND",
        "approval_id": approval_id, "command_sha256": _sha(cmd),
        "operation": "GENERAL", "executed": False,
    }
    if with_secure:
        ResumeCommandGate(root).remember(
            loop=SimpleNamespace(
                task_id=source_id, run_id=request_id, task_epoch="epoch-source",
                continuation_owner=owner, workspace=workspace,
            ),
            action=action, old_approval_id=approval_id,
        )
    store.pending_batch(source_id, [pending])
    store.finish(source_id, "PAUSED", "HUMAN_APPROVAL_TIMEOUT")
    return source_id, action, pending


def begin(root, source_id, owner, workspace, *, transport="TELEGRAM", channel_id="456"):
    return ResumeCommandGate(root).begin(
        request=f"接續工作 {source_id}",
        owner=owner, workspace=str(workspace),
        channel_id=channel_id, transport=transport,
        incoming={"text": f"接續工作 {source_id}", "test": "source is local"},
    )


def test_pre_resume_does_not_queue_or_model_and_consumes_exact_command_once(tmp_path):
    owner = "telegram:123:456"
    source, action, _ = setup_source(tmp_path, owner, tmp_path)
    gate = ResumeCommandGate(tmp_path)
    pre = begin(tmp_path, source, owner, tmp_path)
    assert pre["state"] == "PENDING"
    assert pre["source_task_id"] == source
    assert pre["command_sha256"] == _sha(action["command"])
    assert "python check.py" in format_resume_approval_prompt(pre)
    assert gate.public(pre["approval_id"])["state"] == "PENDING"
    decided = gate.decide(pre["approval_id"], allowed=True,
                          owner=owner, channel_id="456")
    assert decided["state"] == "APPROVED"
    assert decided["incoming"]["text"] == f"接續工作 {source}"
    approval_record = SecurityApprovalLedger(tmp_path).get(pre["approval_id"])
    assert approval_record["state"] == "APPROVED"
    assert approval_record["expires_at"] - approval_record["approved_at"] >= 3599
    child = "TASK-" + "C"*20
    gate.bind_child(pre["approval_id"], child_task_id=child, owner=owner)
    loop = SimpleNamespace(
        task_id=child, continuation_owner=owner, workspace=tmp_path,
        continuation_source={"task_id": source},
    )
    altered = dict(action, command="python other.py")
    assert gate.consume_for_action(loop=loop, action=altered) is None
    assert gate.consume_for_action(loop=loop, action=action)["state"] == "APPROVED"
    assert gate.consume_for_action(loop=loop, action=action) is None
    assert SecurityApprovalLedger(tmp_path).get(pre["approval_id"])["state"] == "CONSUMED"


def test_pre_resume_denial_is_saved_and_cannot_execute(tmp_path):
    owner="telegram:123:456"
    source, action, _=setup_source(tmp_path,owner,tmp_path)
    gate=ResumeCommandGate(tmp_path)
    pre=begin(tmp_path,source,owner,tmp_path)
    outcome=gate.decide(pre["approval_id"],allowed=False,owner=owner,channel_id="456")
    assert outcome["state"]=="REJECTED"
    gate.bind_child(pre["approval_id"], child_task_id="TASK-"+"D"*20,owner=owner)
    loop=SimpleNamespace(task_id="TASK-"+"D"*20,continuation_owner=owner,
                         workspace=tmp_path, continuation_source={"task_id":source})
    assert gate.consume_for_action(loop=loop,action=action)["state"]=="DENIED"
    assert gate.consume_for_action(loop=loop,action=action) is None
    assert SecurityApprovalLedger(tmp_path).get(pre["approval_id"])["state"]=="REJECTED"


def test_wrong_owner_or_workspace_fails_closed(tmp_path):
    owner="telegram:123:456"
    source, action, _=setup_source(tmp_path,owner,tmp_path)
    with pytest.raises(ResumeApprovalError, match="source_not_eligible"):
        begin(tmp_path,source,"telegram:999:456",tmp_path)
    other=tmp_path/"other"; other.mkdir()
    with pytest.raises(ResumeApprovalError, match="source_not_eligible"):
        begin(tmp_path,source,owner,other)
    pre=begin(tmp_path,source,owner,tmp_path)
    with pytest.raises(ResumeApprovalError,match="owner_mismatch"):
        ResumeCommandGate(tmp_path).decide(
            pre["approval_id"], allowed=True, owner=owner, channel_id="999",
        )
    assert ResumeCommandGate(tmp_path).public(pre["approval_id"])["state"]=="PENDING"


def test_legacy_telegram_timeout_recovers_exact_command_only_with_hash_and_event(tmp_path):
    owner="telegram:123:456"
    source, action, pending=setup_source(
        tmp_path,owner,tmp_path,with_secure=False,
    )
    path=remote_events_path(tmp_path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({"events":{
        "EVT-SAFE":{"event_type":"SECURITY_CONFIRMATION_REQUIRED",
                    "task_id":source,"payload":{
                        "approval_id":pending["approval_id"],
                        "approval_kind":"UNKNOWN_COMMAND",
                        "command":action["command"],
                        "command_sha256":pending["command_sha256"],
                        "cwd":str(tmp_path),
                    }},
    }}),encoding="utf-8")
    pre=begin(tmp_path,source,owner,tmp_path)
    assert pre and pre["command_sha256"]==_sha(action["command"])
    # Cannot recover altered command data from the old event.
    path.write_text(json.dumps({"events":{"EVT-ALTERED":{
        "event_type":"SECURITY_CONFIRMATION_REQUIRED","task_id":source,
        "payload":{
            "approval_id":pending["approval_id"],
            "approval_kind":"UNKNOWN_COMMAND",
            "command":"python malicious.py",
            "command_sha256":pending["command_sha256"],
            "cwd":str(tmp_path),
        },
    }}}),encoding="utf-8")
    with pytest.raises(ResumeApprovalError,match="exact_command_not_recorded"):
        begin(tmp_path,source,owner,tmp_path)


def test_webcopilot_owner_is_scoped_to_conversation_not_same_username():
    owner = lambda url: task_owner(SimpleNamespace(
        metadata={"transport":"WEBGPT_COPILOT"},
        reply_route={"conversation_key":url},
    ))
    assert owner("https://chatgpt.com/c/abc")==owner("https://chatgpt.com/c/abc")
    assert owner("https://chatgpt.com/c/abc")!=owner("https://chatgpt.com/c/other")
    assert owner("https://chatgpt.com/c/abc").startswith("webcopilot:")


def test_webcopilot_modal_renders_explicit_choices_without_model_tool():
    class FakePage:
        def __init__(self):
            self.requests=[]
        def evaluate(self, script, arg):
            self.requests.append((script,arg))
            if "dataset.choice" in script and "return node.dataset.clicked" in script:
                return "APPROVED"
            return True
    page=FakePage()
    pre={"approval_id":"APR-"+"A"*20}
    assert show_resume_dialog(page,pre,"確認執行 python check.py")
    assert "event.isTrusted" in page.requests[0][0]
    assert "允許執行一次" in page.requests[0][0]
    assert "拒絕" in page.requests[0][0]
    assert "textContent=message" in page.requests[0][0]
    assert poll_resume_dialog(page,pre["approval_id"])=="APPROVED"
    close_resume_dialog(page,pre["approval_id"])
    assert len(page.requests)==3


def test_telegram_resume_button_is_one_time_not_delete():
    markup=security_approval_inline_keyboard("APR-"+"A"*20,approval_kind="RESUME_UNKNOWN_COMMAND")
    buttons=markup["inline_keyboard"][0]
    assert [b["text"] for b in buttons]==["允許執行一次","拒絕"]
    assert buttons[0]["callback_data"]=="security_approval:APR-"+"A"*20


def test_pre_resume_no_challenge_when_no_paused_pending_command(tmp_path):
    store=TaskContinuationStore(tmp_path)
    owner="telegram:123:456"
    source="TASK-"+"A"*20
    store.register(task_id=source,request_id="RR-TEST",epoch="epoch",
                   goal="task",workspace=tmp_path,owner=owner)
    store.finish(source,"PAUSED","no pending")
    assert begin(tmp_path,source,owner,tmp_path) is None
