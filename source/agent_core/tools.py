#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared deterministic tool execution and verification primitives."""
import json
import os
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from .chunked_write import ChunkedWriteError, ChunkedWriteManager
from .project_sync import inspect_project_scope, compare_project_snapshot, load_project_snapshot
from .project_bundle import dependency_evidence, build_source_bundles, delta_records, build_project_delta
from .project_sync_message import build_atomic_project_sync
from .project_ledger import inspect_project_ledger, query_project_history
from .project_access import (
    ProjectAccessError,
    build_project_capsule,
    query_project_with_runtime_index,
)
from .edit_plan_contract import validate_edit_plan
from .batch_apply import apply_edit_plan
from .aggregated_verification import run_aggregated_verification
from .semantic_map import inspect_semantic_map, update_semantic_map, update_semantic_map_file
from .task_plan import freeze_task_plan, freeze_task_plan_file, execute_frozen_task_plan
from .bounded_process import run_bounded_process
from .command_security import (CommandSecurityError, build_command_approval_manifest, inspect_command, require_command_allowed)
from .capability_recovery import build_evidence_to_action_route, render_evidence_to_action_guidance
from .path_security import PathSecurityError, is_within
from .safe_file_operations import SafeFileOperationError, build_delete_manifest, execute_delete_manifest
from .security_approval import SecurityApprovalError, SecurityApprovalLedger
from .security_context import SecurityContext
from .windows_security import restricted_executor_required


_SCOPED_INSPECTION_TOOLS = {"list_directory", "inspect_directory", "inspect_project_scope"}
_PROJECT_SCOPE_UNSUPPORTED_FIELDS = {"project_root", "path", "depth", "include_files"}


def _directory_listing_request(agent) -> bool:
    text = str(getattr(agent, "current_request_text", "") or "")
    lowered = text.casefold()
    chinese_listing = any(token in text for token in ("列出", "顯示", "有哪些檔案", "有什麼檔案")) and any(
        token in text for token in ("檔案", "文件", "目錄", "資料夾", "路徑下")
    )
    english_listing = any(
        token in lowered
        for token in ("list files", "list the files", "show files", "directory contents")
    )
    return bool(chinese_listing or english_listing)


def _explicit_request_paths(agent) -> list[Path]:
    roots: list[Path] = []
    for raw in tuple(getattr(agent, "_authorized_local_paths", ()) or ()):
        try:
            resolved = Path(str(raw)).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def preview_tool_scope(tool_call: dict, agent=None) -> dict:
    """Resolve an inspection scope without performing the inspection.

    ``execute_tool`` consumes these same resolved paths, so the path shown in
    telemetry is guaranteed to be the path used by the filesystem operation.
    """
    tool = str(tool_call.get("tool", "") or "")
    explicit_paths = _explicit_request_paths(agent)
    info = {
        "tool": tool,
        "allowed": True,
        "requested_paths": [],
        "resolved_paths": [],
        "explicit_request_paths": [str(path) for path in explicit_paths],
        "scope_source": "none",
        "error": "",
    }
    if tool not in _SCOPED_INSPECTION_TOOLS:
        return info

    try:
        security_context = SecurityContext.from_agent(agent, fallback_workspace=Path.cwd())
        if _directory_listing_request(agent) and tool != "list_directory":
            raise ValueError("directory_listing_requires_list_directory")
        if tool == "inspect_project_scope":
            unsupported = sorted(_PROJECT_SCOPE_UNSUPPORTED_FIELDS.intersection(tool_call))
            if unsupported:
                raise ValueError("inspect_project_scope_unsupported_fields:" + ",".join(unsupported))
            requested = str(tool_call.get("workspace", "") or "").strip()
            if explicit_paths and not requested:
                raise ValueError("explicit_path_scope_requires_workspace")
            info["requested_paths"] = [requested] if requested else []
            resolved_paths = [Path(_project_workspace(tool_call, agent)).resolve()]
            info["scope_source"] = "workspace" if requested else "workspace_default"
        elif tool == "list_directory":
            requested = str(tool_call.get("path", "") or "").strip()
            if explicit_paths and not requested:
                raise ValueError("explicit_path_scope_requires_path")
            requested = requested or "."
            info["requested_paths"] = [requested]
            resolved_paths = [security_context.require_read(requested)]
            info["scope_source"] = "path" if requested != "." else "workspace_default"
        else:
            requested_values = [str(value) for value in tool_call.get("paths", [])]
            if explicit_paths and not requested_values:
                raise ValueError("explicit_path_scope_requires_paths")
            info["requested_paths"] = requested_values
            resolved_paths = [security_context.require_read(value) for value in requested_values]
            info["scope_source"] = "paths"

        if explicit_paths:
            expanded = [
                resolved
                for resolved in resolved_paths
                if not any(is_within(resolved, explicit) for explicit in explicit_paths)
            ]
            if expanded:
                raise ValueError(
                    "resolved_scope_expands_beyond_explicit_path:"
                    + ",".join(str(path) for path in expanded)
                )
        info["resolved_paths"] = [str(path) for path in resolved_paths]
    except (PathSecurityError, ValueError, OSError, RuntimeError) as exc:
        info["allowed"] = False
        info["error"] = str(exc)
    return info


def _queue_agent_attachments(agent, paths, trace_id: str):
    """Pass trace identity when supported while keeping legacy test adapters valid."""
    try:
        return agent.queue_attachments(paths, trace_id=trace_id)
    except TypeError as exc:
        if "trace_id" not in str(exc):
            raise
        return agent.queue_attachments(paths)

def _run_powershell_capture(
    command: str,
    timeout: int = 30,
    capture_root: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    telemetry: dict | None = None,
    approval_manifest: dict | None = None,
) -> dict:
    """Execute PowerShell and return structured evidence instead of only text."""
    try:
        require_command_allowed(command, workspace=cwd, approval_manifest=approval_manifest)
    except CommandSecurityError as exc:
        return {
            "exit_code": None, "stdout": "", "stderr": "", "timed_out": False,
            "error": f"SECURITY_COMMAND_REJECTED:{exc}", "security_rejected": True,
        }
    payload = run_bounded_process(
        ["powershell", "-NoProfile", "-Command", command],
        cwd=cwd,
        timeout=timeout,
        capture_root=capture_root,
        telemetry=telemetry, approval_manifest=approval_manifest, security_command=command,
    )
    return {"command": command, **payload}


