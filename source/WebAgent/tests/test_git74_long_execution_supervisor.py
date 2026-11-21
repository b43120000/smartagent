"""Git74 sidecar UT/IT: actual child, no deadline; callback/auto-consent, fail closed."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_core.long_execution_supervisor import (
    begin_wait, bind_notice, finish_notice, finish_wait, pending_notifications,
    read_wait, record_observation, set_decision, wait_until_exit,
)
from agent_core.process_liveness import observe_owned_process
from RemoteAgent.remote_feature_catalog import (
    command_for_remote_callback, long_wait_inline_keyboard,
)
from agent_core.smartagent_protocol import validate_tool_envelope


def test_keyboard_is_opaque_and_telegram_owned():
    markup = long_wait_inline_keyboard("WAIT-" + "A" * 20)
    choices = [b["callback_data"] for b in markup["inline_keyboard"][0]]
    assert len(choices) == 2
    assert command_for_remote_callback(choices[0]) == "long_wait_choice WAIT-" + "A" * 20 + " Y"
    assert command_for_remote_callback(choices[1]) == "long_wait_choice WAIT-" + "A" * 20 + " N"
    assert command_for_remote_callback("long_wait:INVALID:Y") is None


def test_wait_policy_canonical_admission():
    action = {"tool": "run_command", "operation": "TEST",
              "command": "Write-Output 'hello'", "shell_mode": "POWERSHELL",
              "wait_policy": "UNTIL_EXIT"}
    ok, issue = validate_tool_envelope(action, native_broker=True)
    assert ok, issue
    action["wait_policy"] = "NONE"
    ok, issue = validate_tool_envelope(action, native_broker=True)
    assert not ok
    assert issue["reason"] == "RUN_COMMAND_WAIT_POLICY_INVALID"


def test_no_reply_auto_accept_only_after_ten_minutes(tmp_path, monkeypatch):
    row = begin_wait(install=tmp_path, task_id="TASK-ONE", request_id="RR-ONE",
                     action_id="ACT-ONE", execution_id="exec-a", pid=123)
    import agent_core.long_execution_supervisor as sup
    fake = {"clock": row["created_at"] + 599}
    monkeypatch.setattr(sup.time, "time", lambda: fake["clock"])
    obs = {"execution_id":"exec-a", "pid":123, "observed_at":fake["clock"],
           "process_state":"RUNNING", "evidence_source":"OWNED_POPEN_WAITPID",
           "liveness_verified":True}
    record_observation(tmp_path, row["wait_id"], obs)
    assert read_wait(tmp_path, row["wait_id"])["decision"] == "PENDING"
    fake["clock"] += 2
    obs["observed_at"] = fake["clock"]
    record_observation(tmp_path, row["wait_id"], obs)
    assert read_wait(tmp_path, row["wait_id"])["decision"] == "AUTO_CONTINUE"
    assert read_wait(tmp_path, row["wait_id"])["state"] == "RUNNING"


def test_callback_binding_and_safe_pause(tmp_path):
    row = begin_wait(install=tmp_path, task_id="TASK-ONE", request_id="RR-ONE",
                     action_id="ACT-ONE", execution_id="exec-a", pid=123)
    assert len(pending_notifications(tmp_path)) == 1
    assert bind_notice(tmp_path, row["wait_id"], chat_id=8765)
    finish_notice(tmp_path, row["wait_id"], success=True)
    with pytest.raises(ValueError):
        set_decision(tmp_path, row["wait_id"], task_id="TASK-WRONG",
                     chat_id=8765, choice="CONTINUE")
    with pytest.raises(ValueError):
        set_decision(tmp_path, row["wait_id"], task_id="TASK-ONE",
                     chat_id=8888, choice="CONTINUE")
    updated = set_decision(tmp_path, row["wait_id"], task_id="TASK-ONE",
                           chat_id=8765, choice="PAUSE_AFTER_EXIT")
    assert updated["decision"] == "PAUSE_AFTER_EXIT"
    assert set_decision(tmp_path, row["wait_id"], task_id="TASK-ONE",
                        chat_id=8765, choice="CONTINUE")["decision"] == "PAUSE_AFTER_EXIT"


def test_completed_before_reply_and_os_identity_proven(tmp_path):
    row = begin_wait(install=tmp_path, task_id="TASK-ONE", request_id="RR-ONE",
                     action_id="ACT-ONE", execution_id="exec-a", pid=123)
    proof = {"execution_id":"exec-a", "pid":123, "process_state":"EXITED",
             "evidence_source":"OWNED_POPEN_WAITPID", "process_identity_verified":True}
    end = finish_wait(tmp_path, row["wait_id"], proof, 0)
    assert end["state"] == "COMPLETED"
    assert end["decision"] == "COMPLETED_BEFORE_DECISION"
    assert pending_notifications(tmp_path) == []
    assert end["exit_code"] == 0


def test_real_sidecar_python_process_waits_beyond_bounded_timeout(tmp_path):
    # A 2.2-second silent Python child proves wait_policy UNTIL_EXIT is not
    # secretly routed through the old one-second action deadline.
    cmd = [sys.executable, "-c", "import time; time.sleep(2.2)"]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        proof, end = wait_until_exit(process, install=tmp_path,
            telemetry={"task_id":"TASK-IT", "request_id":"RR-IT",
                       "action_id":"ACT-IT"}, execution_id="real-child",
            interval=0.1)
        assert proof["process_state"] == "EXITED"
        assert end["state"] == "COMPLETED"
        assert end["exit_code"] == 0
        assert end["liveness_sample_count"] >= 1
        assert end["decision"] == "COMPLETED_BEFORE_DECISION"
        assert (tmp_path / "localdata" / "runtime" / "long_execution" / "events.jsonl").exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_untrusted_exit_identity_cannot_complete_wait(tmp_path):
    row = begin_wait(install=tmp_path, task_id="TASK-ONE", request_id="RR-ONE",
                     action_id="ACT-ONE", execution_id="exec-a", pid=123)
    end = finish_wait(tmp_path, row["wait_id"],
        {"execution_id":"exec-a","pid":999,"process_state":"EXITED",
         "process_identity_verified":True}, exit_code=0)
    assert end["state"] == "UNVERIFIED"
    assert end["exit_code"] is None
