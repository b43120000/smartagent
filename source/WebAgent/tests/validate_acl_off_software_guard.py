#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from agent_core.bounded_process import run_bounded_process
from agent_core.command_security import CommandSecurityError, require_command_allowed
from agent_core.security_preflight import preflight
from agent_core import windows_security


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
