"""Git50: Planner sees native-broker availability without false OS READY.

These are advisory tests. Native OS token and filesystem denial evidence is
proven separately by Windows AppContainer integration.
"""
from __future__ import annotations

from agent_core import turbo_sandbox, turbo_execution_policy, turbo_broker_admission


def test_broker_enrollment_hint_is_not_runtime_authorization(tmp_path, monkeypatch):
    ws = tmp_path / "ws"; root = ws / "turbo"
    root.mkdir(parents=True)
    policy = {
        "revision": 51, "turbo_requested": True,
        "turbo_roots": [str(root)], "denied_write_roots": [],
    }
    access = {
        "policy": policy, "turbo_roots": (str(root),),
        "writable_workspaces": (str(ws),), "read_only_roots": (),
    }
    monkeypatch.setattr(turbo_sandbox, "acl_mode", lambda: "on")
    monkeypatch.setattr(turbo_sandbox, "access_snapshot", lambda: access)
    monkeypatch.setattr(turbo_execution_policy, "access_snapshot", lambda: access)
    monkeypatch.setattr(turbo_broker_admission, "assess", lambda *_args, **_kw: {
        "decision": "BROKER_REQUIRED",
    })
    hint = turbo_execution_policy.turbo_planning_snapshot(ws)
    assert hint["status"] == "BROKER_ENROLLED", hint
    assert hint["turbo_effective"] == "OFF"
    assert hint["authorization_granted"] is False
    assert hint["runtime_admission_still_required"] is True
    assert hint["per_action_os_attestation_required"] is True
    assert hint["native_broker_tools"] == [
        "run_command", "create_directory", "write_file"
    ]
    assert hint["configured_roots"] == [str(root)]


def test_broker_not_enrolled_stays_blocked(tmp_path, monkeypatch):
    ws = tmp_path / "ws"; root = ws / "turbo"
    root.mkdir(parents=True)
    access = {
        "policy": {
            "revision": 52, "turbo_requested": True,
            "turbo_roots": [str(root)], "denied_write_roots": [],
        },
        "turbo_roots": (str(root),),
        "writable_workspaces": (str(ws),), "read_only_roots": (),
    }
    monkeypatch.setattr(turbo_sandbox, "acl_mode", lambda: "on")
    monkeypatch.setattr(turbo_sandbox, "access_snapshot", lambda: access)
    monkeypatch.setattr(turbo_execution_policy, "access_snapshot", lambda: access)
    monkeypatch.setattr(turbo_broker_admission, "assess", lambda *_args, **_kw: {
        "decision": "BLOCKED",
    })
    hint = turbo_execution_policy.turbo_planning_snapshot(ws)
    assert hint["status"] == "BLOCKED"
    assert hint["native_broker_tools"] == []
    assert hint["authorization_granted"] is False
    assert hint["per_action_os_attestation_required"] is False
