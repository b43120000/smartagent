"""Git75 P0 proof: fewer Planner roundtrips and real Project Sync freshness."""
from __future__ import annotations

import json

from agent_core.goal75_stage_ledger import GoalStageLedger
from agent_core.goal75_project_sync_it import run as run_real_project_sync
from agent_core.tool_result_contract import encode_result
from agent_core.executor_run_blackboard import read_executor_run
from WebAgent.pokeclaw_executor_loop import run_pokeclaw_executor
from WebAgent.tests.test_minimal_core_v10 import FakeLoop


def _stage(sid: str, predecessor: str = "") -> dict:
    return {
        "stage_id": sid, "description": f"Verify original {sid}",
        "depends_on": [predecessor] if predecessor else [],
        "acceptance": [
            {"field": "data.fixture_phase", "operator": "eq", "expected": sid},
            {"field": "data.verification_status", "operator": "eq",
             "expected": "PASS"},
        ],
    }


def _receipt(sid: str, verification: str = "PASS") -> dict:
    return {"status": "SUCCEEDED", "executed": True,
            "data": {"fixture_phase": sid,
                     "verification_status": verification,
                     "exit_code": 0}}


def _runtime_fixture(tmp_path, monkeypatch, *, goal: str):
    monkeypatch.setattr("agent_core.task_progress.set_runtime_state",
                        lambda *a, **kw: None)
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.admit_action",
        lambda *a, **kw: {"decision": "NORMAL",
                         "reason_code": "TEST_ACL_OFF"},
    )
    loop = FakeLoop(tmp_path, goal, [], "")
    loop.executor_only_mode = True
    loop.max_turns = 12
    return loop


def test_stage_auto_commit_requires_persisted_expected_and_frozen_acceptance(tmp_path):
    ledger = GoalStageLedger(
        tmp_path, task_id="TASK-P0-RUNTIME",
        request_id="RR-P0-RUNTIME", task_epoch="EPOCH-P0-RUNTIME",
        goal="P0 and P1",
    )
    ledger.create([_stage("P0"), _stage("P1", "P0")])
    ledger.record_action(
        "P0", action_id="ACT-WITHOUT-EXPECTED",
        evidence_ref="MIN-EV-" + "a" * 24,
        receipt=_receipt("P0"), expected_match=None,
        evidence_store_verified=True,
    )
    assert ledger.commit_if_independently_verified("P0") is None
    ledger.record_action(
        "P0", action_id="ACT-WITHOUT-PERSISTENCE",
        evidence_ref="MIN-EV-" + "b" * 24,
        receipt=_receipt("P0"), expected_match=True,
        evidence_store_verified=False,
    )
    assert ledger.commit_if_independently_verified("P0") is None
    ledger.record_action(
        "P0", action_id="ACT-FAILED-VERIFICATION",
        evidence_ref="MIN-EV-" + "c" * 24,
        receipt=_receipt("P0", "FAIL"), expected_match=False,
        evidence_store_verified=True,
    )
    assert ledger.commit_if_independently_verified("P0") is None
    ref = "MIN-EV-" + "d" * 24
    ledger.record_action(
        "P0", action_id="ACT-VERIFIED",
        evidence_ref=ref, receipt=_receipt("P0"),
        expected_match=True, evidence_store_verified=True,
    )
    accepted = ledger.commit_if_independently_verified("P0")
    assert accepted is not None
    stage = accepted["stages"][0]
    assert stage["status"] == "VERIFIED_COMMITTED"
    assert stage["commit"]["evidence_refs"] == [ref]
    assert stage["strategy_revision"] == 0
    assert ledger.current()["stage_id"] == "P1"


