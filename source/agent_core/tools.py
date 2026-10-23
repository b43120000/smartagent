#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared deterministic tool execution and verification primitives."""
import json
import os
import re
import stat
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from .chunked_write import ChunkedWriteError, ChunkedWriteManager
from .project_sync import inspect_project_scope, inspect_project_working_set, compare_project_snapshot, load_project_snapshot, ProjectInventoryError
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
from .task_plan import (
    execute_frozen_task_plan,
    freeze_task_plan,
    freeze_task_plan_file,
    repair_task_plan,
)
from .bounded_process import run_bounded_process
from .command_security import (CommandSecurityError, build_command_approval_manifest, inspect_command, require_command_allowed)
from .capability_recovery import build_evidence_to_action_route, render_evidence_to_action_guidance
from .path_security import PathSecurityError, is_within
from .safe_file_operations import SafeFileOperationError, build_delete_manifest, execute_delete_manifest
from .security_approval import SecurityApprovalError, SecurityApprovalLedger
from .security_context import SecurityContext
from .windows_security import restricted_executor_required


_SCOPED_INSPECTION_TOOLS = {"list_directory", "inspect_directory", "inspect_project_scope", "inspect_project_working_set", "find_file"}
_PROJECT_SCOPE_UNSUPPORTED_FIELDS = {"project_root", "path", "depth", "include_files"}
READ_FILE_TEXT_MAX_BYTES = 1_000_000


def _read_bounded_bytes(path: Path) -> bytes:
    # The extra byte detects a file growing after stat without reading an
    # unbounded payload into memory.
    with path.open('rb') as stream:
        return stream.read(READ_FILE_TEXT_MAX_BYTES + 1)


