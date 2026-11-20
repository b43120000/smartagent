"""Git68 hardening: independent Git source evidence, V2 and Blocked contracts.

No test runs a host command, accesses GitHub, deletes files or deploys fixture.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from agent_core.tool_result_contract import encode_result
from agent_core.runtime_finish_comparator import compare_runtime_finish
from WebAgent.github_stage_diagnostics import github_source_stage_evidence
from WebAgent.pokeclaw_executor_loop import (
    _blocking_runtime_evidence, run_pokeclaw_executor,
)


def _receipt(status="SUCCEEDED", stdout="", stderr="", exit_code=0):
    return {
        "tool": "run_command", "executed": True, "status": status,
        "data": {"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
    }


def test_remote_refs_do_not_prove_private_authentication_commit_or_blob():
    head = "61bbcc24080a7c5b106116a132663ede2844af53"
    output = head + "\tHEAD\n" + head + "\trefs/heads/main\n"
    result = github_source_stage_evidence(
        {"tool": "run_command",
         "command": "git ls-remote https://github.com/b43120000/test.git"},
        _receipt(stdout=output), "MIN-EV-" + "a" * 24,
    )
    assert result["stage_verified"]
    assert result["stage"] == "REMOTE_REFS"
    assert result["ref_count"] == 2
    assert result["dns_status"] == "RESOLVED_THIS_REQUEST"
    assert result["repo_read_status"] == "REMOTE_REFS_READ"
    assert result["credential_identity"] == "NOT_OBSERVED"
    assert result["pinned_commit_status"] == "NOT_VERIFIED"
    assert result["blob_identity_status"] == "NOT_VERIFIED"
    assert result["stdout_sha256"] == hashlib.sha256(output.strip().encode()).hexdigest()


@pytest.mark.parametrize(
    ("stderr", "status", "field"),
    [
        ("fatal: Could not resolve host: github.com", "DNS_RESOLUTION_FAILED", "dns_status"),
        ("fatal: Authentication failed for 'https://github.com/x/y.git'",
         "REPOSITORY_ACCESS_FAILED", "repo_read_status"),
        ("fatal: repository access unavailable", "COMMAND_FAILED_OTHER", "status"),
    ],
)
def test_remote_failure_stages_remain_distinct(stderr, status, field):
    result = github_source_stage_evidence(
        {"tool": "run_command", "command": "git ls-remote origin"},
        _receipt(status="FAILED", stderr=stderr, exit_code=128),
        "MIN-EV-" + "b" * 24,
    )
    assert result["status"] == status
    assert not result["stage_verified"]
    assert result["pinned_commit_status"] == "NOT_VERIFIED"
    assert result["blob_identity_status"] == "NOT_VERIFIED"
    if field != "status":
        assert result[field] == "FAILED"


@pytest.mark.parametrize(
    ("command", "output", "stage", "key", "value"),
    [
        ("git cat-file -t " + "a" * 40, "commit\n",
         "LOCAL_COMMIT_OBJECT", "pinned_commit_status", "LOCAL_OBJECT_ONLY"),
        ("git ls-tree " + "a" * 40 + " fixture.zip",
         "100644 blob " + "b" * 40 + "\tfixture.zip\n",
         "PINNED_TREE_BLOB", "blob_identity_status", "COMMIT_TREE_ENTRY_ONLY"),
        ("git hash-object fixture.zip", "c" * 40 + "\n",
         "LOCAL_BLOB_HASH", "blob_identity_status", "LOCAL_GIT_BLOB_ONLY"),
        ("Get-FileHash -LiteralPath 'E:\\AIspace\\fixture.zip' -Algorithm SHA256",
         "d" * 64 + "\n", "LOCAL_ARCHIVE_SHA256",
         "blob_identity_status", "LOCAL_ARCHIVE_SHA256_ONLY"),
    ],
)
def test_commit_and_blob_stages_are_only_local_evidence(command, output, stage, key, value):
    result = github_source_stage_evidence(
        {"tool": "run_command", "command": command},
        _receipt(stdout=output), "MIN-EV-" + "c" * 24,
    )
    assert result["stage"] == stage
    assert result["stage_verified"]
    assert result[key] == value
    assert result["repo_read_status"] == "NOT_OBSERVED"


def test_fetch_success_still_does_not_claim_pinned_commit():
    result = github_source_stage_evidence(
        {"tool": "run_command",
         "command": "git fetch origin " + "a" * 40},
        _receipt(), "MIN-EV-" + "d" * 24,
    )
    assert result["status"] == "FETCH_EXIT_ZERO_OBJECT_UNVERIFIED"
    assert result["pinned_commit_status"] == "NOT_VERIFIED"


def test_unexecuted_or_unrelated_action_cannot_prove_source():
    action = {"tool": "run_command", "command": "git ls-remote origin"}
    receipt = _receipt(stdout="a" * 40 + "\tHEAD\n")
    receipt["executed"] = False
    result = github_source_stage_evidence(action, receipt, "MIN-EV-" + "e" * 24)
    assert result["status"] == "NOT_EXECUTED"
    assert not result["stage_verified"]
    assert github_source_stage_evidence(
        {"tool": "observe_fact", "path": r"E:\AIspace"}, receipt, "MIN-EV-" + "f" * 24
    ) is None


def test_blocked_evidence_binds_failed_command_without_rewriting_tool_result():
    raw = encode_result(
        "FAILED", "COMMAND_EXIT_NONZERO", tool="run_command", executed=True,
        data={"exit_code": 128, "stderr": "Could not resolve host: github.com"},
    )
    source = SimpleNamespace(action_result_ledger={
        "A1": {
            "produced_round": 1, "result_status": "FAILED",
            "result_reason": "COMMAND_EXIT_NONZERO",
            "evidence_ref": "MIN-EV-" + "a" * 24,
            "tool": "run_command", "result": raw,
        }
    })
    before = dict(source.action_result_ledger["A1"])
    result = _blocking_runtime_evidence(source)
    assert result["status"] == "OBSERVED_RUNTIME_BLOCKER"
    assert result["exit_code"] == 128
    assert result["execution_status"] == "FAILED"
    assert result["evidence_ref"] == "MIN-EV-" + "a" * 24
    assert source.action_result_ledger["A1"] == before


def test_v2_comparator_accepts_multiline_stdout_without_planner_copy(tmp_path, monkeypatch):
    from WebAgent.tests.test_git68_finish_contract_adapter_v2 import _fixture

    loop, goal, board, contract, checks, refs = _fixture(monkeypatch, 1)
    output = (
        "61bbcc24080a7c5b106116a132663ede2844af53\tHEAD\n"
        "ae51a3fa7f6d0d5a1bca573293fc9a195e42ec5a\trefs/heads/feature\n"
        + ("x" * 7000)
    )
    raw = encode_result(
        "SUCCEEDED", "DIRECTORY_INSPECTED", tool="inspect_directory", executed=True,
        data={"observed_exists": True, "observation_status": "CONFIRMED", "stdout": output},
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()
    action_id = next(iter(loop.action_result_ledger))
    loop.action_result_ledger[action_id]["result"] = raw
    loop.action_result_ledger[action_id]["result_sha256"] = digest
    board["events"][0]["runtimeEvidence"]["result_sha256"] = digest
    result = compare_runtime_finish(
        loop, tmp_path, goal, None, goal_checks=checks,
        allow_open=True, canonical_finish=contract,
        answer=contract["planner_intent"]["summary"],
    )
    assert result["verdict"] == "PASS", result
    assert result["evidence_refs"] == refs
    assert result["mismatches"] == []


def test_tool_task_legacy_v1_is_rejected_then_v2_can_finish(tmp_path, monkeypatch):
    from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop
    from WebAgent.tests.test_minimal_core_v10 import tool_json
    from agent_core.executor_run_blackboard import read_executor_run

    request = "檢視執行紀錄，依實際程序退出碼確認是否符合本次檢查目標"
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.assess",
        lambda *a, **k: {"decision": "NORMAL", "reason_code": "TEST_ACL_OFF"},
    )
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.admit_action",
        lambda *a, **k: {"decision": "NORMAL", "reason_code": "TEST_ACL_OFF"},
    )
    actions = []
    def execute(action):
        actions.append(dict(action))
        return encode_result(
            "SUCCEEDED", "COMMAND_EXITED", tool="run_command", executed=True,
            data={"process_state": "EXITED", "exit_code": 0,
                  "stdout": "HEAD\trefs/heads/main\n", "stderr": "",
                  "timed_out": False, "verification_status": "PASS"},
        )
    loop._execute_action = execute

    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if len(loop.prompts) == 1:
            return tool_json("run_command", command="git status --short",
                             operation="GIT_INSPECT")
        if len(loop.prompts) == 2:
            return json.dumps({"type": "finish", "summary": "Legacy incomplete output",
                               "expected_runtime": []})
        recent = json.loads(
            prompt.split("Recent actual PokeClaw ToolResults:\n", 1)[1].split("\n", 1)[0]
        )
        return json.dumps({
            "type": "finish", "contract_version": 2,
            "summary": "exit_code is zero",
            "goal_checks": [{
                "criterion": "actual exited", "evidence_ref": recent[-1]["evidence_ref"],
                "field": "data.exit_code", "operator": "eq", "expected": 0,
            }],
        })
    loop.planner = planner
    run_pokeclaw_executor(loop, request)
    board = read_executor_run(
        tmp_path, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch,
    )
    events = [e["kind"] for e in board["events"]]
    assert "MODEL_FINISH_V2_REQUIRED" in events
    assert len(actions) == 1
    assert loop.terminal_outcome == "SUCCESS"
    assert loop.terminal_acceptance["final_verdict"] == "PASS"


def test_p0_blocked_does_not_trigger_delete_or_deployment(tmp_path, monkeypatch):
    from WebAgent.tests.test_git33_pokeclaw_executor_loop import _loop
    from WebAgent.tests.test_minimal_core_v10 import tool_json
    from agent_core.executor_run_blackboard import read_executor_run

    request = (
        "檢查遠端來源；若 GitHub 連線失敗，停止，不得清除、部署或覆寫任何檔案"
    )
    loop, _ = _loop(tmp_path, monkeypatch, [], request=request)
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.assess",
        lambda *a, **k: {"decision": "NORMAL", "reason_code": "TEST_ACL_OFF"},
    )
    monkeypatch.setattr(
        "agent_core.turbo_broker_admission.admit_action",
        lambda *a, **k: {"decision": "NORMAL", "reason_code": "TEST_ACL_OFF"},
    )
    actions = []
    def execute(action):
        actions.append(dict(action))
        return encode_result(
            "FAILED", "COMMAND_EXIT_NONZERO", tool="run_command", executed=True,
            data={"process_state": "EXITED", "exit_code": 128, "stdout": "",
                  "stderr": "fatal: Could not resolve host: github.com",
                  "timed_out": False, "verification_status": "FAIL"},
        )
    loop._execute_action = execute

    def planner(prompt, *_):
        loop.prompts.append(prompt)
        if len(loop.prompts) == 1:
            return tool_json("run_command", command="git status --short",
                             operation="GIT_INSPECT")
        return json.dumps({
            "type": "blocked", "reason": "P0 failed: DNS lookup unavailable",
        })
    loop.planner = planner
    run_pokeclaw_executor(loop, request)
    board = read_executor_run(
        tmp_path, task_id=loop.task_id, request_id=loop.run_id,
        task_epoch=loop.task_epoch,
    )
    assert len(actions) == 1
    assert all(x["tool"] == "run_command" for x in actions)
    assert loop.terminal_outcome == "BLOCKED"
    assert not loop.terminal_acceptance
    assert loop.executor_result["goal_verified"] is False
    assert loop.executor_result["blocked_runtime_evidence"]["exit_code"] == 128
    blocked_events = [e for e in board["events"] if e["kind"] == "MODEL_BLOCKED"]
    assert len(blocked_events) == 1
    assert blocked_events[0]["blocking_runtime_evidence"]["exit_code"] == 128


def test_git_mutate_recognizes_absolute_git_binary_without_readonly_bypass():
    from agent_core.command_operation import (
        _looks_like_git, operation_mismatch_detail,
    )
    from agent_core.action_dependencies import command_is_proven_observation

    sha = "6b0d680185e40ae654acef03d0eeb9074536fb8e"
    invocation = (
        r"& 'C:\smartagent\.venv\git\cmd\git.exe' fetch "
        "https://github.com/b43120000/test.git " + sha
    )
    assert _looks_like_git(invocation)
    assert operation_mismatch_detail({
        "operation": "GIT_MUTATE", "command": invocation,
    }) == ""
    assert not command_is_proven_observation({
        "operation": "GIT_INSPECT", "command": invocation,
    })
    assert not _looks_like_git("Write-Output 'C:\\tools\\git.exe fetch'")
    assert operation_mismatch_detail({
        "operation": "GIT_INSPECT",
        "command": "git fetch https://github.com/b43120000/test.git " + sha,
    }).find("INSPECT_contains_explicit_mutation") >= 0


def test_positive_readonly_git_merge_base_ancestry_grammar_is_exact():
    from agent_core.action_dependencies import command_is_proven_observation
    from agent_core.command_operation import operation_mismatch_detail

    target = "6b0d680185e40ae654acef03d0eeb9074536fb8e"
    head = "61bbcc24080a7c5b106116a132663ede2844af53"
    command = "git merge-base --is-ancestor " + target + " " + head
    assert command_is_proven_observation({
        "operation": "GIT_INSPECT", "command": command,
    })
    assert operation_mismatch_detail({
        "operation": "GIT_INSPECT", "command": command,
    }) == ""
    assert command_is_proven_observation({
        "operation": "GIT_INSPECT",
        "command": "git -C 'E:\\AIspace\\cloned_test' merge-base --is-ancestor "
                   + target + " " + head,
    })
    for invalid in [
        command + "; Remove-Item -Recurse E:\\AIspace",
        command + " --extra",
        "git fetch https://github.com/b43120000/test.git " + target,
        "git merge-base --is-ancestor " + target + " xyz",
    ]:
        assert not command_is_proven_observation({
            "operation": "GIT_INSPECT", "command": invalid,
        })


def test_remote_head_local_graph_ancestry_binds_two_real_runtime_evidence_refs():
    head = "61bbcc24080a7c5b106116a132663ede2844af53"
    pin = "6b0d680185e40ae654acef03d0eeb9074536fb8e"
    head_ref = "MIN-EV-" + "b" * 24
    ancestry_ref = "MIN-EV-" + "c" * 24
    remote = github_source_stage_evidence(
        {
            "tool": "run_command",
            "command": "git ls-remote https://github.com/b43120000/test.git HEAD",
        },
        _receipt(stdout=head + "\tHEAD\n"), head_ref,
    )
    assert remote["remote_head_sha"] == head
    assert remote["repository"] == "b43120000/test"
    check = github_source_stage_evidence(
        {
            "tool": "run_command",
            "command": "git merge-base --is-ancestor " + pin + " " + head,
        },
        _receipt(stdout=""), ancestry_ref, prior_stages=[remote],
    )
    assert check["stage_verified"]
    assert check["pinned_commit_status"] == "REMOTE_HEAD_ANCESTOR_PROVED"
    proof = check["source_provenance"]
    assert proof["repository"] == "b43120000/test"
    assert proof["pinned_commit"] == pin
    assert proof["remote_head_sha"] == head
    assert proof["remote_head_evidence_ref"] == head_ref
    assert proof["local_ancestry_evidence_ref"] == ancestry_ref
    assert not proof["credential_identity_verified"]


def test_remote_ancestry_cannot_use_unmatched_or_unverified_remote_head():
    pin = "6b0d680185e40ae654acef03d0eeb9074536fb8e"
    head = "61bbcc24080a7c5b106116a132663ede2844af53"
    wrong = "f" * 40
    source = github_source_stage_evidence(
        {"tool": "run_command",
         "command": "git ls-remote https://github.com/b43120000/test.git HEAD"},
        _receipt(stdout=wrong + "\tHEAD\n"), "MIN-EV-" + "d" * 24,
    )
    command = {"tool": "run_command",
               "command": "git merge-base --is-ancestor " + pin + " " + head}
    mismatch = github_source_stage_evidence(
        command, _receipt(stdout=""), "MIN-EV-" + "e" * 24,
        prior_stages=[source],
    )
    assert mismatch["pinned_commit_status"] == "LOCAL_ANCESTRY_ONLY"
    assert "source_provenance" not in mismatch
    failed = github_source_stage_evidence(
        command, _receipt(status="FAILED", stderr="", exit_code=1),
        "MIN-EV-" + "f" * 24, prior_stages=[source],
    )
    assert not failed["stage_verified"]
    assert "source_provenance" not in failed


def test_latest_admission_denial_is_not_reported_as_stale_partial_tool_result():
    source = SimpleNamespace(action_result_ledger={})
    events = [
        {
            "sequence": 8, "kind": "TOOL_RESULT",
            "toolName": "inspect_directory",
            "runtimeEvidence": {
                "status": "PARTIAL",
                "reason_code": "DIRECTORY_INSPECTION_INCOMPLETE",
                "evidence_ref": "MIN-EV-" + "7" * 24,
                "process": {},
            },
        },
        {
            "sequence": 14, "kind": "ADMISSION_REJECTED",
            "toolName": "run_command",
            "admission_diagnostic": {
                "stage": "COMMAND_CLASSIFICATION",
                "reason_code": "GIT_MUTATE_requires_git_command",
            },
            "runtimeEvidence": {
                "status": "REJECTED", "executed": False,
                "reason_code": "GIT_MUTATE_requires_git_command",
                "evidence_ref": "",
            },
        },
        {
            "sequence": 15, "kind": "ADMISSION_REJECTED",
            "toolName": "run_command",
            "admission_diagnostic": {
                "stage": "COMMAND_CLASSIFICATION",
                "reason_code": "command_not_proven_read_only",
            },
            "runtimeEvidence": {
                "status": "REJECTED", "executed": False,
                "reason_code": "command_not_proven_read_only",
                "evidence_ref": "",
            },
        },
    ]
    result = _blocking_runtime_evidence(source, {"events": events})
    assert result["status"] == "OBSERVED_ADMISSION_REJECTION"
    assert result["blackboard_sequence"] == 15
    assert result["reason_code"] == "command_not_proven_read_only"
    assert result["evidence_ref"] == ""
    assert result["executed"] is False
    assert result["authorization_granted"] is False
    assert result["exit_code"] is None


def test_portable_git_full_path_remote_head_and_ancestry_are_readonly():
    from agent_core.action_dependencies import command_is_proven_observation
    from agent_core.action_resolution_trace import exact_remote_git_read_repository
    from agent_core.command_operation import operation_mismatch_detail

    executable = r'& "C:\smartagent\.venv\git\cmd\git.exe"'
    remote_command = (
        executable + " ls-remote https://github.com/b43120000/test.git HEAD"
    )
    head = "61bbcc24080a7c5b106116a132663ede2844af53"
    target = "6b0d680185e40ae654acef03d0eeb9074536fb8e"
    ancestry_command = (
        executable + " merge-base --is-ancestor " + target + " " + head
    )
    assert exact_remote_git_read_repository(remote_command) == "b43120000/test"
    assert command_is_proven_observation({
        "operation": "GIT_INSPECT", "command": remote_command,
    })
    assert command_is_proven_observation({
        "operation": "GIT_INSPECT", "command": ancestry_command,
    })
    assert operation_mismatch_detail({
        "operation": "GIT_INSPECT", "command": ancestry_command,
    }) == ""
    remote = github_source_stage_evidence(
        {"tool": "run_command", "command": remote_command},
        _receipt(stdout=head + "\tHEAD\n"), "MIN-EV-" + "1" * 24,
    )
    assert remote["remote_head_sha"] == head
    ancestry = github_source_stage_evidence(
        {"tool": "run_command", "command": ancestry_command},
        _receipt(), "MIN-EV-" + "2" * 24, prior_stages=[remote],
    )
    assert ancestry["stage_verified"]
    assert ancestry["pinned_commit_status"] == "REMOTE_HEAD_ANCESTOR_PROVED"
    assert ancestry["source_provenance"]["pinned_commit"] == target
    assert ancestry["source_provenance"]["remote_head_evidence_ref"] == (
        "MIN-EV-" + "1" * 24
    )
    fetch = executable + " fetch https://github.com/b43120000/test.git " + target
    assert not command_is_proven_observation({
        "operation": "GIT_INSPECT", "command": fetch,
    })
    assert "INSPECT_contains_explicit_mutation" in operation_mismatch_detail({
        "operation": "GIT_INSPECT", "command": fetch,
    })
    assert operation_mismatch_detail({
        "operation": "GIT_MUTATE", "command": fetch,
    }) == ""


def test_portable_git_readonly_scope_does_not_accept_shell_or_interpolation():
    from agent_core.action_dependencies import command_is_proven_observation
    from agent_core.action_resolution_trace import exact_remote_git_read_repository

    head = "61bbcc24080a7c5b106116a132663ede2844af53"
    target = "6b0d680185e40ae654acef03d0eeb9074536fb8e"
    safe = (
        r'& "C:\smartagent\.venv\git\cmd\git.exe"'
        + " merge-base --is-ancestor " + target + " " + head
    )
    for command in [
        safe + " ; Remove-Item -Recurse E:\\AIspace",
        safe + " --also-run",
        safe.replace("git.exe", "python.exe"),
        safe.replace("git.exe", "$xgit.exe"),
        safe.replace("git.exe", "gi" + chr(96) + "t.exe"),
    ]:
        assert not command_is_proven_observation({
            "operation": "GIT_INSPECT", "command": command,
        })
    bad_remote = (
        r'& "C:\smartagent\.venv\git\cmd\git.exe"'
        + " ls-remote https://github.com/b43120000/test.git HEAD ; echo forged"
    )
    assert exact_remote_git_read_repository(bad_remote) == ""
