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