def _evaluate_verification_step(
    step: dict,
    default_timeout: int = 30,
    capture_root: str | Path | None = None,
    *,
    security_context: SecurityContext | None = None,
) -> dict:
    """Mechanically execute Planner-specified verification; no AI decision here."""
    if not isinstance(step, dict):
        return {
            "label": "invalid_verification_step",
            "action": "invalid",
            "passed": False,
            "spec_valid": False,
            "error": f"verification step must be dict/object; got {type(step).__name__}: {step!r}",
        }
    action = step.get("action", "run_command")
    label = step.get("label", action)
    delay_sec = float(step.get("delay_sec", 0) or 0)
    if delay_sec > 0:
        time.sleep(delay_sec)
    evidence = {"label": label, "action": action, "passed": False, "spec_valid": True}

    if action == "run_command":
        command = step.get("command", "")
        if not isinstance(command, str) or not command.strip():
            evidence.update({"spec_valid": False, "error": "run_command verification requires a non-empty command"})
            return evidence
        result = _run_powershell_capture(
            command, int(step.get("timeout", default_timeout)), capture_root,
            cwd=security_context.workspace_root if security_context else None,
        )
        evidence["result"] = result
        passed = not result.get("timed_out") and result.get("exit_code") == step.get("expect_exit_code", 0)
        combined = (result.get("stdout", "") + "\n" + result.get("stderr", ""))
        if step.get("expect_contains") is not None:
            expected = step.get("expect_contains")
            expected = expected if isinstance(expected, list) else [expected]
            passed = passed and all(str(x) in combined for x in expected)
        if step.get("expect_not_contains") is not None:
            forbidden = step.get("expect_not_contains")
            forbidden = forbidden if isinstance(forbidden, list) else [forbidden]
            passed = passed and all(str(x) not in combined for x in forbidden)
        if step.get("expect_regex") is not None:
            patterns = step.get("expect_regex")
            patterns = patterns if isinstance(patterns, list) else [patterns]
            try:
                passed = passed and all(
                    re.search(str(pattern), combined, flags=re.MULTILINE) is not None
                    for pattern in patterns
                )
            except re.error as exc:
                evidence.update({"spec_valid": False, "error": f"invalid expect_regex: {exc}"})
                return evidence
        evidence["passed"] = bool(passed)
        return evidence

    if action == "file_exists":
        try:
            path = security_context.require_read(step.get("path", "")) if security_context else Path(step.get("path", ""))
        except PathSecurityError as exc:
            evidence["error"] = f"SECURITY_PATH_REJECTED:{exc}"
            return evidence
        exists = path.exists()
        expected = bool(step.get("expect", True))
        evidence.update({"path": str(path), "exists": exists, "expected": expected, "passed": exists == expected})
        return evidence

    if action == "file_contains":
        try:
            path = security_context.require_read(step.get("path", "")) if security_context else Path(step.get("path", ""))
        except PathSecurityError as exc:
            evidence["error"] = f"SECURITY_PATH_REJECTED:{exc}"
            return evidence
        needle = str(step.get("text", ""))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            found = needle in text
            expected = bool(step.get("expect", True))
            evidence.update({"path": str(path), "text": needle, "found": found, "expected": expected, "passed": found == expected})
        except Exception as e:
            evidence["error"] = str(e)
        return evidence

    evidence["spec_valid"] = False
    evidence["error"] = f"未知 verification action: {action}"
    return evidence


def tool_run_command(
    command: str,
    timeout: int = 30,
    verify: list = None,
    success_criteria: str = "",
    capture_root: str | Path | None = None,
    *,
    security_context: SecurityContext | None = None,
    approval_manifest: dict | None = None,
) -> str:
    """Run an action and the Web Planner's explicit post-action verification plan.

    The local runtime never invents success criteria.  It only executes the
    checks supplied by the Planner and reports PASS/FAIL evidence back.
    """
    telemetry = None
    if security_context and security_context.interface_name == "remote":
        telemetry = {"root": security_context.workspace_root, "task_id": security_context.task_id, "request_id": security_context.request_id}
    main = _run_powershell_capture(
        command, timeout, capture_root,
        cwd=security_context.workspace_root if security_context else None,
        telemetry=telemetry, approval_manifest=approval_manifest,
    )
    lines = [
        "[COMMAND_RESULT]",
        f"command: {command}",
        f"exit_code: {main.get('exit_code')}",
    ]
    if main.get("stdout"):
        lines.append("stdout:\n" + main["stdout"])
    if main.get("stderr"):
        lines.append("stderr:\n" + main["stderr"])
    for stream in ("stdout", "stderr"):
        if main.get(f"{stream}_truncated"):
            lines.append(
                f"[{stream.upper()}_LOCAL_REF] bytes={main.get(f'{stream}_bytes', 0)} "
                f"path={main.get(f'{stream}_ref', '')}"
            )
    if main.get("error"):
        lines.append("error: " + main["error"])

    if main.get("timed_out") or main.get("exit_code") not in (0,):
        lines.append("VERIFICATION_STATUS: FAIL")
        lines.append("原因: 主指令本身未成功完成；請 Planner 根據 stderr/error 修正後再執行。")
        return "\n".join(lines)

    if verify is None:
        checks = []
    elif isinstance(verify, dict):
        checks = [verify]
    elif isinstance(verify, (list, tuple)):
        checks = list(verify)
    else:
        checks = [verify]

    if not checks:
        lines.append("VERIFICATION_STATUS: UNVERIFIED")
        lines.append("原因: Planner 沒有提供 post-run verification actions；不得僅憑指令 exit_code 宣告改動成功。")
        return "\n".join(lines)

    lines.append("[POST_RUN_VERIFICATION]")
    if success_criteria:
        lines.append("success_criteria: " + success_criteria)
    all_passed = True
    all_specs_valid = True
    for idx, step in enumerate(checks, 1):
        ev = _evaluate_verification_step(
            step, default_timeout=timeout, capture_root=capture_root,
            security_context=security_context,
        )
        all_specs_valid = all_specs_valid and bool(ev.get("spec_valid", True))
        all_passed = all_passed and bool(ev.get("passed"))
        lines.append(f"verify[{idx}]: " + json.dumps(ev, ensure_ascii=False, default=str))
    verification_status = "SPEC_INVALID" if not all_specs_valid else ("PASS" if all_passed else "FAIL")
    lines.append("VERIFICATION_STATUS: " + verification_status)
    if verification_status == "SPEC_INVALID":
        lines.append("驗證規格無效：請 Planner 修正 verify schema/matcher 後，以 verifies_action_id 綁定原 action 重新驗證。")
    elif not all_passed:
        lines.append("驗證失敗：請 Planner 讀取上述 evidence，定位問題、修正，再重新執行與驗證。")
    return "\n".join(lines)