def _directory_listing_request(agent) -> bool:
    text = str(getattr(agent, "current_request_text", "") or "")
    # Negated mentions are not affirmative listing instructions. Keep any
    # separate positive listing instruction intact; this never changes scope.
    text = re.sub(r'(?:不是|並非|不要|禁止|不得|不需要|無需|不必)\s*(?:再|去|要)?\s*(?:列出|顯示)', '', text)
    text = re.sub(r"\b(?:do not|don't|never|no need to)\s+(?:list (?:the )?files|show files|show directory contents)\b", '', text, flags=re.I)
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
    if tool == 'run_command':
        try:
            context = SecurityContext.from_agent(agent, fallback_workspace=Path.cwd())
            cwd = resolve_command_working_directory(tool_call.get('working_directory'), context, agent)
            info.update(requested_paths=[tool_call.get('working_directory') or str(context.workspace_root)],
                resolved_paths=[str(cwd)],scope_source='working_directory' if 'working_directory' in tool_call else 'workspace_default')
        except (PathSecurityError, ValueError, OSError) as exc:
            info.update(allowed=False,error=str(exc))
        return info
    if tool == 'delete_path':
        requested = str(tool_call.get('path', '') or '')
        info['requested_paths'] = [requested]
        info['scope_source'] = 'absolute_delete_target'
        try:
            context = SecurityContext.from_agent(agent, fallback_workspace=Path.cwd())
            target = context.require_write(requested, require_absolute=True, allow_root=False, forbid_glob=True)
            if target == context.workspace_root or target == context.config.runtime_write_root or target in context.write_roots:
                raise ValueError('authorized_root_delete_forbidden')
            info['resolved_paths'] = [str(target)]
        except (PathSecurityError, ValueError, OSError) as exc:
            info.update(allowed=False, error=str(exc))
        return info
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
        elif tool == 'inspect_project_working_set':
            requested=str(tool_call.get('workspace','') or '').strip()
            if explicit_paths and not requested:raise ValueError('explicit_path_scope_requires_workspace')
            values=tool_call.get('paths')
            if not isinstance(values,list) or not values or not all(isinstance(v,str) and v.strip() and v==v.strip() for v in values):
                raise ValueError('project_working_set_paths_invalid')
            root=Path(_project_workspace(tool_call,agent)).resolve()
            resolved_paths=[];info['requested_paths']=list(values);info['scope_source']='workspace_and_paths'
            for value in values:
                candidate=Path(value)
                resolved=security_context.require_read(str(candidate if candidate.is_absolute() else root/candidate))
                if not is_within(resolved,root):raise ValueError('project_working_set_path_outside_workspace:'+value)
                resolved_paths.append(resolved)
        elif tool in {"list_directory", "find_file"}:
            field = "root" if tool == "find_file" else "path"
            requested = str(tool_call.get(field, "") or "").strip()
            if explicit_paths and not requested:
                raise ValueError("explicit_path_scope_requires_" + field)
            requested = requested or "."
            info["requested_paths"] = [requested]
            resolved_paths = [security_context.require_read(requested)]
            info["scope_source"] = field if requested != "." else "workspace_default"
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
    working_directory: Path | None = None,
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
    # Accept equivalent schema spellings, but never silently downgrade a
    # declared file predicate into a command exit-code-only check.
    step = dict(step)
    primary, alias = step.get("action"), step.get("type")
    if primary is not None and alias is not None and primary != alias:
        return {"label":"invalid_verification_step","action":"invalid","passed":False,
                "spec_valid":False,"error":"verification action/type conflict"}
    action = primary if primary is not None else alias if alias is not None else "run_command"
    if not isinstance(action, str) or action not in {"run_command", "file_exists", "file_contains"}:
        return {"label":"invalid_verification_step","action":"invalid","passed":False,
                "spec_valid":False,"error":"unsupported verification action"}
    if action in {"file_exists", "file_contains"}:
        if not isinstance(step.get("path"), str) or not step["path"].strip() or type(step.get("expect", True)) is not bool:
            return {"label":"invalid_verification_step","action":action,"passed":False,
                    "spec_valid":False,"error":"file verification requires non-empty path and boolean expect"}
    if action == "file_contains":
        if "text" in step and "contains" in step and step["text"] != step["contains"]:
            return {"label":"invalid_verification_step","action":action,"passed":False,
                    "spec_valid":False,"error":"verification text/contains conflict"}
        needle = step.get("text", step.get("contains"))
        if not isinstance(needle, str) or not needle:
            return {"label":"invalid_verification_step","action":action,"passed":False,
                    "spec_valid":False,"error":"file_contains requires non-empty text/contains"}
        step["text"] = needle
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
            cwd=working_directory or (security_context.workspace_root if security_context else None),
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
            candidate = Path(step.get('path',''))
            if working_directory and not candidate.is_absolute(): candidate = working_directory / candidate
            path = security_context.require_read(candidate) if security_context else candidate
        except PathSecurityError as exc:
            evidence["error"] = f"SECURITY_PATH_REJECTED:{exc}"
            return evidence
        try:
            path.stat()
            exists = True
        except (FileNotFoundError, NotADirectoryError):
            exists = False
        except OSError as exc:
            evidence.update({"path":str(path),"exists":None,"error":f"{type(exc).__name__}: {exc}"})
            return evidence
        expected = bool(step.get("expect", True))
        evidence.update({"path": str(path), "exists": exists, "expected": expected, "passed": exists == expected})
        return evidence

    if action == "file_contains":
        try:
            candidate = Path(step.get('path',''))
            if working_directory and not candidate.is_absolute(): candidate = working_directory / candidate
            path = security_context.require_read(candidate) if security_context else candidate
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
    operation: str,
    timeout: int = 30,
    verify: list = None,
    success_criteria: str = "",
    capture_root: str | Path | None = None,
    verifies_action_id: str = "",
    condition_id: str = "",
    expected_failure: dict | None = None,
    expected_verification: dict | None = None,
    *,
    security_context: SecurityContext | None = None,
    approval_manifest: dict | None = None,
    working_directory: str | Path | None = None,
) -> str:
    """Run an action and the Web Planner's explicit post-action verification plan.

    The local runtime never invents success criteria.  It only executes the
    checks supplied by the Planner and reports PASS/FAIL evidence back.
    """
    from .command_operation import (
        COMMAND_RESULT_SCHEMA, build_operation_result, normalize_command_operation,
        operation_terminal_eligible, operation_result_contract,
        result_schema_for_operation,
    )
    operation = normalize_command_operation(operation)
    if expected_verification is not None:
        from .verification_expectation import validate_expectation
        try:
            if expected_failure is not None or operation not in {'MUTATE','BUILD','TEST','VERIFY','GIT_MUTATE'}:
                raise ValueError('expected_verification operation/expected_failure conflict')
            expected_verification = validate_expectation(expected_verification, verify)
        except ValueError as exc:
            from .tool_result_contract import encode_result
            return encode_result('REJECTED','EXPECTED_VERIFICATION_INVALID',tool='run_command',executed=False,detail=str(exc))
    cwd = resolve_command_working_directory(working_directory, security_context)
    from .action_dependencies import command_is_proven_observation
    observation_confirmed = command_is_proven_observation({'operation':operation,'command':command})
    def finish(operation_result):
        from .tool_result_contract import attach_runtime_result
        rejected = bool(main.get('security_rejected'))
        execution = operation_result['execution_status']
        running = main.get('process_state') == 'RUNNING'
        status = 'REJECTED' if rejected else 'RUNNING' if running else execution
        executed = not rejected and (running or main.get('exit_code') is not None)
        return attach_runtime_result('\n'.join(lines), status,
            'COMMAND_REJECTED' if rejected else 'COMMAND_RUNNING' if running else 'COMMAND_EXITED' if executed else 'PROCESS_NOT_STARTED', tool='run_command',
            executed=executed, data={**operation_result,
                'execution_status':status, 'verification_status':'UNVERIFIED' if running else operation_result['verification_status'],
                'terminal_eligible':False if running or rejected else operation_result['terminal_eligible'],
                'working_directory':str(cwd),
                'observation_confirmed':observation_confirmed,
                'exit_code':main.get('exit_code'), 'timed_out':bool(main.get('timed_out')),
                'process_state':'RUNNING' if running else 'EXITED' if executed else 'NOT_STARTED', 'pid':main.get('pid')})
    telemetry = None
    if security_context and security_context.interface_name == "remote":
        telemetry = {"root": security_context.workspace_root, "task_id": security_context.task_id, "request_id": security_context.request_id}
    main = _run_powershell_capture(
        command, timeout, capture_root,
        cwd=cwd,
        telemetry=telemetry, approval_manifest=approval_manifest,
    )
    lines = [
        "[COMMAND_RESULT]",
        f"result_schema: {COMMAND_RESULT_SCHEMA}",
        f"operation_result_schema: {result_schema_for_operation(operation)}",
        "required_result_fields: " + ",".join(operation_result_contract(operation)),
        f"operation: {operation}",
        f"command: {command}",
        f"working_directory: {cwd}",
        'working_directory_source: ' + ('explicit' if working_directory else 'workspace_default'),
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

    if expected_failure is not None:
        spec_valid = isinstance(expected_failure, dict) and bool(expected_failure)
        expectation_evidence = {
            "expected_failure": dict(expected_failure or {}),
            "actual_exit_code": main.get("exit_code"),
            "timed_out": bool(main.get("timed_out")),
            "checks": [],
        }
        expectation_passed = spec_valid and not main.get("timed_out") and main.get("exit_code") not in (0, None)
        exit_codes = (expected_failure or {}).get("exit_codes")
        if exit_codes is not None:
            matched = main.get("exit_code") in exit_codes if isinstance(exit_codes, list) else False
            expectation_evidence["checks"].append({"field": "exit_code", "passed": matched})
            expectation_passed = expectation_passed and matched
        stderr_regex = (expected_failure or {}).get("stderr_regex")
        if stderr_regex is not None:
            try:
                matched = re.search(str(stderr_regex), str(main.get("stderr", "")), re.MULTILINE) is not None
            except re.error as exc:
                matched = False
                spec_valid = False
                expectation_evidence["error"] = f"invalid stderr_regex: {exc}"
            expectation_evidence["checks"].append({"field": "stderr_regex", "passed": matched})
            expectation_passed = expectation_passed and matched
        output_contains = (expected_failure or {}).get("output_contains")
        if output_contains is not None:
            values = output_contains if isinstance(output_contains, list) else [output_contains]
            combined = str(main.get("stdout", "")) + "\n" + str(main.get("stderr", ""))
            matched = bool(values) and all(str(item) in combined for item in values)
            expectation_evidence["checks"].append({"field": "output_contains", "passed": matched})
            expectation_passed = expectation_passed and matched
        expectation_status = (
            "SPEC_INVALID" if not spec_valid else ("PASS" if expectation_passed else "FAIL")
        )
        execution_status = "FAILED" if main.get("timed_out") or main.get("exit_code") not in (0,) else "SUCCEEDED"
        verification_status = "FAIL" if execution_status == "FAILED" else "UNVERIFIED"
        lines.append("VERIFICATION_STATUS: " + verification_status)
        lines.append("EXPECTATION_STATUS: " + expectation_status)
        lines.append("TERMINAL_ELIGIBLE: " + str(expectation_status == "PASS").lower())
        operation_result = build_operation_result(
            operation, command=command, execution_status=execution_status,
            verification_status=verification_status, stdout=main.get("stdout", ""),
            stdout_ref=main.get("stdout_ref", ""), verifies_action_id=verifies_action_id,
            condition_id=condition_id, expectation_status=expectation_status,
            expected_failure=expected_failure, expectation_evidence=expectation_evidence,
        )
        lines.append("[OPERATION_RESULT] " + json.dumps(operation_result, ensure_ascii=False, default=str))
        if expectation_status == "PASS":
            lines.append("預期失敗已被客觀結果證明；原始 FAILED/FAIL 狀態保留，任務契約 assertion 為 PASS。")
        else:
            lines.append("預期失敗契約未符合；不得以負向測試成功結案。")
        return finish(operation_result)

    if main.get("timed_out") or main.get("exit_code") not in (0,):
        verification_status = "FAIL"
        lines.append("VERIFICATION_STATUS: " + verification_status)
        lines.append("TERMINAL_ELIGIBLE: " + str(operation_terminal_eligible(operation, "FAIL")).lower())
        operation_result = build_operation_result(
            operation, command=command, execution_status="FAILED",
            verification_status=verification_status, stdout=main.get("stdout", ""),
            stdout_ref=main.get("stdout_ref", ""), verifies_action_id=verifies_action_id,
            condition_id=condition_id,
        )
        lines.append("[OPERATION_RESULT] " + json.dumps(operation_result, ensure_ascii=False, default=str))
        lines.append("原因: 主指令本身未成功完成；請 Planner 根據 stderr/error 修正後再執行。")
        return finish(operation_result)

    if verify is None:
        checks = []
    elif isinstance(verify, dict):
        checks = [verify]
    elif isinstance(verify, (list, tuple)):
        checks = list(verify)
    else:
        checks = [verify]

    if not checks:
        verification_status = "UNVERIFIED"
        lines.append("VERIFICATION_STATUS: " + verification_status)
        lines.append("TERMINAL_ELIGIBLE: " + str(operation_terminal_eligible(operation, "UNVERIFIED")).lower())
        operation_result = build_operation_result(
            operation, command=command, execution_status="SUCCEEDED",
            verification_status=verification_status, stdout=main.get("stdout", ""),
            stdout_ref=main.get("stdout_ref", ""), verifies_action_id=verifies_action_id,
            condition_id=condition_id,
        )
        lines.append("[OPERATION_RESULT] " + json.dumps(operation_result, ensure_ascii=False, default=str))
        lines.append("原因: Planner 沒有提供 post-run verification actions；不得僅憑指令 exit_code 宣告改動成功。")
        return finish(operation_result)

    lines.append("[POST_RUN_VERIFICATION]")
    if success_criteria:
        lines.append("success_criteria: " + success_criteria)
    all_passed = True
    all_specs_valid = True
    verification_evidence = []
    for idx, step in enumerate(checks, 1):
        ev = _evaluate_verification_step(
            step, default_timeout=timeout, capture_root=capture_root,
            security_context=security_context,
            working_directory=cwd,
        )
        all_specs_valid = all_specs_valid and bool(ev.get("spec_valid", True))
        all_passed = all_passed and bool(ev.get("passed"))
        verification_evidence.append(ev)
        lines.append(f"verify[{idx}]: " + json.dumps(ev, ensure_ascii=False, default=str))
    verification_status = "SPEC_INVALID" if not all_specs_valid else ("PASS" if all_passed else "FAIL")
    expectation_status = ''
    expectation_evidence = {}
    if expected_verification is not None:
        from .verification_expectation import evaluate_expectation
        expectation_status, expectation_evidence = evaluate_expectation(expected_verification, verification_status, verification_evidence)
        lines.append('EXPECTATION_STATUS: ' + expectation_status)
    lines.append("VERIFICATION_STATUS: " + verification_status)
    lines.append("TERMINAL_ELIGIBLE: " + str(operation_terminal_eligible(operation, verification_status)).lower())
    operation_result = build_operation_result(
        operation, command=command, execution_status="SUCCEEDED",
        verification_status=verification_status, stdout=main.get("stdout", ""),
        stdout_ref=main.get("stdout_ref", ""), verification_evidence=verification_evidence,
        verifies_action_id=verifies_action_id,
        condition_id=condition_id,
        expectation_status=expectation_status, expectation_evidence=expectation_evidence,
    )
    if expected_verification is not None:
        operation_result['expected_verification'] = expected_verification
    lines.append("[OPERATION_RESULT] " + json.dumps(operation_result, ensure_ascii=False, default=str))
    if verification_status == "SPEC_INVALID":
        lines.append("驗證規格無效：請 Planner 修正 verify schema/matcher 後，以 verifies_action_id 綁定原 action 重新驗證。")
    elif not all_passed:
        if expectation_status == 'PASS':
            lines.append('負向後置驗證符合事前契約；原 verification_status=FAIL 保留，不要重做或改寫檔案。')
        else:
            lines.append("驗證失敗：請 Planner 讀取上述 evidence，定位問題、修正，再重新執行與驗證。")
    return finish(operation_result)

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

def tool_write_file(path: str, content: str, *, security_context: SecurityContext | None = None, structured: bool = False) -> str:
    from .tool_result_contract import encode_result
    import hashlib
    p=None
    opened=False
    saved=False
    checked=False
    verified=False
    actual=b''
    expected=b''
    def result(status,reason,text):
        if not structured:
            return text
        return encode_result(status,reason,executed=opened,requested_path=path,
            resolved_path=str(p) if p is not None else None,
            data={'text':text,'file_saved':saved,'readback_checked':checked,
                  'bytes_written':len(expected) if saved else None,'readback_bytes':len(actual) if checked else None,
                  'expected_bytes':len(expected),'sha256':hashlib.sha256(actual).hexdigest() if verified else '',
                  'verification_status':'PASS' if verified else 'FAIL' if checked else 'UNVERIFIED',
                  'possible_partial_write':opened and not verified,'terminal_eligible':verified},tool='write_file')
    try:
        if structured and len(content)>4096:
            return result('REJECTED','WRITE_CONTENT_LIMIT','[錯誤] write_file content 超過 4096 字元')
        expected=content.encode('utf-8')
        p = security_context.require_write(path) if security_context else Path(path)
        try:
            metadata=p.stat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(metadata.st_mode):
                return result('REJECTED','NOT_A_FILE',f'[錯誤] 目標不是一般檔案: {path}')
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('w',encoding='utf-8',newline='') as stream:
            opened=True
            stream.write(content)
        saved=True
        with p.open('rb') as stream:
            actual=stream.read(len(expected)+1)
        checked=True
        verified=actual==expected
        if not verified:
            return result('PARTIAL','WRITE_READBACK_MISMATCH',f'[錯誤] 寫入後讀回內容不一致: {path}')
        return result('SUCCEEDED','FILE_WRITTEN_VERIFIED',f'[成功] 寫入 {len(actual)} bytes 到 {path}')
    except PathSecurityError as e:
        return result('REJECTED','PATH_NOT_AUTHORIZED',f'[錯誤] {e}')
    except PermissionError as e:
        return result('PARTIAL' if opened else 'REJECTED','WRITE_ACCESS_DENIED',f'[錯誤] {e}')
    except UnicodeError as e:
        return result('PARTIAL' if opened else 'REJECTED','INVALID_WRITE_ENCODING',f'[錯誤] {e}')
    except Exception as e:
        return result('PARTIAL' if opened else 'FAILED','WRITE_IO_ERROR',f'[錯誤] {e}')

def tool_list_directory(path: str = ".", *, security_context: SecurityContext | None = None, structured: bool = False) -> str:
    from .tool_result_contract import encode_result
    p = None
    items = []
    entries = []
    enumeration_completed = False
    def result(status, reason, text, *, executed=False):
        if not structured:
            return text
        return encode_result(status, reason, executed=executed, requested_path=path,
            resolved_path=str(p) if p is not None else None,
            data={'text':text, 'entries':entries, 'recursive':False,
                  'terminal_eligible':status=='SUCCEEDED'}, tool='list_directory')
    try:
        p = security_context.require_read(path) if security_context else Path(path)
        if not stat.S_ISDIR(p.stat().st_mode):
            return result('REJECTED','NOT_A_DIRECTORY',f'[錯誤] 不是目錄: {path}')
        children = sorted(p.iterdir())
        enumeration_completed = True
        for item in children:
            metadata = item.stat()
            directory = stat.S_ISDIR(metadata.st_mode)
            regular_file = stat.S_ISREG(metadata.st_mode)
            tag = "[DIR] " if directory else "[FILE]"
            size = f" ({metadata.st_size/1024:.1f}KB)" if regular_file else ""
            items.append(f"{tag} {item.name}{size}")
            entries.append({'name':item.name,'kind':'DIRECTORY' if directory else 'FILE' if regular_file else 'OTHER',
                            'size_bytes':metadata.st_size if regular_file else None})
        return result('SUCCEEDED','DIRECTORY_LISTED',"\n".join(items) if items else "(空目錄)",executed=True)
    except FileNotFoundError as e:
        return result('PARTIAL' if enumeration_completed else 'FAILED',
                      'DIRECTORY_LIST_INCOMPLETE' if enumeration_completed else 'DIRECTORY_NOT_FOUND',
                      f'[錯誤] {e}',executed=enumeration_completed)
    except PermissionError as e:
        return result('PARTIAL' if enumeration_completed else 'REJECTED',
                      'DIRECTORY_LIST_INCOMPLETE' if enumeration_completed else 'ACCESS_DENIED',f'[錯誤] {e}',executed=enumeration_completed)
    except PathSecurityError as e:
        return result('REJECTED','PATH_NOT_AUTHORIZED',f'[錯誤] {e}')
    except Exception as e:
        return result('PARTIAL' if enumeration_completed else 'FAILED','DIRECTORY_LIST_INCOMPLETE' if enumeration_completed else 'DIRECTORY_IO_ERROR',
                      f'[錯誤] {e}',executed=enumeration_completed)


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
            elif not stat.S_ISDIR(root.stat().st_mode):
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
                                    metadata = entry.stat(follow_symlinks=False)
                                    report["file_count"] += 1
                                    report["total_bytes"] += int(metadata.st_size)
                                    extensions[Path(entry.name).suffix.lower() or "[no_extension]"] += 1
                                    if len(report["samples"]) < limit:
                                        report["samples"].append({
                                            "path": str(Path(entry.path)), "bytes": int(metadata.st_size),
                                        })
                                except (OSError, PermissionError) as exc:
                                    report["inaccessible_count"] += 1
                                    if len(report["errors"]) < 10:
                                        report["errors"].append(f"{entry.path}: {type(exc).__name__}: {exc}")
                    except (OSError, PermissionError) as exc:
                        report["inaccessible_count"] += 1
                        if len(report["errors"]) < 10:
                            report["errors"].append(f"{current}: {type(exc).__name__}: {exc}")
        except FileNotFoundError:
            report["errors"].append("path_not_found")
        except PermissionError as exc:
            report["inaccessible_count"] += 1
            report["errors"].append(f"PermissionError: {exc}")
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
    from .tool_result_contract import attach_runtime_result, encode_result
    if not isinstance(query, str) or not query.strip():
        return encode_result('REJECTED','SEARCH_QUERY_REQUIRED',tool='web_search',
            data={'network_status':'NOT_ATTEMPTED'})
    response_received = False
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_redirect=1"
        req = urllib.request.Request(url, headers={"User-Agent": "SmartAgent/1.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            raw_response = r.read()
            response_received = True
            data = json.loads(raw_response.decode())
        if not isinstance(data, dict) or not isinstance(data.get('RelatedTopics', []), list):
            raise ValueError('invalid_search_response')
        abstract = data.get("AbstractText", "")
        if not isinstance(abstract, str):
            raise ValueError('invalid_search_abstract')
        results = []
        if abstract:
            results.append({'text':abstract,'url':str(data.get('AbstractURL', '') or ''),
                            'title':str(data.get('Heading', '') or '')})
        # Instant Answer topics can be grouped. Preserve provider text/URLs;
        # bounded output is not a claim of complete web-search coverage.
        pending = list(data.get('RelatedTopics', []))
        visited = 0
        while pending and len(results) < 4 and visited < 100:
            topic = pending.pop(0); visited += 1
            if not isinstance(topic, dict):
                raise ValueError('invalid_search_topic')
            if 'Topics' in topic:
                if not isinstance(topic['Topics'], list):
                    raise ValueError('invalid_search_topic_group')
                pending = topic['Topics'] + pending
            elif topic.get('Text'):
                if not isinstance(topic['Text'], str):
                    raise ValueError('invalid_search_topic_text')
                results.append({'text':topic['Text'],'url':str(topic.get('FirstURL', '') or ''),'title':''})
        return attach_runtime_result(abstract or "\n".join(row['text'] for row in results) or "(無結果)",
            'SUCCEEDED','SEARCH_COMPLETED',tool='web_search',executed=True,
            data={'query':query,'result_count':len(results),'results':results,
                'provider':'duckduckgo_instant_answer','request_url':url,
                'network_status':'RESPONDED','verification_status':'PASS',
                'verification_scope':'PROVIDER_RESPONSE_ONLY'})
    except Exception as e:
        return encode_result('FAILED','SEARCH_RESPONSE_INVALID' if response_received else 'NETWORK_SEARCH_FAILED',
            tool='web_search',executed=True,detail=f'{type(e).__name__}: {e}',
            data={'query':query,'network_status':'RESPONDED' if response_received else 'FAILED'})

import urllib.parse

SKIP_SEARCH_DIRS = {".git", ".svn", "node_modules", "__pycache__", ".idea", ".vs", "build", "dist"}

def tool_find_file(
    name: str,
    root: str = ".",
    max_results: int = 20,
    *,
    security_context: SecurityContext | None = None,
    structured: bool = False,
) -> str:
    """Find files below root by exact name first, then by substring."""
    from .tool_result_contract import encode_result
    base = None
    visited = 0
    exact, partial, errors = [], [], []
    total = 0
    error_count = 0
    excluded_link_count = 0
    scope_exclusions = []
    limit = 20
    def result(status, reason, text, complete=False):
        if not structured:
            return text
        matches = (exact + partial)[:limit]
        return encode_result(status, reason, executed=visited>0,
            requested_path=root,resolved_path=str(base) if base is not None else None,
            data={'text':text,'matches':matches,'result_limit':limit,
                  'truncated':total>len(matches),'total_matches':total,
                  'search_complete':complete,'errors':errors,'error_count':error_count,
                  'scope_exclusions':scope_exclusions,'excluded_link_count':excluded_link_count,
                  'excluded_directory_names':sorted(SKIP_SEARCH_DIRS),
                  'terminal_eligible':status=='SUCCEEDED'},tool='find_file')
    def onerror(exc):
        nonlocal error_count
        error_count += 1
        if len(errors)<10:
            errors.append({'type':type(exc).__name__,'path':str(getattr(exc,'filename','') or ''),'detail':str(exc)})
    try:
        base = security_context.require_read(root) if security_context else Path(root).expanduser().resolve()
        if not stat.S_ISDIR(base.stat().st_mode):
            return result('REJECTED','NOT_A_DIRECTORY',f'[錯誤] 搜尋根路徑不是目錄: {root}')
        query = name.strip().lower()
        if not query:
            return result('REJECTED','SEARCH_NAME_REQUIRED','[錯誤] find_file 缺少 name')
        if any(c in query for c in '*?'):
            return result('REJECTED','UNSUPPORTED_GLOB_PATTERN','[錯誤] find_file 使用精確檔名／substring，不接受 glob；請改用檔名片段')
        limit=int(max_results)
        if limit<1:
            return result('REJECTED','INVALID_RESULT_LIMIT','[錯誤] max_results 必須大於零')
        limit=min(limit,100)
        for current_root, dirs, files in os.walk(base,onerror=onerror,followlinks=False):
            visited += 1
            bounded_dirs = []
            for directory in sorted(dirs):
                if directory.lower() in SKIP_SEARCH_DIRS:
                    continue
                child = Path(current_root) / directory
                try:
                    metadata = child.lstat()
                    # Windows os.walk(followlinks=False) does not exclude
                    # junctions. Prune every reparse directory before descent.
                    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata,'st_file_attributes',0) & 0x400:
                        excluded_link_count += 1
                        if len(scope_exclusions)<10:
                            scope_exclusions.append({'path':str(child),'reason':'LINK_OR_REPARSE_NOT_FOLLOWED'})
                        continue
                    resolved = security_context.require_read(str(child)) if security_context else child.resolve()
                    if not is_within(resolved,base):
                        raise PathSecurityError('search_directory_outside_requested_root')
                    bounded_dirs.append(directory)
                except (PathSecurityError,OSError) as exc:
                    onerror(exc)
            dirs[:] = bounded_dirs
            for filename in sorted(files):
                low = filename.lower()
                if low != query and query not in low:
                    continue
                full = Path(current_root) / filename
                try:
                    resolved = security_context.require_read(str(full)) if security_context else full.resolve()
                    if not is_within(resolved,base):
                        raise PathSecurityError('search_match_outside_requested_root')
                except (PathSecurityError,OSError) as exc:
                    onerror(exc);continue
                total += 1
                bucket = exact if low==query else partial
                if len(bucket)<limit:
                    bucket.append(str(full))
        matches=(exact+partial)[:limit]
        text='\n'.join(matches) if matches else f'[未找到] {name} (root={base})'
        if error_count:
            denied=all(e['type']=='PermissionError' for e in errors)
            return result('PARTIAL' if visited else 'REJECTED' if denied else 'FAILED',
                'SEARCH_INCOMPLETE' if visited else 'SEARCH_ACCESS_DENIED' if denied else 'SEARCH_IO_ERROR',
                '[錯誤] 搜尋未完成；不得將空 matches 當作已證實未找到\n'+text)
        return result('SUCCEEDED','SEARCH_COMPLETED',text,complete=True)
    except FileNotFoundError as e:
        return result('FAILED','SEARCH_ROOT_NOT_FOUND',f'[錯誤] {e}')
    except PermissionError as e:
        onerror(e);return result('REJECTED','SEARCH_ACCESS_DENIED',f'[錯誤] {e}')
    except PathSecurityError as e:
        return result('REJECTED','PATH_NOT_AUTHORIZED',f'[錯誤] {e}')
    except Exception as e:
        onerror(e);return result('PARTIAL' if visited else 'FAILED','SEARCH_IO_ERROR',f'[錯誤] {e}')

def tool_save_session_summary(agent, payload: dict) -> str:
    if not agent or not agent.workspace_root:
        return "[錯誤] 尚未建立 Workspace Root，無法保存 project session summary"
    if not callable(getattr(agent,'save_project_summary',None)):
        from .tool_result_contract import encode_result
        return encode_result('REJECTED','CAPABILITY_UNAVAILABLE',tool='save_session_summary')
    result=agent.save_project_summary(payload)
    from .tool_result_contract import decode_result, attach_runtime_result, encode_result
    if decode_result(result): return result
    target=Path(agent.workspace_root)/'.agents'/'agent_session_summary.md'
    if str(result).startswith('[成功]') and target.is_file():
        import hashlib
        raw=target.read_bytes()
        if str(payload.get('summary','')).encode('utf-8') in raw:
            return attach_runtime_result(result,'SUCCEEDED','SESSION_SUMMARY_SAVED',tool='save_session_summary',
                executed=True,resolved_path=str(target),data={'file_saved':True,'verification_status':'PASS',
                    'size_bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
    return encode_result('FAILED','SUMMARY_NOT_CONFIRMED',tool='save_session_summary',executed=True,data={'text':str(result)})


def _task_plan_request_scope(agent):
    """Identity comes from Runtime, never optional model-owned action fields."""
    epoch = getattr(agent, 'current_task_epoch', '')
    digest = getattr(agent, 'current_intent_digest', '')
    if not epoch or not digest:
        return None
    return dict(request_id=agent.current_request_id, task_id=agent.current_task_id,
                task_epoch=epoch, intent_digest=digest, request_phase='PLANNER_ROUND_PENDING',
                continuation_seq=max(1, int(getattr(agent, '_protocol_turn_seq', 0))))


def _project_workspace(tool_call: dict, agent, *, write: bool = False, request_scoped: bool = False):
    requested=tool_call.get("workspace","")
    default=str(agent.workspace_root) if agent and getattr(agent,"workspace_root",None) else "."
    context = SecurityContext.from_agent(agent, fallback_workspace=Path.cwd())
    candidate = requested or default
    explicit_paths = _explicit_request_paths(agent) if request_scoped else []
    if not requested and explicit_paths:
        if len(explicit_paths) != 1:
            raise ValueError('workspace_scope:explicit_workspace_required')
        candidate = str(explicit_paths[0])
    try:
        authorized = context.require_write(candidate) if write else context.require_read(candidate)
    except PathSecurityError as exc:
        raise ValueError(f"workspace_not_authorized:{candidate}:{exc}") from exc
    if explicit_paths and not any(is_within(authorized, root) for root in explicit_paths):
        raise ValueError('workspace_scope:target_outside_request_scope')
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
    explicit_paths = _explicit_request_paths(agent)
    if not target_value and explicit_paths:
        if len(explicit_paths) != 1:
            raise ValueError("project_sync_root_required:ambiguous_request_scope")
        target_value = str(explicit_paths[0])
    target = Path(
        _project_workspace({"workspace": target_value}, agent, write=True)
    ).resolve()
    if explicit_paths and not any(is_within(target, root) for root in explicit_paths):
        raise ValueError("project_sync_target_outside_request_scope:" + str(target))

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
    explicit_paths = _explicit_request_paths(agent)
    if explicit_paths and not any(is_within(target, root) for root in explicit_paths):
        raise ValueError("query_project_target_outside_request_scope:" + str(target))

    workspace_value = str(tool_call.get("workspace", "") or "").strip()
    if workspace_value:
        boundary = Path(
            _project_workspace({"workspace": workspace_value}, agent, write=False)
        ).resolve()
        if not is_within(target, boundary):
            raise ValueError("query_project_root_conflict:target_outside_workspace")
    return str(target)

def resolve_command_working_directory(value, security_context: SecurityContext | None, agent=None) -> Path:
    context = security_context or SecurityContext.for_workspace(Path.cwd())
    if value is not None and (not isinstance(value, (str,Path)) or not str(value).strip()):
        raise PathSecurityError('command_working_directory_invalid')
    cwd = context.require_write(value or context.workspace_root, require_absolute=True, forbid_glob=True)
    if not cwd.is_dir():
        raise PathSecurityError('command_working_directory_not_directory:' + str(cwd))
    requested = _explicit_request_paths(agent)
    requested = [root.parent if root.is_file() else root for root in requested]
    if requested and not any(is_within(cwd, root) for root in requested):
        raise PathSecurityError('command_working_directory_outside_request_scope:' + str(cwd))
    return cwd


def _execution_approval_manifest(tool_call: dict, agent, security_context: SecurityContext, working_directory=None) -> dict | None:
    command = str(tool_call.get("command", "") or "")
    admission = inspect_command(command, workspace=working_directory or security_context.workspace_root)
    if admission.allowed:
        return None
    if not admission.approval_eligible:
        raise CommandSecurityError(admission.code, admission.detail)
    manifest = build_command_approval_manifest(command, workspace=working_directory or security_context.workspace_root)
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


def _ledger_read_result(tool, reader, workspace, **fields):
    """Record read failures only after invoking the actual read operation."""
    try:
        return json.dumps(reader(workspace, **fields), ensure_ascii=False, separators=(',', ':'))
    except (ValueError, OSError) as exc:
        if str(exc).startswith('project_ledger_workspace_not_directory:'):
            raise
        from .tool_result_contract import encode_result
        return encode_result('FAILED', 'LEDGER_READ_FAILED', tool=tool, executed=True,
            detail=f'{type(exc).__name__}: {exc}',
            data={'execution_confirmed':True,'possible_partial_execution':False})


def execute_tool(tool_call: dict, agent=None, models: dict | None = None) -> str:
    """Common P0 result boundary; original transcripts/business data remain intact."""
    from .smartagent_protocol import normalize_tool_numeric_fields
    tool_call = normalize_tool_numeric_fields(tool_call)
    from .tool_result_contract import decode_result, attach_runtime_result
    from .tool_result_policy import P1_TOOLS, business_payload, structured_outcome
    p2_tools={'web_search','save_session_summary','google_drive_upload','ask_executor'}
    tool = str(tool_call.get('tool', ''))
    if tool == "observe_fact":
        # This read-only tool observes a filesystem fact inside the existing
        # authorization boundary. It never guesses absence from access errors.
        from .evidence_foundation import (
            EvidenceSpecError, validate_observe_fact_action,
            probe_file_exists, evaluate_predicate,
        )
        from .tool_result_contract import encode_result, attach_runtime_result
        try:
            predicate = validate_observe_fact_action(tool_call)
        except EvidenceSpecError as exc:
            return encode_result('REJECTED', 'EVIDENCE_SPEC_INVALID',
                tool=tool, executed=False, detail=str(exc),
                data={'verification_status':'SPEC_INVALID','terminal_eligible':False})
        try:
            security = SecurityContext.from_agent(agent, fallback_workspace=Path.cwd())
            path = security.require_read(tool_call["path"], require_absolute=True,
                                         allow_root=False, forbid_glob=True)
        except (PathSecurityError, ValueError, PermissionError, OSError) as exc:
            return encode_result('REJECTED', 'EVIDENCE_SCOPE_REJECTED',
                tool=tool, executed=False, detail=str(exc),
                data={'verification_status':'UNVERIFIED','terminal_eligible':False})
        fact = probe_file_exists(path, raw_ref=str(tool_call.get('action_id', '')))
        confirmed = fact['observation_status'] == 'CONFIRMED'
        verdict = (evaluate_predicate(predicate, fact) if predicate is not None
                   else {'verification_status':'UNVERIFIED', 'comparison':'NOT_REQUESTED'})
        verification = verdict['verification_status']
        body = {'schema':'SMARTAGENT_FACT_OBSERVATION_RESULT_V1',
                'status':'READY' if confirmed else 'INCOMPLETE',
                'raw_evidence': {'source':'filesystem.stat',
                                 'result':'FOUND' if fact.get('value') is True else
                                          'NOT_FOUND' if fact.get('value') is False else 'UNKNOWN'},
                'observation':fact, 'predicate':predicate, 'evaluation':verdict}
        executed = True  # The authorized, bounded stat probe was attempted.
        data = {'observations':[fact], 'execution_finality':'COMPLETED',
                'verification_status':verification,
                'expectation_status':verification if predicate is not None else '',
                'comparison':verdict['comparison'],
                'terminal_eligible':confirmed and verification not in {'FAIL','UNKNOWN','SPEC_INVALID'},
                'observation_status':fact['observation_status']}
        status = 'SUCCEEDED' if confirmed else 'UNKNOWN'
        reason = ('FACT_OBSERVED' if confirmed and predicate is None else
                  'FACT_PREDICATE_PASSED' if verification=='PASS' else
                  'FACT_PREDICATE_FAILED' if verification=='FAIL' else
                  'FACT_OBSERVATION_UNKNOWN')
        return attach_runtime_result(
            json.dumps(body, ensure_ascii=False, separators=(',', ':')),
            status, reason, tool=tool, executed=executed, data=data,
            requested_path=tool_call['path'], resolved_path=str(path),
            action_id=tool_call.get('action_id', ''),
            request_id=getattr(agent, 'current_request_id', '') if agent else '',
            task_id=getattr(agent, 'current_task_id', '') if agent else '')
    # Validate negative assertions before any query, including direct calls
    # that bypass the WebAgent envelope validator. A malformed expectation
    # must never execute and cannot be retroactively attached to a result.
    expected_outcome = None
    if tool == "query_project" and "expected_outcome" in tool_call:
        from .query_expected_outcome import validate_query_expected_outcome, QueryExpectationError
        try:
            expected_outcome = validate_query_expected_outcome(tool_call)
        except QueryExpectationError as exc:
            from .tool_result_contract import encode_result
            return encode_result(
                'REJECTED', 'QUERY_EXPECTATION_INVALID', tool=tool, executed=False,
                detail=str(exc),
                data={'expectation_status':'SPEC_INVALID','terminal_eligible':False},
            )
    if 'field_refs' in tool_call:
        from .tool_result_contract import encode_result
        return encode_result('REJECTED', 'UNEXPANDED_REQUEST_CONTEXT_REFERENCE',
            tool=tool, executed=False,
            detail='Resolve field_refs through request-bound WebAgent admission before execution.')
    try:
        result = _execute_tool_impl(tool_call, agent, models)
    except (PathSecurityError, ProjectAccessError, PermissionError, FileNotFoundError, ValueError, OSError) as exc:
        if tool not in P1_TOOLS | p2_tools:
            raise
        from .tool_result_contract import encode_result
        # An unexpected exception may follow partial work. Do not invent a
        # successful execution or turn an unknown mutation into a safe retry.
        rejected = isinstance(exc,(PathSecurityError,ProjectAccessError)) or str(exc).startswith((
            'workspace_not_authorized:', 'workspace_not_found_', 'workspace_scope:', 'project_sync_strategy_invalid:',
            'project_sync_root_conflict:', 'project_sync_root_required:',
            'project_sync_target_outside_request_scope:', 'query_project_top_level_query_fields_forbidden:',
            'query_project_project_root_required', 'query_project_root_conflict:',
            'query_project_target_outside_request_scope:',
            'compare_project_snapshot_invalid:', 'compare_project_snapshot_scope:',
            'project_working_set_paths_invalid', 'project_working_set_path_outside_workspace:',
            'project_ledger_workspace_not_directory:',
        ))
        status = 'REJECTED' if rejected else 'UNKNOWN'
        reason = 'PREFLIGHT_REJECTED' if rejected else 'EXECUTION_STATE_UNKNOWN'
        result = encode_result(status, reason, tool=tool,
            requested_path=tool_call.get('path',''),detail=f'{type(exc).__name__}: {exc}',
            data={'execution_confirmed':False,'possible_partial_execution':not rejected})
        return attach_runtime_result(result, status, reason, tool=tool, executed=False,
            requested_path=tool_call.get('path',''),
            data={'execution_confirmed':False,'possible_partial_execution':not rejected},
            action_id=tool_call.get('action_id',''),
            request_id=getattr(agent,'current_request_id','') if agent else '',
            task_id=getattr(agent,'current_task_id','') if agent else '')
    if tool not in {'read_file','write_file','list_directory','find_file','run_command','upload_file','upload_files','return_artifact',
                    'download_artifact','web_edit_file','execute_artifact_bundle',
                    'web_search','save_session_summary','google_drive_upload','ask_executor'} | P1_TOOLS:
        return result
    outcome = decode_result(result)
    if tool=='google_drive_upload' or (tool=='ask_executor' and str(result).startswith(('[LOCAL_AI_FALLBACK_RESULT]', '[LOCAL_AI_EVIDENCE_FAIL]', '[執行引擎失敗]'))):
        # Local model prose can contain lookalike trailers. Only the trusted
        # invocation branch below may produce its Runtime outcome.
        outcome=None
    if outcome:
        status, reason, executed, data = outcome['status'], outcome['reason'], outcome['executed'], outcome.get('data', {})
        if tool in _SCOPED_INSPECTION_TOOLS and str(result).startswith('[TOOL_SCOPE_REJECTED]'):
            reason = 'TOOL_SCOPE_REJECTED'
            data = {'scope':json.loads(str(result).split('] ',1)[1]),'terminal_eligible':False}
        original_tool = outcome.get('tool', tool)
        alias = tool=='upload_file' and original_tool=='upload_files'
        stale_action = outcome.get('action_id') and tool_call.get('action_id') and outcome['action_id'] != tool_call['action_id']
        stale_request = outcome.get('request_id') and agent and outcome['request_id'] != getattr(agent,'current_request_id','')
        if (original_tool != tool and not alias) or stale_action or stale_request:
            status, reason, executed, data = 'UNKNOWN', 'RESULT_IDENTITY_MISMATCH', False, {}
    elif tool in P1_TOOLS:
        status, reason, executed, data = structured_outcome(
            tool, business_payload(result), expected_outcome=expected_outcome,
        )
    else:
        text = str(result)
        data = {}
        rejected = any(marker in text for marker in ('SECURITY_PATH_REJECTED','SECURITY_COMMAND_REJECTED',
            'TELEGRAM_ARTIFACT_REJECTED','WEB_DIRECT_EDIT_UNAVAILABLE'))
        failed = text.startswith(('[錯誤]', '[ARTIFACT_DOWNLOAD_FAILED]', '[WEB_DIRECT_EDIT_FAILED]', '[ARTIFACT_BUNDLE_FAILED]'))
        status, reason, executed = ('REJECTED','PREFLIGHT_REJECTED',False) if rejected else ('FAILED','TOOL_FAILED',False) if failed else ('UNKNOWN','UNCLASSIFIED_PRODUCER_RESULT',False)
        if tool=='execute_artifact_bundle':
            try: payload = json.loads(text)
            except ValueError: payload = {}
            business = payload.get('status', '')
            if business == 'ARTIFACT_BUNDLE_COMPLETED':
                status, reason, executed = 'SUCCEEDED','BUNDLE_COMMITTED',True
            elif business == 'ARTIFACT_BUNDLE_FAILED':
                status, reason, executed = 'FAILED','BUNDLE_FAILED',bool(payload.get('executed'))
            data = {'business_status':business, 'executed_count':payload.get('executed',0),
                'replayed_count':payload.get('replayed',0), 'bundle_id':payload.get('bundle_id',''),
                'failed_action_executed':payload.get('failed_action_executed',False),
                'partial_execution':bool(payload.get('executed') or payload.get('failed_action_executed'))}
            executed = executed or bool(payload.get('failed_action_executed'))
        elif tool=='google_drive_upload':
            if text.startswith('[GOOGLE_DRIVE_UPLOAD_SUCCESS] '):
                payload=json.loads(text.split('] ',1)[1])
                if payload.get('file_id'):
                    status,reason,executed='SUCCEEDED','DRIVE_UPLOAD_CONFIRMED',True
                    data={'delivery_status':'CONFIRMED','remote_file_id':payload['file_id'],'artifact':payload}
            elif text.startswith('[GOOGLE_DRIVE_UPLOAD_REJECTED]'):
                status,reason,executed='REJECTED','DRIVE_PREFLIGHT_REJECTED',False
            elif text.startswith('[GOOGLE_DRIVE_UPLOAD_FAILED]'):
                status,reason,executed='FAILED','DRIVE_UPLOAD_FAILED',True
        elif tool=='ask_executor':
            if text.startswith('[LOCAL_AI_FALLBACK_BLOCKED]'):
                status,reason,executed='REJECTED','FALLBACK_NOT_ADMITTED',False
            elif text.startswith('[LOCAL_AI_FALLBACK_RESULT]'):
                status,reason,executed='SUCCEEDED','EXECUTOR_RESPONSE_RECEIVED',True
                data={'verification_status':'UNVERIFIED','terminal_eligible':False,'machine_actions_executed':False}
            elif text.startswith(('[執行引擎失敗]','[LOCAL_AI_EVIDENCE_FAIL]')):
                status,reason,executed='FAILED','EXECUTOR_RESPONSE_FAILED',True
    # Populate normalized observations from trusted producer receipts. These
    # enrich Planner evidence but never change existing query/command outcomes.
    if tool in {'query_project', 'run_command'}:
        from .evidence_foundation import adapt_project_query, adapt_command_outcome
        raw_ref = str(tool_call.get('action_id', '') or '')
        if tool == 'query_project':
            facts = adapt_project_query(business_payload(result), raw_ref=raw_ref)
        else:
            facts = adapt_command_outcome({'tool':tool, 'data':data}, raw_ref=raw_ref)
        if facts:
            data = {**data, 'observations': facts}
    # The final Runtime trailer is authoritative even if transcript text contains
    # lookalike status markers. JSON results keep their old business fields.
    attached = attach_runtime_result(result, status, reason, tool=tool, executed=executed, data=data,
        requested_path=tool_call.get('path', tool_call.get('output_path','')),
        resolved_path=(outcome or {}).get('resolved_path'), action_id=tool_call.get('action_id',''),
        request_id=getattr(agent,'current_request_id','') if agent else '',
        task_id=getattr(agent,'current_task_id','') if agent else '')
    if tool == 'query_project':
        # max_bytes covers the transmitted result, including Runtime metadata.
        # Reuse query pagination rather than dropping the authoritative envelope.
        from .project_access import _fit_payload, DEFAULT_QUERY_MAX_BYTES, HARD_QUERY_MAX_BYTES
        payload = json.loads(attached)
        limit = max(4096, min(int(tool_call.get('max_bytes') or DEFAULT_QUERY_MAX_BYTES), HARD_QUERY_MAX_BYTES))
        if len(attached.encode('utf-8')) > limit:
            before_count = len(payload.get('results', []))
            payload = _fit_payload(payload, limit - 128)
            if len(payload.get('results', [])) < before_count:
                payload['_runtime_result']['status'] = 'PARTIAL'
                payload['_runtime_result']['reason'] = 'PROJECT_QUERY_RESPONSE_PARTIAL'
                payload['_runtime_result']['data']['terminal_eligible'] = False
            attached = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    return attached


def _execute_tool_impl(tool_call: dict, agent=None, models: dict | None = None) -> str:
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
            cwd = resolve_command_working_directory(tool_call.get('working_directory'), security_context, agent)
            approval_manifest = _execution_approval_manifest(tool_call, agent, security_context, cwd)
        except PathSecurityError as exc:
            from .tool_result_contract import encode_result
            return encode_result('REJECTED','WORKING_DIRECTORY_REJECTED',tool='run_command',executed=False,
                requested_path=tool_call.get('working_directory',''),detail=str(exc))
        except (CommandSecurityError, SecurityApprovalError) as exc:
            if agent:
                agent.last_verification_status = "FAIL"
            return f"[SECURITY_COMMAND_REJECTED] {exc}\nVERIFICATION_STATUS: FAIL"
        result = tool_run_command(
            tool_call.get("command", ""),
            tool_call.get("operation", ""),
            timeout=int(tool_call.get("timeout", 30)),
            verify=tool_call.get("verify") or [],
            success_criteria=tool_call.get("success_criteria", ""),
            capture_root=workspace / ".agents" / "results" / "command_capture",
            verifies_action_id=tool_call.get("verifies_action_id", ""),
            condition_id=tool_call.get("condition_id", ""),
            expected_failure=tool_call.get("expected_failure"),
            expected_verification=tool_call.get('expected_verification'),
            security_context=security_context, approval_manifest=approval_manifest,
            working_directory=tool_call.get('working_directory'),
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
        from .tool_result_contract import encode_result
        path = tool_call.get("path", "")
        try:
            authorized_path = security_context.require_read(path)
        except PathSecurityError as exc:
            return encode_result('REJECTED', 'PATH_NOT_AUTHORIZED', requested_path=path, detail=str(exc), tool=tool)
        # Preflight before any upload route: a missing/non-file target is not
        # an attachment queue success and must not trigger a needless upload.
        try:
            # Path.is_file() suppresses OSError on newer Python; stat preserves
            # the distinction between an inaccessible path and a missing file.
            import stat
            if not stat.S_ISREG(authorized_path.stat().st_mode):
                return encode_result('FAILED', 'NOT_A_FILE',
                    requested_path=path, resolved_path=str(authorized_path), tool=tool)
        except FileNotFoundError as exc:
            return encode_result('FAILED', 'FILE_NOT_FOUND', requested_path=path, detail=str(exc), tool=tool)
        except PermissionError as exc:
            return encode_result('REJECTED', 'ACCESS_DENIED', requested_path=path, detail=str(exc), tool=tool)
        except OSError as exc:
            return encode_result('FAILED', 'READ_PREFLIGHT_IO_ERROR', requested_path=path, detail=str(exc), tool=tool)
        # Preserve the project evidence route, but ordinary bounded UTF-8
        # reads should return content. Binary/large payloads retain explicit
        # attachment staging; QUEUED never means content was delivered.
        if agent and models.get(agent.planner_key, {}).get("provider") == "web_scraper":
            evidence_route = build_evidence_to_action_route(
                [tool_call], getattr(agent, "_authorized_local_paths", ()) or (),
            )
            if evidence_route.get("active"):
                return encode_result('REJECTED', 'EVIDENCE_ROUTE_REQUIRED', requested_path=path,
                    resolved_path=str(authorized_path), data={'route':evidence_route,
                    'text':render_evidence_to_action_guidance(evidence_route), 'content_read':False}, tool=tool)
            try:
                from .project_artifact_policy import classify_project_file, SOURCE_CODE, TEXT_DATA_CONFIG
                if (authorized_path.stat().st_size <= READ_FILE_TEXT_MAX_BYTES
                        and classify_project_file(authorized_path)['classification'] in {SOURCE_CODE, TEXT_DATA_CONFIG}):
                    raw = _read_bounded_bytes(authorized_path)
                    if len(raw) > READ_FILE_TEXT_MAX_BYTES:
                        return encode_result('FAILED', 'READ_SIZE_LIMIT_EXCEEDED_DURING_READ', executed=True,
                            requested_path=path, resolved_path=str(authorized_path), tool=tool)
                    try:
                        content = raw.decode('utf-8')
                    except UnicodeDecodeError:
                        content = None
                    if content is not None and not any(ord(char) < 32 and char not in '\t\r\n' for char in content):
                        return encode_result('SUCCEEDED', 'FILE_READ', executed=True,
                            requested_path=path, resolved_path=str(authorized_path),
                            data={'text':content, 'content_read':True, 'bytes_read':len(raw)}, tool=tool)
            except FileNotFoundError as exc:
                return encode_result('FAILED', 'FILE_NOT_FOUND', requested_path=path, detail=str(exc), tool=tool)
            except PermissionError as exc:
                return encode_result('REJECTED', 'ACCESS_DENIED', requested_path=path, detail=str(exc), tool=tool)
            except OSError as exc:
                return encode_result('FAILED', 'READ_IO_ERROR', executed=True, requested_path=path, detail=str(exc), tool=tool)
            queued = agent.queue_attachments([str(authorized_path)])
            from .tool_result_contract import decode_result
            queue_outcome = decode_result(queued)
            queued_ok = (queue_outcome or {}).get('status') == 'QUEUED' or '[WEBAGENT_ATTACHMENT_QUEUED]' in queued or '[附件已排程' in queued
            return encode_result('QUEUED' if queued_ok else 'FAILED', 'READ_REROUTED_TO_ATTACHMENT' if queued_ok else 'ATTACHMENT_QUEUE_FAILED',
                executed=True, requested_path=path, resolved_path=str(authorized_path),
                data={'text':queued, 'content_read':False, 'delivery_status':'NOT_DELIVERED',
                      'artifacts':((queue_outcome or {}).get('data') or {}).get('artifacts', [])}, tool=tool)
        try:
            if authorized_path.stat().st_size > READ_FILE_TEXT_MAX_BYTES:
                return encode_result('REJECTED', 'READ_SIZE_LIMIT', requested_path=path, tool=tool)
            raw = _read_bounded_bytes(authorized_path)
            if len(raw) > READ_FILE_TEXT_MAX_BYTES:
                return encode_result('FAILED', 'READ_SIZE_LIMIT_EXCEEDED_DURING_READ', executed=True,
                    requested_path=path, resolved_path=str(authorized_path), tool=tool)
            content = raw.decode('utf-8', errors='replace')
            return encode_result('SUCCEEDED', 'FILE_READ', executed=True,
                requested_path=path, resolved_path=str(authorized_path), data={'text':content,
                    'content_read':True, 'bytes_read':len(raw)}, tool=tool)
        except FileNotFoundError as exc:
            return encode_result('FAILED', 'FILE_NOT_FOUND', requested_path=path, detail=str(exc), tool=tool)
        except PermissionError as exc:
            return encode_result('REJECTED', 'ACCESS_DENIED', requested_path=path, detail=str(exc), tool=tool)
        except OSError as exc:
            return encode_result('FAILED', 'READ_IO_ERROR', executed=True, requested_path=path, detail=str(exc), tool=tool)
    elif tool == "write_file":
        from .tool_result_contract import adapt_legacy_result
        result = tool_write_file(
            tool_call.get("path", ""), tool_call.get("content", ""),
            security_context=security_context, structured=True,
        )
        return adapt_legacy_result(tool, result, tool_call.get('path', ''))
    elif tool in {"begin_file_write", "write_file_chunk", "commit_file_write", "abort_file_write"}:
        if not agent:
            return '[CHUNKED_WRITE_FAILED] ' + json.dumps({
                "status": "FAILED", "execution_status": "FAILED",
                "error_code": "agent_instance_missing", "data": {},
            })
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
            return "[CHUNKED_WRITE_FAILED] " + json.dumps(
                manager.failure_result(tool_call, exc), ensure_ascii=False, sort_keys=True,
            )
    elif tool == "list_directory":
        from .tool_result_contract import adapt_legacy_result
        result = tool_list_directory(
            scope["resolved_paths"][0], security_context=security_context, structured=True
        )
        return adapt_legacy_result(tool, result, scope['resolved_paths'][0])
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
        try:
            return json.dumps(inspect_project_scope(workspace,verify_cached_content=True),ensure_ascii=False,separators=(",",":"))
        except ProjectInventoryError as exc:
            from .tool_result_contract import encode_result
            return encode_result('FAILED','PROJECT_SCAN_INCOMPLETE',tool=tool,executed=exc.scan_started,
                requested_path=workspace,resolved_path=workspace,detail=str(exc),
                data={'workspace_root':workspace,'scan_completed':False,'snapshot_id':'',
                      'error_path':exc.path,'error_reason':exc.reason,'terminal_eligible':False})
    elif tool == "inspect_project_working_set":
        workspace=_project_workspace(tool_call,agent)
        return json.dumps(inspect_project_working_set(workspace,tool_call.get("paths",[])),ensure_ascii=False,separators=(",",":"))
    elif tool == "inspect_semantic_map":
        workspace=_project_workspace(tool_call,agent,request_scoped=True)
        try:
            return json.dumps(inspect_semantic_map(workspace,tool_call.get("paths")),ensure_ascii=False,separators=(",",":"))
        except (ValueError, OSError) as exc:
            from .tool_result_contract import encode_result
            rejected=str(exc).startswith(('semantic_map_scope_mismatch:', 'semantic_map_path_outside_workspace:', 'semantic_map_invalid_required_paths'))
            return encode_result('REJECTED' if rejected else 'FAILED',
                'SEMANTIC_MAP_SCOPE_REJECTED' if rejected else 'SEMANTIC_MAP_READ_FAILED',
                tool=tool,executed=not rejected,detail=f'{type(exc).__name__}: {exc}',
                data={'possible_partial_execution':False,'execution_confirmed':not rejected})
    elif tool == "update_semantic_map":
        workspace=_project_workspace(tool_call,agent,write=True,request_scoped=True)
        return json.dumps(update_semantic_map(workspace,tool_call.get("patch",{})),ensure_ascii=False,separators=(",",":"))
    elif tool == "update_semantic_map_file":
        workspace=_project_workspace(tool_call,agent,write=True,request_scoped=True)
        return json.dumps(update_semantic_map_file(workspace,tool_call.get("path",""),tool_call.get("expected_sha256","")),ensure_ascii=False,separators=(",",":"))
    elif tool == "compare_project_snapshot":
        known_snapshot=tool_call.get("known_snapshot")
        if known_snapshot is not None and not isinstance(known_snapshot,(str,dict)):
            raise ValueError("compare_project_snapshot_invalid:known_snapshot_type")
        explicit_paths=_explicit_request_paths(agent)
        scoped_call=dict(tool_call)
        if not scoped_call.get('workspace') and explicit_paths:
            if len(explicit_paths)!=1:
                raise ValueError('compare_project_snapshot_scope:workspace_required')
            scoped_call['workspace']=str(explicit_paths[0])
        workspace=_project_workspace(scoped_call,agent)
        if explicit_paths and not any(is_within(Path(workspace),root) for root in explicit_paths):
            raise ValueError('compare_project_snapshot_scope:target_outside_request_scope')
        if isinstance(known_snapshot,str):
            if not re.fullmatch(r'[0-9a-fA-F]{64}',known_snapshot):
                raise ValueError('compare_project_snapshot_invalid:known_snapshot_id_format')
            known=load_project_snapshot(workspace,known_snapshot)
            if not known:
                raise ValueError('compare_project_snapshot_invalid:known_snapshot_id_not_found')
        else:
            known=known_snapshot
        if known:
            if not isinstance(known.get('files',[]),list) or any(
                not isinstance(row,dict) or not isinstance(row.get('path'),str) or not row['path']
                for row in known.get('files',[])
            ):
                raise ValueError('compare_project_snapshot_invalid:known_snapshot_files')
            if known.get('workspace_root') is not None and not isinstance(known.get('workspace_root'),(str,Path)):
                raise ValueError('compare_project_snapshot_invalid:known_snapshot_root_type')
            if known.get('workspace_root') and Path(known['workspace_root']).resolve()!=Path(workspace):
                raise ValueError('compare_project_snapshot_invalid:known_snapshot_root')
        current=inspect_project_scope(workspace,verify_cached_content=True)
        return json.dumps(compare_project_snapshot(current,known),ensure_ascii=False,separators=(",",":"))
    elif tool == "extract_project_dependencies":
        workspace=_project_workspace(tool_call,agent,request_scoped=True)
        snapshot=inspect_project_scope(workspace,verify_cached_content=True)
        return json.dumps(dependency_evidence(workspace,snapshot.get("files",[])),ensure_ascii=False,separators=(",",":"))
    elif tool == "build_project_bundle":
        workspace=_project_workspace(tool_call,agent,write=True,request_scoped=True)
        context=SecurityContext.from_agent(agent,fallback_workspace=Path.cwd())
        explicit_paths=_explicit_request_paths(agent)
        def bundle_output(value):
            target=Path(value)
            if not target.is_absolute():target=Path(workspace)/target
            resolved=context.require_write(str(target),require_absolute=True,forbid_glob=True)
            if explicit_paths and not any(is_within(resolved,root) for root in explicit_paths):
                raise PathSecurityError('bundle_output_outside_request_scope:'+str(resolved))
            return str(resolved)
        requested_output=tool_call.get('output_dir','')
        output_dir=bundle_output(requested_output) if requested_output else ''
        # Inventory cache is also a write: validate .agents before the scan.
        bundle_output(Path(workspace)/'.agents')
        snapshot=inspect_project_scope(workspace,verify_cached_content=True)
        if not output_dir:output_dir=bundle_output(Path(workspace)/'.agents'/'project_sync'/snapshot['snapshot_id'])
        result=build_source_bundles(workspace,snapshot.get("files",[]),output_dir,max_bytes=int(tool_call.get("max_bytes",500000)),max_files=int(tool_call.get("max_files",50)))
        result["snapshot_id"]=snapshot.get("snapshot_id","")
        return json.dumps(result,ensure_ascii=False,separators=(",",":"))
    elif tool == "build_project_delta":
        workspace=_project_workspace(tool_call,agent,write=True,request_scoped=True)
        # Delta creates inventory caches, parts and snapshot records locally.
        context=SecurityContext.from_agent(agent,fallback_workspace=Path.cwd())
        context.require_write(str(Path(workspace)/'.agents'),require_absolute=True,forbid_glob=True)
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
        return _ledger_read_result(tool, inspect_project_ledger, workspace, limit=int(tool_call.get("limit",20)))
    elif tool == "query_project_history":
        workspace=_project_workspace(tool_call,agent)
        return _ledger_read_result(tool, query_project_history,
                workspace,
                event_types=tool_call.get("event_types",[]),
                snapshot_id=str(tool_call.get("snapshot_id","") or ""),
                path_contains=str(tool_call.get("path_contains","") or ""),
                since=tool_call.get("since"),
                limit=int(tool_call.get("limit",50)),
        )
    elif tool == "validate_edit_plan":
        workspace=_project_workspace(tool_call,agent,request_scoped=True)
        return json.dumps(validate_edit_plan(workspace,tool_call.get("plan",{})),ensure_ascii=False,separators=(",",":"))
    elif tool == "apply_edit_plan":
        workspace=_project_workspace(tool_call,agent,write=True,request_scoped=True)
        context=SecurityContext.from_agent(agent,fallback_workspace=Path.cwd())
        return json.dumps(apply_edit_plan(workspace,tool_call.get("plan",{}),
            authorize_target=lambda target:context.require_write(str(target),require_absolute=True,forbid_glob=True)),ensure_ascii=False,separators=(",",":"))
    elif tool == "aggregate_verification":
        workspace=_project_workspace(tool_call,agent,write=True)
        return json.dumps(run_aggregated_verification(workspace,tool_call.get("commands",[]),timeout=int(tool_call.get("timeout",120))),ensure_ascii=False,separators=(",",":"))
    elif tool == "propose_task_plan":
        workspace=_project_workspace(tool_call,agent,write=True,request_scoped=True)
        request_scope=_task_plan_request_scope(agent)
        return json.dumps(freeze_task_plan(workspace,tool_call.get("plan",{}),request_scope=request_scope),ensure_ascii=False,separators=(",",":"))
    elif tool == "repair_task_plan":
        workspace=_project_workspace(tool_call,agent,write=True,request_scoped=True)
        request_scope=_task_plan_request_scope(agent)
        return json.dumps(repair_task_plan(workspace,tool_call.get("plan",{}),request_scope=request_scope),ensure_ascii=False,separators=(",",":"))
    elif tool == "propose_task_plan_file":
        workspace=_project_workspace(tool_call,agent,write=True,request_scoped=True)
        request_scope=_task_plan_request_scope(agent)
        source=security_context.require_read(tool_call.get("path",""),require_absolute=True,forbid_glob=True)
        return json.dumps(freeze_task_plan_file(workspace,str(source),tool_call.get("expected_sha256",""),request_scope=request_scope),ensure_ascii=False,separators=(",",":"))
    elif tool == "execute_frozen_plan":
        workspace=_project_workspace(tool_call,agent,write=True,request_scoped=True)
        request_scope=_task_plan_request_scope(agent)
        context=SecurityContext.from_agent(agent,fallback_workspace=Path.cwd())
        return json.dumps(execute_frozen_task_plan(workspace,tool_call.get("plan_id",""),timeout=int(tool_call.get("timeout",120)),request_scope=request_scope,
            authorize_target=lambda target:context.require_write(str(target),require_absolute=True,forbid_glob=True)),ensure_ascii=False,separators=(",",":"))
    elif tool == "web_search":
        return tool_web_search(tool_call.get("query", ""))
    elif tool == "find_file":
        from .tool_result_contract import adapt_legacy_result
        root = scope['resolved_paths'][0]
        result = tool_find_file(
            tool_call.get("name", ""), root=root,
            max_results=int(tool_call.get("max_results", 20)),
            security_context=security_context,
            structured=True,
        )
        return adapt_legacy_result(tool, result, root)
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
        if not callable(getattr(agent, 'web_edit_file', None)):
            return '[WEB_DIRECT_EDIT_UNAVAILABLE] provider has no direct-edit capability'
        try:
            source = security_context.require_read(tool_call.get("path", ""))
            output_value = tool_call.get("output_path")
            output = security_context.require_write(output_value) if output_value else None
        except PathSecurityError as exc:
            return f"[WEB_DIRECT_EDIT_FAILED] SECURITY_PATH_REJECTED:{exc}"
        import hashlib
        target = output or source
        def digest_file(path):
            if not path.is_file(): return ''
            with path.open('rb') as handle: return hashlib.file_digest(handle, 'sha256').hexdigest()
        before = digest_file(target)
        try:
            result = agent.web_edit_file(str(source), tool_call.get('instruction',''), str(output) if output else None)
        except Exception as exc:
            result = f'[WEB_DIRECT_EDIT_FAILED] {type(exc).__name__}: {exc}'
        from .tool_result_contract import attach_runtime_result
        after = digest_file(target)
        changed = before != after
        saved = str(result).startswith('[WEB_DIRECT_EDIT_SUCCESS]') and target.is_file() and changed
        return attach_runtime_result(result, 'SUCCEEDED' if saved else 'PARTIAL' if changed else 'FAILED',
            'WEB_EDIT_SAVED' if saved else 'WEB_EDIT_FAILED', tool=tool, executed=True,
            resolved_path=str(target), data={'file_saved':saved,'verification_status':'UNVERIFIED',
                'sha256':after, 'changed':changed, 'before_sha256':before,
                'delivery_status':'NOT_DELIVERED'})
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
        from .tool_result_contract import encode_result, rejection_reason
        def delete_result(status, reason, executed=False, data=None, detail=''):
            return encode_result(status, reason, executed=executed,
                requested_path=tool_call.get('path', ''),
                resolved_path=(data or {}).get('target'), data=data, detail=detail)
        def perform_delete(manifest):
            try:
                return execute_delete_manifest(security_context, manifest), None
            except SafeFileOperationError as exc:
                return None, delete_result('REJECTED', 'ACCESS_DENIED' if rejection_reason(exc) == 'ACCESS_DENIED' else 'TARGET_CHANGED', detail=str(exc))
            except OSError as exc:
                return None, delete_result('FAILED', 'ACCESS_DENIED' if isinstance(exc, PermissionError) else 'FILESYSTEM_ERROR', executed=True, detail=str(exc))
        try:
            manifest = build_delete_manifest(
                security_context,
                tool_call.get("path", ""),
                recursive=bool(tool_call.get("recursive", False)),
            )
        except SafeFileOperationError as exc:
            return delete_result('REJECTED', rejection_reason(exc), detail=str(exc))
        except OSError as exc:
            return delete_result('FAILED', 'ACCESS_DENIED' if isinstance(exc, PermissionError) else 'FILESYSTEM_ERROR', detail=str(exc))
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
            deleted, error = perform_delete(manifest)
            if error:
                return error
            deleted["authorization"] = "PERMANENT_WORKSPACE"
            deleted["workspace_scope"] = authorized_scope
            return delete_result('SUCCEEDED', 'DELETED', executed=True, data=deleted)
        record = ledger.request(
            binding,
            ttl_sec=float(getattr(agent, "security_approval_timeout_sec", 300.0) or 300.0),
        )
        notifier = getattr(agent, "notify_security_approval", None)
        if not callable(notifier) or getattr(agent, "_security_approval_notifier", None) is None:
            return delete_result('REJECTED', 'USER_APPROVAL_REQUIRED', data={**manifest, 'approval_id':record['approval_id']})
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
                deleted, error = perform_delete(manifest)
                return error or delete_result('SUCCEEDED', 'DELETED', executed=True, data=deleted)
            if state in {"REJECTED", "CANCELLED", "EXPIRED", "CONSUMED"}:
                return delete_result('REJECTED', 'USER_APPROVAL_'+state)
            time.sleep(0.5)
        return delete_result('REJECTED', 'USER_APPROVAL_EXPIRED')
    elif tool == "save_session_summary":
        return tool_save_session_summary(agent, tool_call)
    elif tool == "final_response":
        return "[PROTOCOL_ERROR] final_response 是 Planner→CMD 控制訊息，不應進入 execute_tool()。"
    elif tool == "turn_commit":
        return "[PROTOCOL_ERROR] turn_commit 是 ACK 控制訊息，不應進入 execute_tool()。"
    elif tool == "ask_executor":
        if not agent:
            return "[錯誤] Agent 實例遺失，無法呼叫執行引擎"
        if not callable(getattr(agent,'_call',None)):
            from .tool_result_contract import encode_result
            return encode_result('REJECTED','CAPABILITY_UNAVAILABLE',tool=tool,
                data={'machine_actions_executed':False})
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
