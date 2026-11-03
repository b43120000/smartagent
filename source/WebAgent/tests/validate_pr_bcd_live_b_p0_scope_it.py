"""PR-B/C/D Live B P0: request-owned Project Root and read/write isolation.

Run: PYTHONPATH=source python -B source/WebAgent/tests/validate_pr_bcd_live_b_p0_scope_it.py
Windows-only path lexical and actual runtime-security admission checks.
No wheel installation, network activity, user-project writes, or GUI needed.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.path_security import PathSecurityError
from agent_core.security_context import SecurityContext
from agent_core.task_workspace_resolver import resolve_task_workspace
from WebAgent.protocol_loop import (
    WebAgentProtocolLoop, extract_authorized_paths, resolve_authorized_paths,
)
from WebAgent.tool_context import WebAgentToolContext


def live_b_request(root: Path) -> str:
    return (
        f"在 {root} 執行 PR-B/C/D Live 驗證。\n"
        "依賴：smartagent-pr-bcd-live>=1.0\n"
        "離線 Wheel：\n"
        "wheelhouse\\smartagent_pr_bcd_live-1.0-py3-none-any.whl\n"
        "允許套件安裝目標：\n"
        f"{root / 'deps'}\n"
        "另允許必要的 Project Environment metadata 寫入：\n"
        f"{root / '.agents' / 'project_context' / 'environment.json'}\n"
        "禁止其他專案路徑寫入、網路下載、修改 SmartAgent 共用 Python 環境。\n"
        "必須依序 INSPECT → MUTATE → 獨立 VERIFY → Resume。"
    )


class EmptyHistory:
    def process_lock(self):
        from contextlib import nullcontext
        return nullcontext()
    def load(self): return ()
    def all(self): return ()


class LiveBP0ScopeIT(unittest.TestCase):
    def test_complete_original_request_does_not_swallow_task_prose(self):
        with tempfile.TemporaryDirectory(prefix="sa-live-b-p0-") as temp:
            root = Path(temp) / "SmartAgent_PR_BCD_LIVE"
            (root / "deps").mkdir(parents=True)
            (root / "wheelhouse").mkdir()
            request = live_b_request(root)
            extracted = extract_authorized_paths(request)
            self.assertEqual(extracted, [
                str(root), str(root / "deps"),
                str(root / ".agents" / "project_context" / "environment.json")
            ])
            self.assertEqual(resolve_authorized_paths(request), extracted)
            self.assertEqual(WebAgentProtocolLoop._authorized_project_root(
                extracted, Path(temp)), root.resolve())

    def test_scope_is_readable_but_request_text_never_grants_write(self):
        with tempfile.TemporaryDirectory(prefix="sa-live-b-p0-") as temp:
            base = Path(temp)
            default = base / "default"
            default.mkdir()
            project = base / "SmartAgent_PR_BCD_LIVE"
            wheelhouse = project / "wheelhouse"
            deps = project / "deps"
            wheelhouse.mkdir(parents=True)
            deps.mkdir()
            other = base / "outside"
            other.mkdir()
            request = live_b_request(project)
            paths = extract_authorized_paths(request)
            tool = WebAgentToolContext(default)
            tool.begin_run("RR-LIVE-B-P0", request, paths)
            # Explicit request paths grant read visibility only; the global
            # writable policy remains independent from the user request.
            with patch("agent_core.security_context._configured_access_roots",
                       return_value=((default.resolve(),), ())):
                for folder in (project, wheelhouse, deps):
                    scope = tool.preview_tool_scope({
                        "tool": "list_directory", "path": str(folder),
                        "action_id": "TEST-LIST"
                    })
                    self.assertTrue(scope["allowed"], scope)
                    self.assertEqual(scope["resolved_paths"], [str(folder.resolve())])
                grouped = tool.preview_tool_scope({
                    "tool": "inspect_directory", "paths": [
                        str(project), str(wheelhouse), str(deps)
                    ], "action_id": "TEST-INSPECT"
                })
                self.assertTrue(grouped["allowed"], grouped)
                disallowed = tool.preview_tool_scope({
                    "tool": "list_directory", "path": str(other),
                    "action_id": "TEST-OUTSIDE"
                })
                self.assertFalse(disallowed["allowed"], disallowed)
                security = SecurityContext.from_agent(tool)
                self.assertEqual(security.require_read(project), project.resolve())
                with self.assertRaises(PathSecurityError):
                    security.require_write(project / "unapproved.txt")
                with self.assertRaises(PathSecurityError):
                    security.require_write(deps / "not-granted-by-request.whl")
                self.assertEqual(security.require_write(default / "allowed.txt"),
                                 (default / "allowed.txt").resolve())
                # Shell cwd still requires *separate* writable policy.
                cmd_scope = tool.preview_tool_scope({
                    "tool": "run_command", "operation": "INSPECT",
                    "command": "Get-Location",
                    "working_directory": str(project)
                })
                self.assertFalse(cmd_scope["allowed"], cmd_scope)

    def test_write_ceiling_denies_all_unverified_writes_without_execution(self):
        from agent_core.pr_bcd_request_scope import (
            parse_bounded_project_writes, check_action_against_write_ceiling,
        )
        from agent_core.tool_result_contract import decode_result
        with tempfile.TemporaryDirectory(prefix="sa-live-b-p0-") as temp:
            base = Path(temp)
            project = base / "SmartAgent_PR_BCD_LIVE"
            (project / "wheelhouse").mkdir(parents=True)
            (project / "deps").mkdir()
            (project / ".agents" / "project_context").mkdir(parents=True)
            default = base / "default"
            default.mkdir()
            request = live_b_request(project)
            explicit = extract_authorized_paths(request)
            ceiling = parse_bounded_project_writes(
                request, explicit_paths=explicit, project_root=project,
            )
            self.assertIsNotNone(ceiling)
            self.assertTrue(ceiling["valid"], ceiling)
            self.assertFalse(ceiling["grants_authorization"])
            tool = WebAgentToolContext(default)
            tool.begin_run("RR-P0-WRITE-SCOPE", request, explicit)
            tool._request_write_ceiling = ceiling
            scope = tool.preview_tool_scope({
                "tool": "list_directory", "path": str(project)
            })
            self.assertTrue(scope["allowed"], scope)
            rejected = [
                {"tool": "write_file", "path": str(project / "unapproved.txt"),
                 "content": "SHOULD_NOT_WRITE"},
                {"tool": "write_file", "path": str(project / "deps" / "some.txt"),
                 "content": "SHOULD_NOT_WRITE"},
                {"tool": "write_file", "path": str(project / ".agents" /
                  "project_context" / "environment.json"), "content": "{}"},
                {"tool": "run_command", "operation": "MUTATE",
                 "command": "python -m pip install --no-index --target deps example.whl",
                 "working_directory": str(project)},
                {"tool": "run_command", "operation": "INSPECT",
                 "command": "Set-Content -Path 'unapproved.txt' -Value dangerous",
                 "working_directory": str(project)},
                {"tool": "delete_path", "path": str(project / "deps")},
            ]
            for action in rejected:
                with self.subTest(action=action):
                    self.assertFalse(check_action_against_write_ceiling(
                        action, ceiling)[0])
                    self.assertFalse(tool.preview_tool_scope(action)["allowed"])
                    result = decode_result(tool.execute({
                        **action, "action_id": "P0-DENIED",
                    }))
                    self.assertIsNotNone(result)
                    self.assertEqual(result["status"], "REJECTED")
                    self.assertIs(result["executed"], False)
            self.assertEqual([p for p in project.rglob("*") if p.is_file()], [])

            # Restrictions must not leak into a new request.
            tool.begin_run("RR-P0-NEXT", "Verify a separate ordinary task.", [])
            self.assertIsNone(tool._request_write_ceiling)

    def test_unparseable_write_ceiling_fails_closed_for_mutation(self):
        from agent_core.pr_bcd_request_scope import (
            parse_bounded_project_writes, check_action_against_write_ceiling,
        )
        with tempfile.TemporaryDirectory(prefix="sa-live-b-p0-") as temp:
            root = Path(temp) / "SmartAgent_PR_BCD_LIVE"
            root.mkdir()
            malformed = (
                f"在 {root} 執行 PR-B/C/D。"
                "禁止其他專案路徑寫入。未提供必要的寫入目標。"
            )
            ceiling = parse_bounded_project_writes(
                malformed, explicit_paths=extract_authorized_paths(malformed),
                project_root=root,
            )
            self.assertFalse(ceiling["valid"])
            self.assertFalse(check_action_against_write_ceiling({
                "tool": "run_command", "operation": "MUTATE",
                "command": "python -m pip install example.whl",
            }, ceiling)[0])
            self.assertTrue(check_action_against_write_ceiling({
                "tool": "list_directory", "path": str(root),
            }, ceiling)[0])

    def test_registered_project_workspace_is_request_first_not_default(self):
        with tempfile.TemporaryDirectory(prefix="sa-live-b-p0-") as temp:
            base = Path(temp)
            default = base / "default"
            default.mkdir()
            project = base / "SmartAgent_PR_BCD_LIVE"
            (project / "deps").mkdir(parents=True)
            request = live_b_request(project)
            registry = {"writable_workspaces": (str(default), str(project)),
                        "read_only_roots": ()}
            with patch("agent_core.task_workspace_resolver.access_snapshot",
                       return_value=registry), \
                 patch("agent_core.workspace_access.access_snapshot",
                       return_value=registry):
                selected = resolve_task_workspace(
                    request, conversation_key="telegram://chat/1",
                    transport="TELEGRAM", sender_id="tester", store=EmptyHistory(),
                )
            self.assertEqual(Path(selected["workspace"]), project.resolve())
            self.assertEqual(selected["source"], "CURRENT_REQUEST")

    def test_quoted_spaces_and_adjacent_paths_preserved(self):
        with tempfile.TemporaryDirectory(prefix="sa-live-b-p0-") as temp:
            base = Path(temp)
            root = base / "SmartAgent PR BCD LIVE"
            root.mkdir()
            quoted = f'在 "{root}" 執行 PR-B/C/D 檢查。'
            self.assertEqual(extract_authorized_paths(quoted), [str(root)])
            mixed = f"先讀取 {root / 'deps'}，再讀取 {base / 'other'}。"
            self.assertEqual(extract_authorized_paths(mixed), [
                str(root / "deps"), str(base / "other")
            ])
            # Explicit ADS-looking paths must not be truncated into
            # unauthorized innocent-looking files.
            ads = str(root / "environment.json") + ":hidden"
            self.assertIn(":hidden", extract_authorized_paths("讀取 " + ads)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