def test_real_executor_only_loop_auto_commits_stages_without_extra_planner_turn(
    tmp_path, monkeypatch,
):
    """Actual Planner->Admission->MIN-EV->Ledger->Finish; OS tool mocked."""
    goal = "請完成 P0 Git 查詢與 P1 Git 查詢並取得真實驗證，不得假報完成"
    loop = _runtime_fixture(tmp_path, monkeypatch, goal=goal)
    phases = [_stage("P0"), _stage("P1", "P0")]
    expected = {
        "contract_version": 1, "aggregation": "ALL",
        "assertions": [{"field": "data.exit_code", "operator": "eq",
                        "value": 0}],
    }
    commands = ["git status --short", "git log -1 --oneline"]

    def planner(prompt, *_):
        loop.prompts.append(prompt)
        turn = len(loop.prompts)
        if turn == 1:
            return json.dumps({"type": "stage_plan", "stages": phases})
        if turn in (2, 3):
            command = commands[turn - 2]
            if turn == 3:
                assert '"current_stage_id":"P1"' in prompt, prompt[-1300:]
            return json.dumps({
                "type": "tool_call", "name": "run_command",
                "arguments": {"command": command,
                              "operation": "GIT_INSPECT",
                              "expected": expected},
            })
        if turn == 4:
            last = list(loop.action_result_ledger.values())[-1]["result_id"]
            return json.dumps({
                "type": "finish", "contract_version": 2,
                "summary": "P0、P1 的真實命令驗證完成",
                "goal_checks": [{
                    "criterion": "P1 命令已返回",
                    "evidence_ref": last, "field": "data.exit_code",
                    "operator": "eq", "expected": 0,
                }],
            }, ensure_ascii=False)
        raise AssertionError(f"redundant Planner round {turn}")

    def execute(action):
        loop.executed.append(dict(action))
        sid = "P0" if action["command"] == commands[0] else "P1"
        assert action["command"] in commands
        return encode_result(
            "SUCCEEDED", "COMMAND_EXITED", tool="run_command",
            executed=True, data={
                "exit_code": 0, "process_state": "EXITED",
                "process_started": True, "fixture_phase": sid,
                "verification_status": "PASS", "stdout": "",
                "stderr": "", "timed_out": False,
            },
        )

    loop.planner = planner
    loop._execute_action = execute
    reply = run_pokeclaw_executor(loop, goal)
    ledger = GoalStageLedger(
        tmp_path, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch, goal=goal)
    record = ledger.read()
    assert record and all(
        s["status"] == "VERIFIED_COMMITTED" for s in record["stages"]
    ), reply
    assert len(loop.executed) == 2
    assert len(loop.prompts) == 4, reply
    board = read_executor_run(
        tmp_path, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch)
    savings = [e for e in board["events"]
               if e["kind"] == "GOAL75_PLANNER_ROUND_SAVED"]
    assert len(savings) == 2, board["events"][-8:]
    assert sum(e["rounds_saved"] for e in savings) == 2
    assert loop.terminal_outcome == "SUCCESS", reply


def test_schema_alias_can_be_repaired_without_second_planner_round(
    tmp_path, monkeypatch,
):
    goal = "請查詢 Git 狀態並回報退出碼"
    loop = _runtime_fixture(tmp_path, monkeypatch, goal=goal)
    calls = []

    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if len(loop.prompts) == 1:
            # Lossless alias; command bytes and policy must not change.
            return json.dumps({
                "type": "tool_call", "name": "run_command",
                "args": {"operation": "GIT_INSPECT",
                         "command": "git status --short"},
            })
        last = list(loop.action_result_ledger.values())[-1]["result_id"]
        return json.dumps({
            "type": "finish", "contract_version": 2,
            "summary": "Git 狀態已確認",
            "goal_checks": [{"criterion": "退出碼",
                             "evidence_ref": last, "field": "data.exit_code",
                             "operator": "eq", "expected": 0}],
        }, ensure_ascii=False)

    def execute(action):
        calls.append(action["command"])
        return encode_result(
            "SUCCEEDED", "COMMAND_EXITED", tool="run_command", executed=True,
            data={"process_started": True, "process_state": "EXITED",
                  "exit_code": 0, "verification_status": "PASS",
                  "stdout": "", "stderr": ""},
        )
    loop.planner = planner
    loop._execute_action = execute
    run_pokeclaw_executor(loop, goal)
    assert calls == ["git status --short"]
    assert len(loop.prompts) == 2
    board = read_executor_run(
        tmp_path, task_id=loop.task_id,
        request_id=loop.run_id, task_epoch=loop.task_epoch)
    changes = [e for e in board["events"]
               if e["kind"] == "GOAL75_LOCAL_SCHEMA_REPAIR"]
    assert len(changes) == 1
    assert changes[0]["changes"] == ["args_to_arguments"]


def test_project_sync_real_file_index_staleness_and_recovery(tmp_path):
    result = run_real_project_sync(tmp_path)
    assert result["status"] == "PASS", result
    assert result["snapshot_changed"]
    assert result["pinned_context_ref_rejected_on_stale_bytes"]
    assert result["unpinned_query_rebuilt_index_once"]
    assert result["previous_bytes_absent_after_refresh"]
    assert result["outside_scope_rejected"]
    assert result["telegram_live_verified"] is False