def run_verification_input_self_tests() -> dict:
    """Regression tests for malformed verification payloads."""
    valid = {"action": "file_exists", "path": __file__, "expect": True}
    cases = {
        "verify_dict": valid,
        "verify_list_dict": [valid],
        "verify_string": "malformed verification",
        "verify_mixed_list": ["malformed verification", valid],
    }
    results = {}
    for name, verify in cases.items():
        try:
            if verify is None:
                checks = []
            elif isinstance(verify, dict):
                checks = [verify]
            elif isinstance(verify, (list, tuple)):
                checks = list(verify)
            else:
                checks = [verify]
            evidence = [_evaluate_verification_step(step) for step in checks]
            results[name] = {"passed": True, "evidence": evidence}
        except Exception as e:
            results[name] = {"passed": False, "error": repr(e)}
    results["all_passed"] = all(x["passed"] for k, x in results.items() if k != "all_passed")
    return results

def tool_read_file(path: str, *, security_context: SecurityContext | None = None) -> str:
    try:
        p = security_context.require_read(path) if security_context else Path(path)
        if not p.exists():
            return f"[錯誤] 檔案不存在: {path}"
        if p.stat().st_size > 1_000_000:
            return f"[錯誤] 檔案過大"
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[錯誤] {e}"

def tool_write_file(path: str, content: str, *, security_context: SecurityContext | None = None) -> str:
    try:
        p = security_context.require_write(path) if security_context else Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"[成功] 寫入 {p.stat().st_size} bytes 到 {path}"
    except Exception as e:
        return f"[錯誤] {e}"

def tool_list_directory(path: str = ".", *, security_context: SecurityContext | None = None) -> str:
    try:
        p = security_context.require_read(path) if security_context else Path(path)
        if not p.exists():
            return f"[錯誤] 目錄不存在: {path}"
        items = []
        for item in sorted(p.iterdir()):
            tag = "[DIR] " if item.is_dir() else "[FILE]"
            size = f" ({item.stat().st_size/1024:.1f}KB)" if item.is_file() else ""
            items.append(f"{tag} {item.name}{size}")
        return "\n".join(items) if items else "(空目錄)"
    except Exception as e:
        return f"[錯誤] {e}"


def _path_within_allowed_roots(path: Path, allowed_roots: list[str]) -> bool:
    candidate = os.path.normcase(str(path))
    for raw_root in allowed_roots:
        try:
            root = os.path.normcase(str(Path(raw_root).expanduser().resolve()))
            if os.path.commonpath([candidate, root]) == root:
                return True
        except (OSError, ValueError):
            continue
    return False


