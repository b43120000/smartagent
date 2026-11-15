"""Git62 non-Windows contract tests: selection is never authority."""
from __future__ import annotations

from pathlib import Path
import pytest

from agent_core import turbo_root_registry as routes
from agent_core import turbo_os_broker_client as broker
from agent_core import turbo_sandbox


@pytest.fixture
def installed(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    a = ws / "alpha"
    b = ws / "beta"
    for directory in (a, b):
        (directory / "nested").mkdir(parents=True)
    policy = {
        "revision": 29, "turbo_requested": True,
        "writable_workspaces": [str(ws)],
        "read_only_roots": [], "denied_write_roots": [],
        "turbo_roots": [str(a), str(b)],
    }
    monkeypatch.setattr(broker, "access_snapshot", lambda: {
        "policy": policy, "turbo_roots": tuple(policy["turbo_roots"]),
        "writable_workspaces": tuple(policy["writable_workspaces"]),
    })
    monkeypatch.setattr(turbo_sandbox, "access_snapshot", lambda: {
        "policy": policy, "turbo_roots": tuple(policy["turbo_roots"]),
        "writable_workspaces": tuple(policy["writable_workspaces"]),
        "read_only_roots": (),
    })
    monkeypatch.setattr(turbo_sandbox, "acl_mode", lambda: "on")
    return ws, a, b, policy


def test_tasks_inside_two_roots_route_to_distinct_boundaries(installed):
    ws, a, b, policy = installed
    assert routes.resolve_root(a / "nested", policy) == a
    assert routes.resolve_root(b / "nested", policy) == b
    assert broker._resolve_workspace_and_cwd(a / "nested") == (ws, a / "nested")
    assert broker._resolve_workspace_and_cwd(b / "nested") == (ws, b / "nested")
    assert routes.canonical_key(a) != routes.canonical_key(b)
    secure = ws / "secure"
    assert routes.journal_path(secure, a) != routes.journal_path(secure, b)


def test_no_cwd_does_not_choose_first_root(installed):
    ws, a, b, policy = installed
    with pytest.raises(routes.TurboRootRoutingError, match="TURBO_MULTI_ROOT_NOT_BOUND"):
        routes.resolve_root(ws, policy)
    with pytest.raises(broker.TurboOSBrokerUnavailable, match="CWD_REQUIRED"):
        broker._resolve_workspace_and_cwd(None)


def test_other_workspace_and_unregistered_paths_fail_closed(installed, tmp_path):
    ws, a, b, policy = installed
    outsider = tmp_path / "external"
    outsider.mkdir()
    with pytest.raises(routes.TurboRootRoutingError, match="NOT_BOUND"):
        routes.resolve_root(outsider, policy)
    with pytest.raises(broker.TurboOSBrokerUnavailable):
        broker._resolve_workspace_and_cwd(outsider)


def test_nested_roots_rejected_before_policy_commit(installed):
    ws, a, b, policy = installed
    with pytest.raises(ValueError, match="turbo_roots_overlap"):
        routes.validate_root_set([str(a), str(a / "nested")])
    policy["turbo_roots"].append(str(a / "nested"))
    with pytest.raises(routes.TurboRootRoutingError, match="TURBO_MULTI_ROOT_AMBIGUOUS|TURBO_MULTI_ROOT_OVERLAP"):
        routes.resolve_root(a / "nested", policy)


def test_link_not_used_to_route_into_another_root(installed):
    ws, a, b, policy = installed
    linked = a / "shortcut"
    try:
        linked.symlink_to(b, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    with pytest.raises(routes.TurboRootRoutingError, match="NOT_BOUND"):
        routes.resolve_root(linked, policy)


def test_runtime_state_selects_target_root_not_first(installed):
    ws, a, b, policy = installed
    for chosen in (a / "nested", b / "nested"):
        state = turbo_sandbox.resolve_turbo_state(chosen)
        assert state["scope_verdict"] == "TURBO_SCOPE_ALLOWED"
        assert Path(state["turbo_root"]) == chosen.parent
        assert state["turbo_effective"] == "OFF"
        assert state["sandbox_attestation_passed"] is False
    state = turbo_sandbox.resolve_turbo_state(ws)
    assert state["scope_verdict"] == "TURBO_SCOPE_DENIED"
    assert state["reason_code"] == "TURBO_MULTI_ROOT_CWD_REQUIRED"


def test_single_root_workspace_legacy_fallback(installed):
    ws, a, b, policy = installed
    policy["turbo_roots"] = [str(a)]
    policy["turbo_requested"] = True
    assert broker._resolve_workspace_and_cwd(ws) == (ws, a)
    state = turbo_sandbox.resolve_turbo_state(ws)
    assert state["turbo_root"] == str(a)



def test_runtime_run_command_uses_exact_multi_root_cwd_not_workspace_parent(installed, monkeypatch):
    """Regression: Task CWD is valid but bounded_process silently replaced it
    with the shared writable parent, triggering TURBO_MULTI_ROOT_CWD_REQUIRED.
    This is a routing test with a FAKE broker; Windows CI separately tests
    real kernel token/SID isolation and negative access enforcement.
    """
    from agent_core import bounded_process, tools, workspace_access

    ws, a, b, policy = installed
    attempted = []

    # The protected policy legitimately resolves BOTH Turbo roots to the same
    # broad writable Workspace. This parent must never select a Turbo SID.
    monkeypatch.setattr(
        workspace_access, "resolve_turbo_authorization_workspace",
        lambda path: ws,
    )
    monkeypatch.setattr(
        broker, "active_broker_available",
        lambda path: str(Path(path)) in {str(a), str(b), str(a / "nested"), str(b / "nested")},
    )

    def fake_native(command, *, cwd, **kwargs):
        attempted.append((Path(cwd), str(command), kwargs))
        return {"exit_code": 0, "stdout": "MOCKED_NATIVE_ROUTING_ONLY",
                "stderr": "", "sandbox_evidence": {"executed": True}}

    monkeypatch.setattr(broker, "execute_turbo_command", fake_native)

    def never_host(*args, **kwargs):
        raise AssertionError("ordinary host process fallback is forbidden")

    monkeypatch.setattr(bounded_process.subprocess, "Popen", never_host)

    for target in (a, b):
        # Exercise the actual tools -> bounded_process path used by
        # Runtime run_command, not merely the low-level root resolver.
        result = tools._run_powershell_capture(
            "(Get-Location).Path",
            timeout=10,
            cwd=target,
            telemetry={"task_id": "TASK-GIT62-ROUTING",
                       "action_id": "ACT-GIT62-ROUTING"},
        )
        assert result["exit_code"] == 0
        assert result["stdout"] == "MOCKED_NATIVE_ROUTING_ONLY"

    assert [item[0] for item in attempted] == [a, b]
    assert all(item[2]["task_id"] == "TASK-GIT62-ROUTING" for item in attempted)


def test_runtime_multi_root_parent_and_missing_cwd_block_without_host_fallback(
    installed, monkeypatch,
):
    """Fail closed for ambiguous Workspace even if a native root exists."""
    from agent_core import bounded_process, workspace_access

    ws, a, b, policy = installed
    monkeypatch.setattr(
        workspace_access, "resolve_turbo_authorization_workspace",
        lambda path: ws if path is not None else None,
    )
    monkeypatch.setattr(
        broker, "active_broker_available", lambda path: True,
    )

    def no_native(*args, **kwargs):
        raise AssertionError("ambiguous CWD must never launch Native AppContainer")

    def no_host(*args, **kwargs):
        raise AssertionError("ambiguous CWD must never fall back to the host")

    monkeypatch.setattr(broker, "execute_turbo_command", no_native)
    monkeypatch.setattr(bounded_process.subprocess, "Popen", no_host)

    for target in (ws, None):
        result = bounded_process.run_bounded_process(
            ["powershell", "-NoProfile", "-Command", "(Get-Location).Path"],
            cwd=target, timeout=10, security_command="(Get-Location).Path",
        )
        assert result["security_rejected"] is True
        assert result["process_started"] is False
        assert result["exit_code"] is None
        assert result["error"].startswith("TURBO_OS_BROKER_BLOCKED:")
        assert result["sandbox_evidence"].get("executed") is False



def test_root_epoch_legacy_migration_add_preserves_unchanged_roots(installed):
    from agent_core.workspace_access import _retained_epochs, turbo_root_epoch

    ws, a, b, legacy = installed
    c = ws / "gamma"
    c.mkdir()
    epochs = _retained_epochs(legacy, [str(a), str(b), str(c)], 30)
    assert epochs == [
        {"root": str(a), "epoch": 29},
        {"root": str(b), "epoch": 29},
        {"root": str(c), "epoch": 30},
    ]
    policy = {**legacy, "revision": 30, "turbo_root_epochs": epochs,
              "turbo_roots": [str(a), str(b), str(c)]}
    assert turbo_root_epoch(policy, a) == 29
    assert turbo_root_epoch(policy, b) == 29
    assert turbo_root_epoch(policy, c) == 30
    # Remove A, then add it back: its previous epoch must NOT be reusable.
    without_a = {**policy, "revision": 31,
                 "turbo_root_epochs": _retained_epochs(policy, [str(b), str(c)], 31),
                 "turbo_roots": [str(b), str(c)]}
    readded = _retained_epochs(without_a, [str(b), str(c), str(a)], 32)
    assert readded[-1] == {"root": str(a), "epoch": 32}
    assert readded[:2] == without_a["turbo_root_epochs"]


def test_root_epochs_reject_partial_or_duplicate_map(installed):
    from agent_core.workspace_access import turbo_root_epoch

    ws, a, b, policy = installed
    missing = {**policy, "turbo_root_epochs": [{"root": str(b), "epoch": 29}]}
    with pytest.raises(ValueError, match="turbo_root_epoch_untrusted"):
        turbo_root_epoch(missing, a)
    duplicate = {**policy, "turbo_root_epochs": [
        {"root": str(a), "epoch": 29}, {"root": str(a), "epoch": 28}]}
    with pytest.raises(ValueError, match="turbo_root_epoch_untrusted"):
        turbo_root_epoch(duplicate, a)
    invalid = {**policy, "turbo_root_epochs": [{"root": str(a), "epoch": "29"}]}
    with pytest.raises(ValueError, match="turbo_root_epoch_untrusted"):
        turbo_root_epoch(invalid, a)