def test_project_sync_mutator_never_executes_after_native_broker_denial(
    tmp_path, monkeypatch,
):
    """Full production dispatch fails closed, regardless of sidecar success."""
    from agent_core.tools import execute_tool
    from agent_core.tool_result_contract import decode_result
    from agent_core.goal75_project_sync_it import FixtureAgent

    workspace = tmp_path / "workspace"
    project = workspace / "project"
    project.mkdir(parents=True)
    (project / "proof.py").write_text("VALUE = 1\\n", encoding="utf-8")
    agent = FixtureAgent(workspace, project)

    def deny(*_args, **_kwargs):
        return {
            "decision": "BLOCKED",
            "reason_code": "TEST_NATIVE_BROKER_NOT_AVAILABLE",
            "evidence": {
                "reason": "TEST_DENIAL_NO_HOST_FALLBACK",
                "authorization_granted": False,
            },
        }

    monkeypatch.setattr("agent_core.turbo_broker_admission.assess", deny)
    result = decode_result(execute_tool({
        "tool": "project_sync", "strategy": "INDEX_ONLY",
        "project_root": str(project), "action_id": "ACT-P0-BROKER-DENIED",
    }, agent=agent))
    assert result and result["status"] == "REJECTED"
    assert result["executed"] is False
    assert result["reason"] == "TURBO_OS_BROKER_BLOCKED"
    assert not (project / ".agents" / "project_context" / "current.json").exists()

def test_real_project_source_and_unified_preflight_stale_refresh_transition(
    tmp_path, monkeypatch,
):
    """Actual index + query bytes; Preflight status is a distinct hint."""
    from agent_core import unified_preflight as pf
    from agent_core.project_access import (
        build_project_capsule, query_project_with_runtime_index,
    )
    from WebAgent.tests.test_git74_unified_preflight import fake_mode, fake_probe

    fake_mode(monkeypatch)  # Runner has no user's Windows ACL policy.
    install = tmp_path / "install"
    install.mkdir()
    project = tmp_path / "workspace"
    project.mkdir()
    target = project / "main.py"
    target.write_bytes(b"VALUE = 'A0'\\n")
    old = build_project_capsule(project)
    baseline = pf.begin_task(
        install, project, task_id="TASK-P0-SYNC-IT",
        request_id="RR-P0-SYNC-IT", probe=fake_probe([]),
    )
    assert baseline["project_sync"]["status"] == "CACHED_INDEX_HINT"
    assert baseline["project_sync"]["snapshot_id"] == old["snapshot_id"]

    original_stat = target.stat()
    target.write_bytes(b"VALUE = 'B1'\\n")
    import os
    os.utime(target, ns=(
        original_stat.st_atime_ns, original_stat.st_mtime_ns,
    ))
    stale = pf.action_observed(
        install, project, task_id="TASK-P0-SYNC-IT",
        request_id="RR-P0-SYNC-IT",
        action={"tool": "run_command", "operation": "BUILD",
                "working_directory": str(project)},
        receipt={"status": "SUCCEEDED", "executed": True,
                 "data": {"exit_code": 0}},
        evidence_ref="MIN-EV-" + "9" * 24, probe=fake_probe([]),
    )
    assert stale["project_sync"]["status"] == "STALE_REINDEX_REQUIRED"
    # Admission denial cannot make an outdated source index current.
    rejected = pf.action_observed(
        install, project, task_id="TASK-P0-SYNC-IT",
        request_id="RR-P0-SYNC-IT",
        action={"tool": "project_sync", "strategy": "INDEX_ONLY"},
        receipt={"status": "REJECTED", "executed": False, "data": {}},
        evidence_ref="MIN-EV-" + "8" * 24, probe=fake_probe([]),
    )
    assert rejected["project_sync"]["status"] == "STALE_REINDEX_REQUIRED"

    actual = query_project_with_runtime_index(
        project, queries=[{
            "operation": "read_range", "path": "main.py",
            "start_line": 1, "end_line": 2,
        }],
    )
    assert actual["status"] == "READY"
    assert actual["index_context"]["recovery_action"] == "INDEX_REBUILT_ONCE"
    assert "B1" in actual["results"][0]["content"]
    assert "A0" not in actual["results"][0]["content"]
    assert actual["results"][0]["file_sha256"] == (
        __import__("hashlib").sha256(target.read_bytes()).hexdigest()
    )
    refreshed = pf.action_observed(
        install, project, task_id="TASK-P0-SYNC-IT",
        request_id="RR-P0-SYNC-IT",
        action={"tool": "query_project", "project_root": str(project)},
        receipt={"status": "SUCCEEDED", "executed": True,
                 "data": {"query_id": actual["query_id"],
                          "snapshot_id": actual["snapshot_id"]}},
        match_report={"match_state": "MATCH"},
        evidence_ref="MIN-EV-" + "7" * 24, probe=fake_probe([]),
    )
    assert refreshed["project_sync"]["status"] == (
        "INDEX_REFRESHED_BY_ADMITTED_ACTION"
    )
    assert refreshed["project_sync"]["snapshot_id"] == actual["snapshot_id"]
    assert refreshed["source_snapshot_is_current_byte_evidence"] is False