def tool_inspect_directory(
    paths: list[str],
    *,
    allowed_roots: list[str],
    recursive: bool = True,
    sample_limit: int = 20,
) -> str:
    """Return bounded metadata only for user-authorized local roots."""
    started = time.perf_counter()
    limit = max(0, min(int(sample_limit), 100))
    reports = []

    for raw_path in paths:
        root_started = time.perf_counter()
        report = {
            "path": str(raw_path), "authorized": False, "exists": False,
            "file_count": 0, "directory_count": 0, "total_bytes": 0,
            "extensions": {}, "samples": [], "inaccessible_count": 0, "errors": [],
        }
        extensions: Counter[str] = Counter()
        try:
            root = Path(raw_path).expanduser().resolve()
            report["path"] = str(root)
            report["authorized"] = _path_within_allowed_roots(root, allowed_roots)
            if not report["authorized"]:
                report["errors"].append("path_not_authorized_by_user_turn")
            elif not root.exists():
                report["errors"].append("path_not_found")
            elif not root.is_dir():
                report["errors"].append("not_a_directory")
            else:
                report["exists"] = True
                pending = [root]
                while pending:
                    current = pending.pop()
                    try:
                        with os.scandir(current) as entries:
                            for entry in entries:
                                try:
                                    if entry.is_dir(follow_symlinks=False):
                                        report["directory_count"] += 1
                                        if recursive:
                                            pending.append(Path(entry.path))
                                        continue
                                    if not entry.is_file(follow_symlinks=False):
                                        continue
                                    stat = entry.stat(follow_symlinks=False)
                                    report["file_count"] += 1
                                    report["total_bytes"] += int(stat.st_size)
                                    extensions[Path(entry.name).suffix.lower() or "[no_extension]"] += 1
                                    if len(report["samples"]) < limit:
                                        report["samples"].append({
                                            "path": str(Path(entry.path)), "bytes": int(stat.st_size),
                                        })
                                except (OSError, PermissionError) as exc:
                                    report["inaccessible_count"] += 1
                                    if len(report["errors"]) < 10:
                                        report["errors"].append(f"{entry.path}: {type(exc).__name__}: {exc}")
                    except (OSError, PermissionError) as exc:
                        report["inaccessible_count"] += 1
                        if len(report["errors"]) < 10:
                            report["errors"].append(f"{current}: {type(exc).__name__}: {exc}")
        except Exception as exc:
            report["errors"].append(f"{type(exc).__name__}: {exc}")

        ordered = sorted(extensions.items(), key=lambda item: (-item[1], item[0]))
        report["extensions"] = dict(ordered[:30])
        if len(ordered) > 30:
            report["extensions_other"] = sum(count for _, count in ordered[30:])
        report["elapsed_ms"] = round((time.perf_counter() - root_started) * 1000, 3)
        reports.append(report)

    payload = {
        "tool": "inspect_directory", "recursive": bool(recursive),
        "sample_limit_per_path": limit, "paths": reports,
        "aggregate": {
            "file_count": sum(item["file_count"] for item in reports),
            "directory_count": sum(item["directory_count"] for item in reports),
            "total_bytes": sum(item["total_bytes"] for item in reports),
            "inaccessible_count": sum(item["inaccessible_count"] for item in reports),
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

def tool_web_search(query: str) -> str:
    """Simple web search via DuckDuckGo (no API key needed)."""
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_redirect=1"
        req = urllib.request.Request(url, headers={"User-Agent": "SmartAgent/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
        abstract = data.get("AbstractText", "")
        related = [t.get("Text","") for t in data.get("RelatedTopics", [])[:3]]
        return abstract or "\n".join(related) or "(無結果)"
    except Exception as e:
        return f"[網路搜尋失敗] {e}"

import urllib.parse

SKIP_SEARCH_DIRS = {".git", ".svn", "node_modules", "__pycache__", ".idea", ".vs", "build", "dist"}

def tool_find_file(
    name: str,
    root: str = ".",
    max_results: int = 20,
    *,
    security_context: SecurityContext | None = None,
) -> str:
    """Find files below root by exact name first, then by substring."""
    try:
        base = security_context.require_read(root) if security_context else Path(root).expanduser().resolve()
        if not base.exists() or not base.is_dir():
            return f"[錯誤] 搜尋根目錄不存在: {root}"
        query = name.strip().lower()
        if not query:
            return "[錯誤] find_file 缺少 name"

        exact, partial = [], []
        for current_root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_SEARCH_DIRS]
            for filename in files:
                low = filename.lower()
                full = str(Path(current_root) / filename)
                if low == query:
                    exact.append(full)
                elif query in low:
                    partial.append(full)
                if len(exact) + len(partial) >= max_results * 3:
                    break
        results = (exact + partial)[:max_results]
        return "\n".join(results) if results else f"[未找到] {name} (root={base})"
    except Exception as e:
        return f"[錯誤] {e}"

def tool_save_session_summary(agent, payload: dict) -> str:
    if not agent or not agent.workspace_root:
        return "[錯誤] 尚未建立 Workspace Root，無法保存 project session summary"
    return agent.save_project_summary(payload)


def _project_workspace(tool_call: dict, agent, *, write: bool = False):
    requested=tool_call.get("workspace","")
    default=str(agent.workspace_root) if agent and getattr(agent,"workspace_root",None) else "."
    context = SecurityContext.from_agent(agent, fallback_workspace=Path.cwd())
    candidate = requested or default
    try:
        authorized = context.require_write(candidate) if write else context.require_read(candidate)
    except PathSecurityError as exc:
        raise ValueError(f"workspace_not_authorized:{candidate}:{exc}") from exc
    if not authorized.exists() or not authorized.is_dir():
        raise ValueError(f"workspace_not_found_or_not_directory:{authorized}")
    return str(authorized)


def _project_sync_workspace(tool_call: dict, agent) -> str:
    """Resolve project_sync target while keeping workspace as a boundary.

    ``project_root`` (or legacy ``path``) names the project to package.
    ``workspace`` may name the same directory or an authorized parent
    container.  Unrelated roots remain a fail-closed conflict.
    """
    project_value = str(tool_call.get("project_root", "") or "").strip()
    path_value = str(tool_call.get("path", "") or "").strip()
    workspace_value = str(tool_call.get("workspace", "") or "").strip()

    target_value = project_value or path_value or workspace_value
    target = Path(
        _project_workspace({"workspace": target_value}, agent, write=True)
    ).resolve()

    if project_value and path_value:
        alias = Path(
            _project_workspace({"workspace": path_value}, agent, write=True)
        ).resolve()
        if alias != target:
            raise ValueError("project_sync_root_conflict:project_root_and_path")

    if workspace_value and (project_value or path_value):
        boundary = Path(
            _project_workspace({"workspace": workspace_value}, agent, write=True)
        ).resolve()
        if not is_within(target, boundary):
            raise ValueError("project_sync_root_conflict:target_outside_workspace")

    return str(target)


def _project_query_workspace(tool_call: dict, agent) -> str:
    """Resolve the exact read-only root for ``query_project``.

    Unlike ``project_sync``, query operations use ``path`` only inside each
    ``queries[]`` item.  Treating a top-level path as a legacy root alias makes
    a file selector ambiguous with the project root, so fail closed here even
    when an internal caller bypasses the protocol envelope validator.
    """
    misplaced = sorted(
        field for field in ("operation", "path", "symbol")
        if field in tool_call
    )
    if misplaced:
        raise ValueError(
            "query_project_top_level_query_fields_forbidden:"
            + ",".join(misplaced)
        )

    project_value = str(tool_call.get("project_root", "") or "").strip()
    if not project_value:
        raise ValueError("query_project_project_root_required")
    target = Path(
        _project_workspace({"workspace": project_value}, agent, write=False)
    ).resolve()

    workspace_value = str(tool_call.get("workspace", "") or "").strip()
    if workspace_value:
        boundary = Path(
            _project_workspace({"workspace": workspace_value}, agent, write=False)
        ).resolve()
        if not is_within(target, boundary):
            raise ValueError("query_project_root_conflict:target_outside_workspace")
    return str(target)

def _execution_approval_manifest(tool_call: dict, agent, security_context: SecurityContext) -> dict | None:
    command = str(tool_call.get("command", "") or "")
    admission = inspect_command(command, workspace=security_context.workspace_root)
    if admission.allowed:
        return None
    if not admission.approval_eligible:
        raise CommandSecurityError(admission.code, admission.detail)
    manifest = build_command_approval_manifest(command, workspace=security_context.workspace_root)
    from .protocol_v8 import action_digest as v8_action_digest
    action_id = str(tool_call.get("action_id", "") or "")
    admitted = dict(getattr(agent, "_v8_admitted_actions", {}).get(action_id, {}) or {}) if agent else {}
    binding = {
        "request_id": security_context.request_id,
        "task_id": security_context.task_id,
        "action_id": action_id,
        "action_digest": str(admitted.get("action_digest", "") or v8_action_digest(tool_call)),
        "target": manifest["target"],
        "manifest_digest": manifest["manifest_digest"],
        "chat_id": str(getattr(agent, "security_approval_chat_id", "") or ""),
        "permanent_scope": "",
        "workspace_root": str(security_context.workspace_root),
        "approval_kind": "EXECUTION",
    }
    ledger = SecurityApprovalLedger(getattr(agent, "security_approval_ledger_root", security_context.workspace_root))
    record = ledger.request(binding, ttl_sec=float(getattr(agent, "security_approval_timeout_sec", 300.0) or 300.0))
    notifier = getattr(agent, "notify_security_approval", None)
    if not callable(notifier) or getattr(agent, "_security_approval_notifier", None) is None:
        raise SecurityApprovalError(f"execution_confirmation_required:{record['approval_id']}")
    notifier(record, manifest)
    deadline = float(record["expires_at"])
    while time.time() < deadline:
        cancel_check = getattr(agent, "security_approval_cancel_check", None)
        if callable(cancel_check):
            cancel_check()
        current = ledger.get(record["approval_id"])
        state = str((current or {}).get("state", ""))
        if state == "APPROVED":
            approved = dict(manifest)
            approved["_approval_id"] = record["approval_id"]
            approved["_approval_binding"] = dict(binding)
            if not restricted_executor_required():
                ledger.consume(record["approval_id"], binding)
                approved["_approval_consumed"] = True
            return approved
        if state in {"REJECTED", "CANCELLED", "EXPIRED", "CONSUMED"}:
            raise SecurityApprovalError(f"execution_approval_state={state}")
        time.sleep(0.5)
    raise SecurityApprovalError("execution_approval_expired")


def execute_tool(tool_call: dict, agent=None, models: dict | None = None) -> str:
    models = models or getattr(agent, "_models_registry", {}) or {}
    tool = tool_call.get("tool", "")
    security_context = SecurityContext.from_agent(agent, fallback_workspace=Path.cwd())
    scope = preview_tool_scope(tool_call, agent)
    if tool in _SCOPED_INSPECTION_TOOLS and not scope["allowed"]:
        return "[TOOL_SCOPE_REJECTED] " + json.dumps(
            scope, ensure_ascii=False, separators=(",", ":")
        )
    if tool == "run_command":
        workspace = security_context.workspace_root
        try:
            approval_manifest = _execution_approval_manifest(tool_call, agent, security_context)
        except (CommandSecurityError, SecurityApprovalError) as exc:
            if agent:
                agent.last_verification_status = "FAIL"
            return f"[SECURITY_COMMAND_REJECTED] {exc}\nVERIFICATION_STATUS: FAIL"
        result = tool_run_command(
            tool_call.get("command", ""),
            timeout=int(tool_call.get("timeout", 30)),
            verify=tool_call.get("verify") or [],
            success_criteria=tool_call.get("success_criteria", ""),
            capture_root=workspace / ".agents" / "results" / "command_capture",
            security_context=security_context, approval_manifest=approval_manifest,
        )
        if agent:
            if "VERIFICATION_STATUS: PASS" in result:
                agent.last_verification_status = "PASS"
            elif "VERIFICATION_STATUS: SPEC_INVALID" in result:
                agent.last_verification_status = "SPEC_INVALID"
            elif "VERIFICATION_STATUS: FAIL" in result:
                agent.last_verification_status = "FAIL"
            else:
                agent.last_verification_status = "UNVERIFIED"
        return result
    elif tool == "read_file":
        path = tool_call.get("path", "")
        try:
            authorized_path = security_context.require_read(path)
        except PathSecurityError as exc:
            return f"[SECURITY_PATH_REJECTED] {exc}"
        # Web Planner mode is attachment-first: if the web brain asks to
        # inspect a file, never paste the file contents back as text. Queue
        # the real file for upload to the SAME web conversation instead.
        if agent and models.get(agent.planner_key, {}).get("provider") == "web_scraper":
            evidence_route = build_evidence_to_action_route(
                [tool_call], getattr(agent, "_authorized_local_paths", ()) or (),
            )
            if evidence_route.get("active"):
                return (
                    "[WEBAGENT_EVIDENCE_ROUTE_REQUIRED]\n"
                    + render_evidence_to_action_guidance(evidence_route)
                )
            queued = agent.queue_attachments([str(authorized_path)])
            return "[Web Planner 模式：read_file 已自動改道為 upload_file，不展開檔案文字]\n" + queued
        return tool_read_file(str(authorized_path), security_context=security_context)
    elif tool == "write_file":
        return tool_write_file(
            tool_call.get("path", ""), tool_call.get("content", ""),
            security_context=security_context,
        )
    elif tool in {"begin_file_write", "write_file_chunk", "commit_file_write", "abort_file_write"}:
        if not agent:
            return "[CHUNKED_WRITE_FAILED] Agent 實例遺失"
        manager = getattr(agent, "_chunked_write_manager", None)
        if manager is None:
            workspace = Path(getattr(agent, "workspace_root", None) or Path.cwd()).resolve()
            roots = list(security_context.write_roots)
            manager = ChunkedWriteManager(workspace / ".agents" / "chunked_writes", roots)
            agent._chunked_write_manager = manager
        try:
            if tool == "begin_file_write":
                result = manager.begin(
                    tool_call,
                    request_id=str(getattr(agent, "current_request_id", "") or ""),
                    run_id=str(getattr(agent, "current_run_id", "") or ""),
                    turn_id=getattr(agent, "_protocol_turn_seq", ""),
                )
            elif tool == "write_file_chunk":
                result = manager.write_chunk(tool_call)
            elif tool == "commit_file_write":
                result = manager.commit(tool_call)
            else:
                result = manager.abort(tool_call)
            return "[CHUNKED_WRITE_SUCCESS] " + json.dumps(result, ensure_ascii=False, sort_keys=True)
        except ChunkedWriteError as exc:
            return f"[CHUNKED_WRITE_FAILED] {exc}"
    elif tool == "list_directory":
        return tool_list_directory(
            scope["resolved_paths"][0], security_context=security_context
        )
    elif tool == "inspect_directory":
        allowed_roots = [str(path) for path in security_context.read_roots]
        try:
            inspect_paths = list(scope["resolved_paths"])
        except PathSecurityError as exc:
            return json.dumps({
                "tool": "inspect_directory", "paths": [],
                "security_rejected": True, "error": str(exc),
            }, ensure_ascii=False, separators=(",", ":"))
        return tool_inspect_directory(
            inspect_paths,
            allowed_roots=allowed_roots,
            recursive=tool_call.get("recursive", True),
            sample_limit=tool_call.get("sample_limit", 20),
        )
    elif tool == "inspect_project_scope":
        workspace=scope["resolved_paths"][0]
        return json.dumps(inspect_project_scope(workspace),ensure_ascii=False,separators=(",",":"))
    elif tool == "inspect_semantic_map":
        workspace=_project_workspace(tool_call,agent)
        return json.dumps(inspect_semantic_map(workspace,tool_call.get("paths")),ensure_ascii=False,separators=(",",":"))
    elif tool == "update_semantic_map":
        workspace=_project_workspace(tool_call,agent,write=True)
        return json.dumps(update_semantic_map(workspace,tool_call.get("patch",{})),ensure_ascii=False,separators=(",",":"))
    elif tool == "update_semantic_map_file":
        workspace=_project_workspace(tool_call,agent,write=True)
        return json.dumps(update_semantic_map_file(workspace,tool_call.get("path",""),tool_call.get("expected_sha256","")),ensure_ascii=False,separators=(",",":"))
    elif tool == "compare_project_snapshot":
        workspace=_project_workspace(tool_call,agent)
        current=inspect_project_scope(workspace)
        known_snapshot=tool_call.get("known_snapshot")
        if isinstance(known_snapshot,str):
            known=load_project_snapshot(workspace,known_snapshot)
        elif known_snapshot is None or isinstance(known_snapshot,dict):
            known=known_snapshot
        else:
            raise TypeError(f"known_snapshot must be snapshot ID string, dict, or None; got {type(known_snapshot).__name__}")
        return json.dumps(compare_project_snapshot(current,known),ensure_ascii=False,separators=(",",":"))
    elif tool == "extract_project_dependencies":
        workspace=_project_workspace(tool_call,agent)
        snapshot=inspect_project_scope(workspace)
        return json.dumps(dependency_evidence(workspace,snapshot.get("files",[])),ensure_ascii=False,separators=(",",":"))
    elif tool == "build_project_bundle":
        workspace=_project_workspace(tool_call,agent,write=True)
        snapshot=inspect_project_scope(workspace)
        output_dir=tool_call.get("output_dir","") or str(Path(workspace)/".agents"/"project_sync"/snapshot.get("snapshot_id","unknown"))
        result=build_source_bundles(workspace,snapshot.get("files",[]),output_dir,max_bytes=int(tool_call.get("max_bytes",500000)),max_files=int(tool_call.get("max_files",50)))
        result["snapshot_id"]=snapshot.get("snapshot_id","")
        return json.dumps(result,ensure_ascii=False,separators=(",",":"))
    elif tool == "build_project_delta":
        workspace=_project_workspace(tool_call,agent,write=True)
        return json.dumps(build_project_delta(workspace,tool_call.get("base_snapshot_id","")),ensure_ascii=False,separators=(",",":"))
    elif tool == "project_sync":
        strategy=str(tool_call.get("strategy","FULL_BUNDLE") or "FULL_BUNDLE").upper()
        if strategy not in {"INDEX_ONLY","FULL_BUNDLE","DELTA"}:
            raise ValueError(f"project_sync_strategy_invalid:{strategy}")
        workspace=_project_sync_workspace(tool_call,agent)
        if strategy == "INDEX_ONLY":
            return json.dumps(build_project_capsule(workspace),ensure_ascii=False,separators=(",",":"))
        result=build_atomic_project_sync(workspace,strategy,base_snapshot_id=tool_call.get("base_snapshot_id",""),max_bytes=int(tool_call.get("max_bytes",500000)),max_files=int(tool_call.get("max_files",50)))
        if result.get("status")=="READY" and agent:
            runner=getattr(agent,"run_project_sync_transaction",None)
            if runner is None:
                result["status"]="LOCAL_READY"
                result["sync_status"]="LOCAL_READY"
                result["project_sync_runtime"]={"status":"TRANSPORT_NOT_CONFIGURED"}
            else:
                runtime=runner(result["transaction"], project_root=workspace)
                result["project_sync_runtime"]=runtime
                result["sync_status"]=runtime.get("status","INCOMPLETE")
                result["status"]="READY" if result["sync_status"]=="PROJECT_SYNC_READY" else "INCOMPLETE"
        return json.dumps(result,ensure_ascii=False,separators=(",",":"))
    elif tool == "query_project":
        workspace=_project_query_workspace(tool_call,agent)
        try:
            result=query_project_with_runtime_index(
                workspace,
                requested_snapshot_id=str(tool_call.get("snapshot_id","") or ""),
                requested_project_handle=str(tool_call.get("project_handle","") or ""),
                queries=tool_call.get("queries",[]),
                max_bytes=int(tool_call.get("max_bytes",24*1024)),
                request_id=str(getattr(agent,"current_request_id","") or ""),
                action_id=str(tool_call.get("action_id","") or ""),
            )
        except ProjectAccessError as exc:
            result={
                "schema":"PROJECT_ACCESS_QUERY_RESULT_V1",
                "status":"REJECTED",
                "error":str(exc),
                "project_root":workspace,
                "attachments_uploaded":0,
                "recovery_action":"NONE",
            }
        return json.dumps(result,ensure_ascii=False,separators=(",",":"))
    elif tool == "inspect_project_ledger":
        workspace=_project_workspace(tool_call,agent)
        return json.dumps(
            inspect_project_ledger(workspace,limit=int(tool_call.get("limit",20))),
            ensure_ascii=False,separators=(",",":"),
        )
    elif tool == "query_project_history":
        workspace=_project_workspace(tool_call,agent)
        return json.dumps(
            query_project_history(
                workspace,
                event_types=tool_call.get("event_types",[]),
                snapshot_id=str(tool_call.get("snapshot_id","") or ""),
                path_contains=str(tool_call.get("path_contains","") or ""),
                since=tool_call.get("since"),
                limit=int(tool_call.get("limit",50)),
            ),
            ensure_ascii=False,separators=(",",":"),
        )
    elif tool == "validate_edit_plan":
        workspace=_project_workspace(tool_call,agent)
        return json.dumps(validate_edit_plan(workspace,tool_call.get("plan",{})),ensure_ascii=False,separators=(",",":"))
    elif tool == "apply_edit_plan":
        workspace=_project_workspace(tool_call,agent,write=True)
        return json.dumps(apply_edit_plan(workspace,tool_call.get("plan",{})),ensure_ascii=False,separators=(",",":"))
    elif tool == "aggregate_verification":
        workspace=_project_workspace(tool_call,agent,write=True)
        return json.dumps(run_aggregated_verification(workspace,tool_call.get("commands",[]),timeout=int(tool_call.get("timeout",120))),ensure_ascii=False,separators=(",",":"))
    elif tool == "propose_task_plan":
        workspace=_project_workspace(tool_call,agent,write=True)
        request_scope={field:tool_call.get(field) for field in ("request_id","task_id","task_epoch","intent_digest","request_phase","continuation_seq")} if tool_call.get("task_epoch") else None
        return json.dumps(freeze_task_plan(workspace,tool_call.get("plan",{}),request_scope=request_scope),ensure_ascii=False,separators=(",",":"))
    elif tool == "propose_task_plan_file":
        workspace=_project_workspace(tool_call,agent,write=True)
        request_scope={field:tool_call.get(field) for field in ("request_id","task_id","task_epoch","intent_digest","request_phase","continuation_seq")} if tool_call.get("task_epoch") else None
        return json.dumps(freeze_task_plan_file(workspace,tool_call.get("path",""),tool_call.get("expected_sha256",""),request_scope=request_scope),ensure_ascii=False,separators=(",",":"))
    elif tool == "execute_frozen_plan":
        workspace=_project_workspace(tool_call,agent,write=True)
        request_scope={field:tool_call.get(field) for field in ("request_id","task_id","task_epoch","intent_digest","request_phase","continuation_seq")} if tool_call.get("task_epoch") else None
        return json.dumps(execute_frozen_task_plan(workspace,tool_call.get("plan_id",""),timeout=int(tool_call.get("timeout",120)),request_scope=request_scope),ensure_ascii=False,separators=(",",":"))
    elif tool == "web_search":
        return tool_web_search(tool_call.get("query", ""))
    elif tool == "find_file":
        root = tool_call.get("root", "") or (str(agent.workspace_root) if agent and agent.workspace_root else ".")
        return tool_find_file(
            tool_call.get("name", ""), root=root,
            max_results=int(tool_call.get("max_results", 20)),
            security_context=security_context,
        )
    elif tool == "upload_file":
        if not agent:
            return "[錯誤] Agent 實例遺失，無法排程附件"
        try:
            path = str(security_context.require_read(tool_call.get("path", "")))
        except PathSecurityError as exc:
            return f"[SECURITY_PATH_REJECTED] {exc}"
        return _queue_agent_attachments(
            agent, [path], tool_call.get("action_id", "")
        )
    elif tool == "upload_files":
        if not agent:
            return "[錯誤] Agent 實例遺失，無法排程附件"
        try:
            paths = [str(security_context.require_read(path)) for path in tool_call.get("paths", [])]
        except PathSecurityError as exc:
            return f"[SECURITY_PATH_REJECTED] {exc}"
        return _queue_agent_attachments(
            agent, paths, tool_call.get("action_id", "")
        )
    elif tool == "return_artifact":
        if not agent or not callable(getattr(agent, "queue_outbound_artifact", None)):
            return "[TELEGRAM_ARTIFACT_REJECTED] outbound transport unavailable"
        return agent.queue_outbound_artifact(
            tool_call.get("path", ""),
            kind=tool_call.get("kind", ""),
            caption=tool_call.get("caption", ""),
        )
    elif tool == "google_drive_upload":
        if not agent:
            return "[GOOGLE_DRIVE_UPLOAD_REJECTED] Agent instance is required"
        try:
            target = security_context.require_read(tool_call.get("path", ""))
        except PathSecurityError as exc:
            return f"[GOOGLE_DRIVE_UPLOAD_REJECTED] {exc}"
        if not target.is_file():
            return f"[GOOGLE_DRIVE_UPLOAD_REJECTED] file_not_found: {target}"
        try:
            from .google_drive import upload_file_to_drive
            payload = upload_file_to_drive(
                target,
                folder_id=tool_call.get("folder_id", ""),
                name=tool_call.get("name", ""),
            )
            return "[GOOGLE_DRIVE_UPLOAD_SUCCESS] " + json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            )
        except Exception as exc:
            return f"[GOOGLE_DRIVE_UPLOAD_FAILED] {type(exc).__name__}: {exc}"
    elif tool == "web_edit_file":
        if not agent:
            return "[WEB_DIRECT_EDIT_FAILED] Agent 實例遺失"
        try:
            source = security_context.require_read(tool_call.get("path", ""))
            output_value = tool_call.get("output_path")
            output = security_context.require_write(output_value) if output_value else None
        except PathSecurityError as exc:
            return f"[WEB_DIRECT_EDIT_FAILED] SECURITY_PATH_REJECTED:{exc}"
        return agent.web_edit_file(
            str(source),
            tool_call.get("instruction", ""),
            str(output) if output else None,
        )
    elif tool == "download_artifact":
        if not agent:
            return "[ARTIFACT_DOWNLOAD_FAILED] Agent 實例遺失"
        try:
            output_path = security_context.require_write(tool_call.get("output_path", ""))
        except PathSecurityError as exc:
            return f"[ARTIFACT_DOWNLOAD_FAILED] SECURITY_PATH_REJECTED:{exc}"
        return agent.web_download_artifact(
            str(output_path),
            expected_filename=tool_call.get("expected_filename", ""),
            timeout=int(tool_call.get("timeout", 45)),
        )
    elif tool == "execute_artifact_bundle":
        if not agent:
            return "[ARTIFACT_BUNDLE_FAILED] Agent instance is required"
        from .artifact_bundle_receiver import ArtifactBundleReceiver
        from .smartagent_protocol import validate_tool_envelope
        try:
            bundle_path = security_context.require_read(tool_call.get("path", ""))
        except PathSecurityError as exc:
            return f"[ARTIFACT_BUNDLE_FAILED] SECURITY_PATH_REJECTED:{exc}"
        receiver = ArtifactBundleReceiver()
        return receiver.execute(
            str(bundle_path),
            expected_sha256=tool_call.get("expected_sha256", ""),
            validate_action=lambda action: validate_tool_envelope(
                action,
                raw_payload=json.dumps(action, ensure_ascii=False, separators=(",", ":")),
            ),
            execute_action=lambda action: execute_tool(action, agent=agent, models=models),
        )
    elif tool == "delete_path":
        try:
            manifest = build_delete_manifest(
                security_context,
                tool_call.get("path", ""),
                recursive=bool(tool_call.get("recursive", False)),
            )
        except SafeFileOperationError as exc:
            return f"[SECURITY_DELETE_REJECTED] {exc}"
        from .protocol_v8 import action_digest as v8_action_digest
        action_id = str(tool_call.get("action_id", "") or "")
        admitted = dict(getattr(agent, "_v8_admitted_actions", {}).get(action_id, {}) or {}) if agent else {}
        permanent_scope = SecurityApprovalLedger.permanent_scope_for_target(
            security_context.workspace_root, manifest["target"]
        )
        binding = {
            "request_id": security_context.request_id,
            "task_id": security_context.task_id,
            "action_id": action_id,
            "action_digest": str(admitted.get("action_digest", "") or v8_action_digest(tool_call)),
            "target": manifest["target"],
            "manifest_digest": manifest["manifest_digest"],
            "chat_id": str(getattr(agent, "security_approval_chat_id", "") or ""),
            "permanent_scope": permanent_scope,
            "workspace_root": str(security_context.workspace_root),
        }
        ledger = SecurityApprovalLedger(
            getattr(agent, "security_approval_ledger_root", security_context.workspace_root)
        )
        authorized_scope = ledger.authorized_workspace_for_target(
            security_context.workspace_root, manifest["target"]
        )
        if authorized_scope:
            deleted = execute_delete_manifest(security_context, manifest)
            deleted["authorization"] = "PERMANENT_WORKSPACE"
            deleted["workspace_scope"] = authorized_scope
            return "[SECURITY_DELETE_SUCCESS] " + json.dumps(
                deleted, ensure_ascii=False, separators=(",", ":")
            )
        record = ledger.request(
            binding,
            ttl_sec=float(getattr(agent, "security_approval_timeout_sec", 300.0) or 300.0),
        )
        notifier = getattr(agent, "notify_security_approval", None)
        if not callable(notifier) or getattr(agent, "_security_approval_notifier", None) is None:
            return "[SECURITY_CONFIRMATION_REQUIRED] " + json.dumps(
                {**manifest, "approval_id": record["approval_id"]},
                ensure_ascii=False, separators=(",", ":"),
            )
        notify_manifest = dict(manifest)
        if permanent_scope:
            notify_manifest["permanent_scope"] = permanent_scope
        notifier(record, notify_manifest)
        deadline = float(record["expires_at"])
        while time.time() < deadline:
            cancel_check = getattr(agent, "security_approval_cancel_check", None)
            if callable(cancel_check):
                cancel_check()
            current = ledger.get(record["approval_id"])
            state = str((current or {}).get("state", ""))
            if state == "APPROVED":
                ledger.consume(record["approval_id"], binding)
                return "[SECURITY_DELETE_SUCCESS] " + json.dumps(
                    execute_delete_manifest(security_context, manifest),
                    ensure_ascii=False, separators=(",", ":"),
                )
            if state in {"REJECTED", "CANCELLED", "EXPIRED", "CONSUMED"}:
                return f"[SECURITY_DELETE_REJECTED] approval_state={state}"
            time.sleep(0.5)
        return "[SECURITY_DELETE_REJECTED] approval_expired"
    elif tool == "save_session_summary":
        return tool_save_session_summary(agent, tool_call)
    elif tool == "final_response":
        return "[PROTOCOL_ERROR] final_response 是 Planner→CMD 控制訊息，不應進入 execute_tool()。"
    elif tool == "turn_commit":
        return "[PROTOCOL_ERROR] turn_commit 是 ACK 控制訊息，不應進入 execute_tool()。"
    elif tool == "ask_executor":
        if not agent:
            return "[錯誤] Agent 實例遺失，無法呼叫執行引擎"
        requested_run_id = str(tool_call.get("run_id", ""))
        explicitly_allowed = tool_call.get("allow_local_ai_fallback") is True
        if not explicitly_allowed or not requested_run_id or requested_run_id != str(agent.current_run_id):
            return (
                f"[LOCAL_AI_FALLBACK_BLOCKED] RUN_ID={agent.current_run_id} "
                "ask_executor 只允許最後手段 semantic fallback；必須帶 "
                "allow_local_ai_fallback=true 且 run_id 必須等於目前 RUN_ID。"
            )
        if agent.web_edit_succeeded_this_task:
            return (
                f"[LOCAL_AI_FALLBACK_BLOCKED] RUN_ID={agent.current_run_id} "
                "本輪 web_edit_file 已成功，禁止 Local AI 重做同一修改。"
            )
        instruction = tool_call.get("instruction", "")
        context = tool_call.get("context", "")
        print(f"\n  [>>>] RUN_ID={agent.current_run_id} 啟用 Local AI 最後手段 ({agent.executor_model}) ...", flush=True)
        messages = [
            {
                "role": "system",
                "content": (
                    "你是非決策型 Local AI fallback。只能完成 Planner 明確指定的局部任務；"
                    "不得決定下一步、不得擴張任務範圍、不得要求使用者補資料。"
                    "你的輸出必須原樣包含提供的 RUN_ID，讓 Web Planner 能確認不是舊結果。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"RUN_ID={agent.current_run_id}\n"
                    f"【決策大腦的指示】\n{instruction}\n\n【附加資訊】\n{context}\n\n"
                    f"完成後第一行必須回傳 RUN_ID={agent.current_run_id}。"
                ),
            },
        ]
        try:
            result = agent._call(agent.executor_key, messages)
            if f"RUN_ID={agent.current_run_id}" not in result:
                return (
                    f"[LOCAL_AI_EVIDENCE_FAIL] RUN_ID={agent.current_run_id} "
                    "Local AI 回覆未包含本輪亂數 RUN_ID，不能視為可信執行證據。\n" + result
                )
            return f"[LOCAL_AI_FALLBACK_RESULT] RUN_ID={agent.current_run_id}\n{result}"
        except Exception as e:
            return f"[執行引擎失敗] RUN_ID={agent.current_run_id} {e}"
    else:
        return f"[未知工具] {tool}"
