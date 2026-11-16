"""Git65 Auto Approve regression: all 8 settings, hard gates, and Windows process IT."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.auto_approve_mode import (
    apply_auto_approve_control, auto_approve_snapshot, evaluate_auto_approve,
    parse_auto_approve_control, set_auto_approve_requested,
)


class AutoApproveRegression(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()

    def access(self, *, turbo=False, auto=False):
        return {
            "policy": {
                "revision": 65, "turbo_requested": turbo,
                "auto_approve_requested": auto,
                "denied_write_roots": [],
            },
            "writable_workspaces": (str(self.workspace),),
            "read_only_roots": (),
        }

    def policy_patches(self, *, acl, turbo=False, auto=False):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch(
            "agent_core.auto_approve_mode.access_snapshot",
            return_value=self.access(turbo=turbo, auto=auto),
        ))
        stack.enter_context(patch("agent_core.auto_approve_mode.acl_mode", return_value=acl))
        stack.enter_context(patch(
            "agent_core.turbo_sandbox.resolve_turbo_state",
            return_value={"turbo_effective": "ON" if acl == "on" and turbo else "OFF"},
        ))
        return stack

    def test_all_eight_mode_combinations(self):
        for acl in ("off", "on"):
            for turbo in (False, True):
                for auto in (False, True):
                    with self.subTest(acl=acl, turbo=turbo, auto=auto):
                        with self.policy_patches(acl=acl, turbo=turbo, auto=auto):
                            state = auto_approve_snapshot(self.workspace)
                            self.assertEqual(state["auto_approve_requested"], "ON" if auto else "OFF")
                            self.assertEqual(state["turbo_requested"], "ON" if turbo else "OFF")
                            self.assertEqual(
                                state["auto_approve_effective"],
                                "ON" if acl == "off" and auto else "OFF",
                            )
                            self.assertEqual(
                                state["turbo_effective"],
                                "ON" if acl == "on" and turbo else "OFF",
                            )
                            self.assertEqual(
                                state["executor_backend"],
                                "WINDOWS_EXECUTOR" if acl == "off" else
                                "NATIVE_APPCONTAINER_BROKER" if turbo else "RESTRICTED_EXECUTOR",
                            )
                            decision = evaluate_auto_approve(
                                self.workspace, "Write-Output GIT65_TEST", working_directory=self.workspace,
                            )
                            self.assertEqual(decision["approved"], acl == "off" and auto)

    def test_unknown_acl_and_missing_policy_fail_closed(self):
        with self.policy_patches(acl="unknown", auto=True):
            self.assertEqual(auto_approve_snapshot(self.workspace)["auto_approve_effective"], "OFF")
        with patch("agent_core.auto_approve_mode.access_snapshot", side_effect=RuntimeError("bad")):
            self.assertFalse(evaluate_auto_approve(self.workspace, "Write-Output x")["approved"])

    def test_outside_workspace_is_never_auto_approved(self):
        with self.policy_patches(acl="off", auto=True):
            self.assertFalse(evaluate_auto_approve(
                self.workspace, "Write-Output x", working_directory=self.outside,
            )["approved"])
            self.assertFalse(evaluate_auto_approve(
                self.outside, "Write-Output x", working_directory=self.outside,
            )["approved"])

    def test_hard_command_security_is_not_bypassed(self):
        with self.policy_patches(acl="off", auto=True):
            decision = evaluate_auto_approve(self.workspace, "icacls C:\\ /grant Everyone:F")
            self.assertFalse(decision["approved"])
            self.assertIn("HARD_DENIED", decision["reason_code"])
            self.assertFalse(evaluate_auto_approve(
                self.workspace, "git clean -fdx",
            )["approved"])

    def test_notifier_is_not_authority_and_status_control_is_explicit(self):
        self.assertEqual(parse_auto_approve_control("autoapprove=on"), "ON")
        self.assertEqual(parse_auto_approve_control("AUTO_APPROVE=off"), "OFF")
        self.assertEqual(parse_auto_approve_control("autoapprove=status"), "STATUS")
        self.assertIsNone(parse_auto_approve_control("please autoapprove=on"))
        self.assertIsNone(parse_auto_approve_control("autoapprove=yes"))
        with patch("agent_core.workspace_access.require_writable_workspace") as scoped, patch(
            "agent_core.auto_approve_mode.set_auto_approve_requested"
        ) as setter, patch(
            "agent_core.auto_approve_mode.auto_approve_snapshot",
            return_value={"auto_approve_requested": "ON"},
        ):
            self.assertEqual(
                apply_auto_approve_control("autoapprove=on", workspace=self.workspace)
                ["auto_approve_requested"], "ON",
            )
            scoped.assert_called_once()
            setter.assert_called_once_with(True)

    def test_tool_layer_eligible_external_executable_receives_manifest(self):
        from agent_core.tools import _execution_approval_manifest
        ctx = SimpleNamespace(
            workspace_root=self.workspace, request_id="REQ-GIT65", task_id="TASK-GIT65",
        )
        command = "external-build-tool --version"
        mock_admission = SimpleNamespace(allowed=False, approval_eligible=True)
        receipt = {
            "approved": True, "reason_code": "AUTO_APPROVE_SECONDARY_CONFIRMATION",
            "authorization_scope": str(self.workspace), "policy_revision": 65,
            "executor_backend": "WINDOWS_EXECUTOR", "command_sha256": "abc",
        }
        with patch("agent_core.tools._native_run_command_admitted", return_value=False), patch(
            "agent_core.tools.inspect_command", return_value=mock_admission
        ), patch(
            "agent_core.tools.build_command_approval_manifest",
            return_value={"schema": "COMMAND_ALLOW_ONCE_V1", "target": "build.exe"},
        ), patch(
            "agent_core.auto_approve_mode.evaluate_auto_approve", return_value=receipt,
        ):
            manifest = _execution_approval_manifest({"command": command}, None, ctx, self.workspace)
        self.assertEqual(manifest["_auto_approval"]["decision"], "AUTO_APPROVED")
        self.assertFalse(manifest["_auto_approval"]["executed"])

    def test_workspace_policy_preserves_auto_setting_during_other_updates(self):
        from agent_core import workspace_access
        with patch.object(workspace_access, "workspace_access_policy_path",
                          return_value=self.root / "policy.json"), patch.object(
            workspace_access, "load_workspace_access_policy",
            return_value={
                "revision": 2, "writable_workspaces": [str(self.workspace)],
                "read_only_roots": [], "denied_write_roots": [],
                "turbo_roots": [], "turbo_requested": False,
                "auto_approve_requested": True,
            },
        ):
            result = workspace_access.save_workspace_access_policy(
                [self.workspace], [], [],
            )
            self.assertTrue(result["auto_approve_requested"])

    def test_request_toggle_is_atomic_and_noop_is_idempotent(self):
        policy = {
            "revision": 6, "writable_workspaces": [str(self.workspace)],
            "read_only_roots": [], "denied_write_roots": [],
            "turbo_roots": [], "turbo_requested": False,
            "auto_approve_requested": False,
        }
        path = self.root / "workspace_access_policy.json"
        with patch("agent_core.auto_approve_mode.workspace_access_policy_path",
                   return_value=path), patch(
            "agent_core.auto_approve_mode.load_workspace_access_policy",
            return_value=policy,
        ):
            result = set_auto_approve_requested(True, expected_revision=6)
            self.assertTrue(result["auto_approve_requested"])
            self.assertEqual(result["revision"], 7)
            self.assertTrue(path.is_file())
            with self.assertRaises(RuntimeError):
                set_auto_approve_requested(True, expected_revision=99)


@unittest.skipUnless(os.name == "nt", "Windows CMD/BAT process integration test")
class WindowsProcessIntegration(unittest.TestCase):
    def test_real_cmd_bat_stdout_stderr_exit(self):
        from agent_core.bounded_process import run_bounded_process
        with tempfile.TemporaryDirectory() as folder:
            bat = Path(folder) / "git65_real_child.bat"
            bat.write_text("@echo off\r\necho GIT65_STDOUT\r\necho GIT65_STDERR 1>&2\r\nexit /b 17\r\n",
                           encoding="utf-8")
            with patch("agent_core.turbo_broker_admission.assess", return_value={
                "decision": "NORMAL", "reason_code": "TURBO_DISABLED", "evidence": {},
            }), patch("agent_core.workspace_access.resolve_turbo_authorization_workspace",
                      return_value=None):
                receipt = run_bounded_process(
                    ["cmd.exe", "/d", "/c", str(bat)], cwd=folder, timeout=30,
                )
            self.assertTrue(receipt.get("process_started"))
            self.assertEqual(receipt.get("exit_code"), 17)
            self.assertIn("GIT65_STDOUT", receipt.get("stdout") or "")
            self.assertIn("GIT65_STDERR", receipt.get("stderr") or "")


if __name__ == "__main__":
    unittest.main()
