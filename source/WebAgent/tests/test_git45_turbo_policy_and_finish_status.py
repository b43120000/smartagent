"""Git45: generic Turbo execution limits, auditable decisions and P1 Finish repair."""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core import workspace_access, turbo_execution_policy
from agent_core.runtime_finish_status_repair import (
    diagnose_finish_status_mismatch, finish_runtime_status_catalog,
)
from agent_core.executor_run_blackboard import read_executor_run
from agent_core.tool_result_contract import encode_result
from WebAgent.minimal_core_loop import run_minimal_core
from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop, TARGET
from WebAgent.tests.test_minimal_core_v10 import tool_json


@pytest.fixture
def env(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    turbo = workspace / "turbo"
    blocked = tmp_path / "readonly"
    for p in (workspace, turbo, blocked):
        p.mkdir()
    path = tmp_path / "access_policy.json"
    monkeypatch.setattr(workspace_access, "workspace_access_policy_path", lambda: path)
    workspace_access.save_workspace_access_policy([workspace], [blocked])
    policy = workspace_access.load_workspace_access_policy()

    def snapshot():
        current = workspace_access.load_workspace_access_policy()
        return {
            "policy": current,
            "turbo_roots": tuple(current["turbo_roots"]),
            "writable_workspaces": tuple(current["writable_workspaces"]),
            "read_only_roots": tuple(current["read_only_roots"]),
        }

    monkeypatch.setattr(turbo_execution_policy, "access_snapshot", snapshot)
    loop = SimpleNamespace(
        workspace=workspace, task_request="取得 https://github.com/example/repo.git\n" +
        "提交 " + "a" * 40,
    )
    return workspace, turbo, blocked, loop


def evaluate(loop, command, cwd):
    return turbo_execution_policy.evaluate_turbo_confirmation(loop, {
        "tool": "run_command",
        "operation": "GIT_MUTATE",
        "command": command,
        "working_directory": str(cwd),
    })


def make_tar(path, *, malicious=False, symlink=False):
    with tarfile.open(path, mode="w") as dest:
        row = tarfile.TarInfo(
            "../escape.txt" if malicious else "payload/data.csv",
        )
        if symlink:
            row.type = tarfile.SYMTYPE
            row.linkname = "../../outside"
            dest.addfile(row)
        else:
            data = b"header\nhello\n"
            row.size = len(data)
            dest.addfile(row, io.BytesIO(data))


def test_turbo_disabled_by_default(env):
    workspace, turbo, blocked, loop = env
    result = evaluate(loop, "git ls-tree HEAD", turbo)
    assert result["auto_confirm"] is False
    assert result["reason_code"] == "TURBO_DISABLED"
    assert result["authorization_granted"] is False


def test_non_destructive_read_and_new_clone(env):
    workspace, turbo, blocked, loop = env
    workspace_access.set_turbo_root(turbo, workspace=workspace)
    assert evaluate(loop, "git ls-tree HEAD", turbo)["auto_confirm"] is True
    url = "https://github.com/example/repo.git"
    dest = turbo / "fresh-clone"
    command = f'git clone {url} "{dest}"'
    result = evaluate(loop, command, workspace)
    assert result["auto_confirm"] is True, result
    assert result["reason_code"] == "TURBO_NEW_GIT_CLONE"
    assert not dest.exists()  # preflight must not create anything
    dest.mkdir()
    refused = evaluate(loop, command, workspace)
    assert refused["auto_confirm"] is False
    assert refused["reason_code"] == "TURBO_CLONE_TARGET_ALREADY_EXISTS"


def test_clone_remote_must_be_in_original_request(env):
    workspace, turbo, blocked, loop = env
    workspace_access.set_turbo_root(turbo, workspace=workspace)
    command = f'git clone https://github.com/attacker/repo.git "{turbo / "new"}"'
    result = evaluate(loop, command, workspace)
    assert result["auto_confirm"] is False
    assert result["reason_code"] == "TURBO_REMOTE_NOT_BOUND_TO_REQUEST"


def test_archive_requires_new_destination_and_pinned_commit(env):
    workspace, turbo, blocked, loop = env
    workspace_access.set_turbo_root(turbo, workspace=workspace)
    repo = turbo / "source"
    (repo / ".git").mkdir(parents=True)
    tar = turbo / "source.tar"
    sha = "a" * 40
    cmd = (f'git -C "{repo}" archive --format=tar '
           f'--output={tar} {sha} subdir')
    good = evaluate(loop, cmd, workspace)
    assert good["auto_confirm"] is True, good
    assert good["reason_code"] == "TURBO_GIT_ARCHIVE_TO_NEW_FILE"
    tar.write_bytes(b"already exists")
    rejected = evaluate(loop, cmd, workspace)
    assert rejected["auto_confirm"] is False
    assert rejected["reason_code"] == "TURBO_ARCHIVE_MAY_OVERWRITE_OR_PARENT_MISSING"


def test_tar_extract_requires_held_safe_archive_and_empty_destination(env):
    workspace, turbo, blocked, loop = env
    workspace_access.set_turbo_root(turbo, workspace=workspace)
    archive = turbo / "source.tar"
    target = turbo / "deployment"
    target.mkdir()
    make_tar(archive)
    assert evaluate(loop, f'tar -tf "{archive}"', workspace)["auto_confirm"] is True
    cmd = f'tar -xf "{archive}" -C "{target}"'
    good = evaluate(loop, cmd, workspace)
    assert good["auto_confirm"] is True, good
    assert good["reason_code"] == "TURBO_TAR_EXTRACT_EMPTY_DESTINATION"
    (target / "already.txt").write_text("do not overwrite")
    denied = evaluate(loop, cmd, workspace)
    assert denied["auto_confirm"] is False
    assert denied["reason_code"] == "TURBO_TAR_DESTINATION_NOT_EMPTY_OR_MISSING"


@pytest.mark.parametrize("bad", ["traversal", "symlink"])
def test_tar_members_cannot_escape_or_be_links(env, bad):
    workspace, turbo, blocked, loop = env
    workspace_access.set_turbo_root(turbo, workspace=workspace)
    archive = turbo / "source.tar"
    target = turbo / "deployment"
    target.mkdir()
    make_tar(archive, malicious=bad == "traversal", symlink=bad == "symlink")
    got = evaluate(loop, f'tar -xf "{archive}" -C "{target}"', workspace)
    assert got["auto_confirm"] is False
    assert got["reason_code"] == "TURBO_TAR_ARCHIVE_UNSAFE_OR_UNVERIFIED"


@pytest.mark.parametrize("command", [
    "git reset --hard", "git checkout HEAD", "git clean -fd",
    "git ls-tree HEAD; Remove-Item target",
    "git ls-tree HEAD | tar -xf -",
    "python script.py", "powershell -Command 'echo hi'",
    "cmd /c del victim",
    "git ls-tree HEAD $(whoami)",
    "git show --output=secret.txt HEAD",
    "git diff --ext-diff",
    "git show --format=%s HEAD",
])
def test_unproven_or_destructive_commands_never_auto_confirm(env, command):
    workspace, turbo, blocked, loop = env
    workspace_access.set_turbo_root(turbo, workspace=workspace)
    assert evaluate(loop, command, turbo)["auto_confirm"] is False


def test_git_hash_verification_is_safe_read_not_mutation():
    from agent_core.action_dependencies import command_is_proven_observation
    path = r"E:\\Workspace\\project\\input.csv"
    for cmd in [
        f"certutil -hashfile '{path}' SHA256",
        f'certutil -hashfile "{path}" SHA256',
        f"git hash-object --no-filters -- '{path}'",
        f'git hash-object --no-filters -- "{path}"',
        f"git -C 'E:\\Workspace\\project' hash-object --no-filters -- '{path}'",
    ]:
        assert command_is_proven_observation({
            "tool": "run_command", "operation": "INSPECT",
            "command": cmd,
        }), cmd
    for bad in [
        f"git hash-object -w -- '{path}'",
        'certutil -hashfile "$(whoami)" SHA256',
        f"certutil -hashfile '{path}' SHA256; Remove-Item '{path}'",
    ]:
        assert not command_is_proven_observation({
            "tool": "run_command", "operation": "INSPECT", "command": bad,
        })


def test_runtime_status_diagnostic_only_handles_real_status_mismatch():
    ref = "MIN-EV-1234567890AB"
    diag = diagnose_finish_status_mismatch({
        "reason_code": "MODEL_RUNTIME_MISMATCH",
        "mismatches": [{
            "evidence_ref": ref, "field": "status",
            "expected": "FAILED", "actual": "REJECTED",
        }],
    })
    assert diag["schema"] == "SMARTAGENT_FINISH_STATUS_REPAIR_V1"
    assert diag["corrections"][0]["runtime_actual_status"] == "REJECTED"
    assert not diag["authorization_granted"]
    assert diag["evidence_unmodified"] is True
    assert diagnose_finish_status_mismatch({
        "reason_code": "MODEL_RUNTIME_MISMATCH",
        "mismatches": [{
            "evidence_ref": ref, "field": "data.output",
            "expected": "secret", "actual": "other",
        }],
    }) is None


def test_finish_status_repair_is_nonexecuting_and_keeps_rejected(tmp_path, monkeypatch):
    request = "根據實際觀測值確認本次檢查狀態，回報證據"
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    loop.max_turns = 3
    called = []
    def execute(action):
        called.append(dict(action))
        if len(called) == 1:
            return encode_result(
                "REJECTED", "PRECONDITION_NOT_SATISFIED",
                tool="inspect_directory", executed=False,
                requested_path=TARGET,
            )
        return encode_result(
            "SUCCEEDED", "DIRECTORY_INSPECTED",
            tool="inspect_directory", executed=True,
            requested_path=TARGET,
            data={"observed_exists": True, "observed_is_directory": True,
                  "observation_status": "CONFIRMED"},
        )
    loop._execute_action = execute

    def planner(prompt, *_):
        loop.prompts.append(prompt)
        n = len(loop.prompts)
        if n <= 2:
            return tool_json("inspect_directory", paths=[TARGET], recursive=False)
        rows = json.loads(
            prompt.split("Runtime ToolResult Status Ledger ", 1)[1]
                  .split("):\n", 1)[1].split("\n", 1)[0]
        )
        assert len(rows) == 2
        assert [r["status"] for r in rows] == ["REJECTED", "SUCCEEDED"]
        if n == 4:
            assert "MODEL_RUNTIME_MISMATCH" in prompt
            assert "FINISH_STATUS_REPAIR" in prompt
            assert "runtime_actual_status" in prompt
        fields = [
            {"status": "FAILED" if n == 3 else "REJECTED"},
            {"status": "SUCCEEDED", "data.observed_exists": True},
        ]
        return json.dumps({
            "type": "finish", "summary": "資料夾已獨立驗證，首筆拒絕仍是 REJECTED",
            "expected_runtime": [{
                "evidence_ref": r["evidence_ref"], "fields": f,
            } for r, f in zip(rows, fields)],
            "goal_checks": [{
                "criterion": "目標存在",
                "evidence_ref": rows[1]["evidence_ref"],
                "field": "data.observed_exists", "operator": "eq", "expected": True,
            }],
        }, ensure_ascii=False)
    loop.planner = planner
    result = run_minimal_core(loop, request)
    assert len(called) == 2
    assert len(loop.prompts) == 4
    board = read_executor_run(tmp_path, task_id=loop.task_id)
    repair = [ev for ev in board["events"] if ev["kind"] == "MODEL_FINISH_STATUS_REPAIR"]
    assert len(repair) == 1
    assert repair[0]["finish_diagnostic"]["corrections"][0]["runtime_actual_status"] == "REJECTED"
    assert board["terminal"]["finish_comparison"]["verdict"] == "PASS"
    assert loop.terminal_outcome == "SUCCESS"



def test_executor_records_turbo_decision_and_evidence_link(env, monkeypatch):
    from WebAgent.tests.test_minimal_core_v10 import FakeLoop
    workspace, turbo, blocked, owner = env
    workspace_access.set_turbo_root(turbo, workspace=workspace)
    request = f"Inspect existing Git repository at {turbo}"
    replies = [
        tool_json(
            "run_command", command="git ls-tree HEAD",
            operation="GIT_INSPECT", working_directory=str(turbo),
            success_criteria="git tree inspection completed",
            verify=[{"action": "run_command", "command": "git status --short",
                     "expect_exit_code": 0}],
        ),
        '{"type":"blocked","reason":"test finished, no claimed completion"}',
    ]
    loop = FakeLoop(workspace, request, replies, "")
    loop.executor_only_mode = True
    loop.max_turns = 2
    executed = []
    def execute(action):
        executed.append(action)
        return encode_result(
            "SUCCEEDED", "COMMAND_EXITED", tool="run_command",
            executed=True, data={"process_state": "EXITED",
                                 "exit_code": 0, "timed_out": False},
        )
    loop._execute_action = execute
    run_minimal_core(loop, request)
    board = read_executor_run(workspace, task_id=loop.task_id)
    decisions = [x for x in board["events"] if x.get("kind") == "TURBO_DECISION"]
    confirmed = [x for x in board["events"] if x.get("kind") == "TURBO_AUTO_ADMITTED"]
    results = [x for x in board["events"] if x.get("kind") == "TOOL_RESULT"]
    assert len(executed) == len(decisions) == len(confirmed) == len(results) == 1
    assert decisions[0]["risk_class"] == "UNKNOWN"
    assert decisions[0]["auto_confirm"] is True
    assert decisions[0]["reason_code"] == "TURBO_KNOWN_NONDESTRUCTIVE_SINGLE_COMMAND"
    assert results[0]["runtimeEvidence"]["turbo_auto_confirm"] is True
    assert results[0]["runtimeEvidence"]["evidence_ref"].startswith("MIN-EV-")
    assert not any(x.get("kind") == "HUMAN_APPROVAL_REQUESTED" for x in board["events"])


def test_collector_exports_privacy_safe_turbo_and_finish_diagnostics():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[3] / "collect.bat").read_text(
        encoding="utf-8", errors="replace",
    )
    assert "executor_turbo_decisions.json" in source
    assert "MODEL_FINISH_STATUS_REPAIR" in source
    assert "status_corrections" in source
    assert "workspace_root_sha256" in source
    assert "turbo_root_sha256" in source
    assert "raw_command_included=$false" in source
    assert "raw_path_included=$false" in source
