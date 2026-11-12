"""Git32 regression: host PowerShell Broker child environment only.

Mock subprocess; no real OS AppContainer attestation or privilege claim.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from agent_core import turbo_os_broker_client as broker


@pytest.mark.parametrize("env_name", ["PSModulePath", "PSMODULEPATH", "psmodulepath"])
def test_broker_child_drops_inherited_module_path(monkeypatch, tmp_path, env_name):
    monkeypatch.setenv(env_name, r"C:\Program Files\PowerShell\Modules;C:\Codex\Modules")
    monkeypatch.setenv("SMARTAGENT_TEST_ENV_PRESERVE", "present")
    monkeypatch.setattr(broker, "_resolve_workspace_and_cwd",
                        lambda cwd: (tmp_path, tmp_path))
    monkeypatch.setattr(broker, "_load_activation",
                        lambda workspace: (
                            {"turbo_root": str(tmp_path)},
                            {"policy_revision": 17},
                            tmp_path / "verified-protected-broker.ps1",
                        ))
    monkeypatch.setattr(broker, "_install_root", lambda: tmp_path)
    observed = []

    def fake_run(args, **kwargs):
        observed.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout="",
                               stderr="PSSecurityException")
    monkeypatch.setattr(broker.subprocess, "run", fake_run)

    with pytest.raises(broker.TurboOSBrokerUnavailable,
                       match="POWERSHELL_SCRIPT_POLICY_BLOCKED"):
        broker.execute_turbo_command("Write-Output test", cwd=tmp_path,
                                     task_id="TASK-ENV",
                                     action_id="MIN-ENV")
    assert len(observed) == 1
    args, kwargs = observed[0]
    assert args[0].casefold().endswith(
        r"system32\windowspowershell\v1.0\powershell.exe"
    ) or args[0].endswith("System32/WindowsPowerShell/v1.0/powershell.exe")
    assert args[1:6] == [
        "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned", "-File"
    ]
    assert args[6] == str(tmp_path / "verified-protected-broker.ps1")
    assert kwargs["shell"] is False
    assert "SMARTAGENT_TEST_ENV_PRESERVE" in kwargs["env"]
    assert not any(key.casefold() == "psmodulepath" for key in kwargs["env"])
    # No global environment mutation: only the broker child is sanitized.
    import os
    assert os.environ[env_name]


@pytest.mark.parametrize("stderr", [
    "PSSecurityException: execution prohibited by policy",
    "File cannot be loaded because running scripts is disabled on this system",
    "因為這個系統上已停用指令碼執行",
])
def test_policy_denial_classification_is_bounded(stderr):
    assert broker._native_broker_rejection_reason(stderr) == (
        "TURBO_OS_BROKER_NATIVE_EXECUTOR_REJECTED:POWERSHELL_SCRIPT_POLICY_BLOCKED"
    )
