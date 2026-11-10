#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.bounded_process import run_bounded_process
from agent_core.command_security import CommandSecurityError, require_command_allowed
from agent_core.security_preflight import preflight
from agent_core import restricted_executor_service, windows_security


def _write_mode(profile: Path, install_root: Path, mode: str) -> None:
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text("{}", encoding="utf-8")
    profile.with_name("acl_mode.json").write_text(
        json.dumps(
            {
                "schema": "SMARTAGENT_ACL_MODE_V1",
                "mode": mode,
                "install_root": str(install_root),
            }
        ),
        encoding="utf-8",
    )


def _verify_candidate_service(install_root: Path) -> None:
    endpoint = install_root / "queue"
    requests = endpoint / "requests"
    requests.mkdir(parents=True)
    request = requests / "EXEC-fixture.json"
    request.write_text("{}", encoding="utf-8")
    profile = {"executor_user": "fixture", "executor_endpoint": str(endpoint)}
    service = restricted_executor_service
    with (
        patch.object(service, "load_security_profile", return_value=profile),
        patch.object(service, "is_admin", return_value=False),
        patch.object(service.getpass, "getuser", return_value="fixture"),
        patch.object(service, "ensure_current_process_kill_job", return_value=SimpleNamespace(active=True)),
        patch.object(service, "acl_mode", return_value="transitioning_on"),
        patch.object(service, "authorized_workspace_roots", return_value=(install_root,)),
        patch.object(service, "workspace_traverse_roots", return_value=()),
        patch.object(service, "_probe_write", return_value=(True, "")) as probe,
        patch.object(service, "_probe_file_write", return_value=(True, "")),
        patch.object(service, "_run") as run,
    ):
        assert service.serve(once=True) == 0
        probe.assert_called_once_with(install_root, expect_allowed=True)
        run.assert_not_called()
    state = json.loads((endpoint / "service_state.json").read_text(encoding="utf-8"))
    assert state["self_test"]["mode"] == "on" and state["self_test"]["passed"], state
    assert request.is_file(), "The candidate executor consumed a request before ACL ON"


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        install_root = Path(temporary).resolve()
        profile = (
            install_root
            / "localdata"
            / "secure"
            / "windows_security"
            / "security_profile.json"
        )
        _write_mode(profile, install_root, "off")

        previous = os.environ.get("SMARTAGENT_RESTRICTED_EXECUTOR_REQUIRED")
        previous_profile = os.environ.get("SMARTAGENT_SECURITY_PROFILE")
        os.environ["SMARTAGENT_RESTRICTED_EXECUTOR_REQUIRED"] = "1"
        os.environ["SMARTAGENT_SECURITY_PROFILE"] = str(profile)
        try:
            assert windows_security.restricted_executor_required(profile) is False

            _write_mode(profile, install_root, "transitioning_on")
            assert windows_security.restricted_executor_required(profile) is True
            assert windows_security.acl_mode(profile, allow_on_transition=True) == "transitioning_on"
            try:
                windows_security.acl_mode(profile)
            except RuntimeError as exc:
                assert str(exc) == "acl_mode_invalid", exc
            else:
                raise AssertionError("staged ACL mode exposed as a ready mode")
            _write_mode(profile, install_root, "off")


            # Git49 Broker admission now requires a well-formed shared Workspace
            # access policy even in Software Guard mode. Make the isolated
            # installation fixture realistic rather than skipping the gate.
            from agent_core import workspace_access
            previous_local_profile = workspace_access._local_security_profile_path
            workspace_access._local_security_profile_path = lambda: profile
            try:
                workspace_access.save_workspace_access_policy([install_root], [])
                assert workspace_access.access_snapshot()["policy"]["turbo_requested"] is False
            except BaseException:
                workspace_access._local_security_profile_path = previous_local_profile
                raise

            original_resolve = __import__(
                "agent_core.security_preflight", fromlist=["resolve_workspace"]
            ).resolve_workspace
            module = __import__("agent_core.security_preflight", fromlist=["preflight"])
            module.resolve_workspace = lambda interface, explicit="": install_root
            original_required = module.restricted_executor_required
            module.restricted_executor_required = lambda: windows_security.restricted_executor_required(profile)
            try:
                report = preflight("remote", str(install_root))
            finally:
                module.resolve_workspace = original_resolve
                module.restricted_executor_required = original_required
            assert report["status"] == "SOFTWARE_GUARD_ONLY", report
            assert report["restricted_executor_required"] is False, report

            try:
                require_command_allowed("diskpart")
            except CommandSecurityError as exc:
                assert exc.code == "diskpart_forbidden", exc
            else:
                raise AssertionError("software command guard was bypassed")

            direct = run_bounded_process(
                [sys.executable, "-c", "print('software-only-direct')"],
            )
            assert direct["exit_code"] == 0, direct
            assert "software-only-direct" in direct["stdout"], direct

            rejected = run_bounded_process("diskpart", shell=True)
            assert rejected.get("security_rejected") is True, rejected
            assert "diskpart_forbidden" in rejected.get("error", ""), rejected
        finally:
            if "previous_local_profile" in locals():
                workspace_access._local_security_profile_path = previous_local_profile
            if previous is None:
                os.environ.pop("SMARTAGENT_RESTRICTED_EXECUTOR_REQUIRED", None)
            else:
                os.environ["SMARTAGENT_RESTRICTED_EXECUTOR_REQUIRED"] = previous
            if previous_profile is None:
                os.environ.pop("SMARTAGENT_SECURITY_PROFILE", None)
            else:
                os.environ["SMARTAGENT_SECURITY_PROFILE"] = previous_profile

    print("ACL_OFF_SOFTWARE_GUARD_VALIDATION_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
