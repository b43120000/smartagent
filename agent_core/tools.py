#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared deterministic tool execution and verification primitives."""
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from .chunked_write import ChunkedWriteError, ChunkedWriteManager
from .project_sync import inspect_project_scope, compare_project_snapshot
from .project_bundle import dependency_evidence, build_source_bundles, delta_records, build_project_delta
from .project_sync_message import build_atomic_project_sync
from .edit_plan_contract import validate_edit_plan
from .batch_apply import apply_edit_plan
from .aggregated_verification import run_aggregated_verification

def _run_powershell_capture(command: str, timeout: int = 30) -> dict:
    """Execute PowerShell and return structured evidence instead of only text."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace"
        )
        return {
            "command": command,
            "exit_code": result.returncode,
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "command": command,
            "exit_code": None,
            "stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
            "stderr": (e.stderr or "") if isinstance(e.stderr, str) else "",
            "timed_out": True,
            "error": f"指令超過 {timeout} 秒",
        }
    except Exception as e:
        return {
            "command": command,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "error": str(e),
        }


def _evaluate_verification_step(step: dict, default_timeout: int = 30) -> dict:
    """Mechanically execute Planner-specified verification; no AI decision here."""
    if not isinstance(step, dict):
        return {
            "label": "invalid_verification_step",
            "action": "invalid",
            "passed": False,
            "error": f"verification step must be dict/object; got {type(step).__name__}: {step!r}",
        }
    action = step.get("action", "run_command")
    label = step.get("label", action)
    delay_sec = float(step.get("delay_sec", 0) or 0)
    if delay_sec > 0:
        time.sleep(delay_sec)
    evidence = {"label": label, "action": action, "passed": False}

    if action == "run_command":
        result = _run_powershell_capture(step.get("command", ""), int(step.get("timeout", default_timeout)))
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
        evidence["passed"] = bool(passed)
        return evidence

    if action == "file_exists":
        path = Path(step.get("path", ""))
        exists = path.exists()
        expected = bool(step.get("expect", True))
        evidence.update({"path": str(path), "exists": exists, "expected": expected, "passed": exists == expected})
        return evidence

    if action == "file_contains":
        path = Path(step.get("path", ""))
        needle = str(step.get("text", ""))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            found = needle in text
            expected = bool(step.get("expect", True))
            evidence.update({"path": str(path), "text": needle, "found": found, "expected": expected, "passed": found == expected})
        except Exception as e:
            evidence["error"] = str(e)
        return evidence

    evidence["error"] = f"未知 verification action: {action}"
    return evidence


def tool_run_command(command: str, timeout: int = 30, verify: list = None, success_criteria: str = "") -> str:
    """Run an action and the Web Planner's explicit post-action verification plan.

    The local runtime never invents success criteria.  It only executes the
    checks supplied by the Planner and reports PASS/FAIL evidence back.
    """
    main = _run_powershell_capture(command, timeout)
    lines = [
        "[COMMAND_RESULT]",
        f"command: {command}",
        f"exit_code: {main.get('exit_code')}",
    ]
    if main.get("stdout"):
        lines.append("stdout:\n" + main["stdout"])
    if main.get("stderr"):
        lines.append("stderr:\n" + main["stderr"])
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
    for idx, step in enumerate(checks, 1):
        ev = _evaluate_verification_step(step, default_timeout=timeout)
        all_passed = all_passed and bool(ev.get("passed"))
        lines.append(f"verify[{idx}]: " + json.dumps(ev, ensure_ascii=False, default=str))
    lines.append("VERIFICATION_STATUS: " + ("PASS" if all_passed else "FAIL"))
    if not all_passed:
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

def tool_read_file(path: str) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"[錯誤] 檔案不存在: {path}"
        if p.stat().st_size > 1_000_000:
            return f"[錯誤] 檔案過大"
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[錯誤] {e}"

def tool_write_file(path: str, content: str) -> str:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"[成功] 寫入 {p.stat().st_size} bytes 到 {path}"
    except Exception as e:
        return f"[錯誤] {e}"

def tool_list_directory(path: str = ".") -> str:
    try:
        p = Path(path)
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

def tool_find_file(name: str, root: str = ".", max_results: int = 20) -> str:
    """Find files below root by exact name first, then by substring."""
    try:
        base = Path(root).expanduser().resolve()
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


def _project_workspace(tool_call:dict,agent):
    requested=tool_call.get("workspace","")
    default=str(agent.workspace_root) if agent and getattr(agent,"workspace_root",None) else "."
    candidate=Path(requested or default).expanduser().resolve()
    allowed=list(getattr(agent,"_authorized_local_paths",[]) or []) if agent else []
    if agent and getattr(agent,"workspace_root",None): allowed.append(str(agent.workspace_root))
    if not _path_within_allowed_roots(candidate,allowed): raise ValueError(f"workspace_not_authorized:{candidate}")
    return str(candidate)

def execute_tool(tool_call: dict, agent=None, models: dict | None = None) -> str:
    models = models or getattr(agent, "_models_registry", {}) or {}
    tool = tool_call.get("tool", "")
    if tool == "run_command":
        result = tool_run_command(
            tool_call.get("command", ""),
            timeout=int(tool_call.get("timeout", 30)),
            verify=tool_call.get("verify") or [],
            success_criteria=tool_call.get("success_criteria", ""),
        )
        if agent:
            if "VERIFICATION_STATUS: PASS" in result:
                agent.last_verification_status = "PASS"
            elif "VERIFICATION_STATUS: FAIL" in result:
                agent.last_verification_status = "FAIL"
            else:
                agent.last_verification_status = "UNVERIFIED"
        return result
    elif tool == "read_file":
        path = tool_call.get("path", "")
        # Web Planner mode is attachment-first: if the web brain asks to
        # inspect a file, never paste the file contents back as text. Queue
        # the real file for upload to the SAME web conversation instead.
        if agent and models.get(agent.planner_key, {}).get("provider") == "web_scraper":
            queued = agent.queue_attachments([path])
            return "[Web Planner 模式：read_file 已自動改道為 upload_file，不展開檔案文字]\n" + queued
        return tool_read_file(path)
    elif tool == "write_file":
        return tool_write_file(tool_call.get("path", ""), tool_call.get("content", ""))
    elif tool in {"begin_file_write", "write_file_chunk", "commit_file_write", "abort_file_write"}:
        if not agent:
            return "[CHUNKED_WRITE_FAILED] Agent 實例遺失"
        manager = getattr(agent, "_chunked_write_manager", None)
        if manager is None:
            workspace = Path(getattr(agent, "workspace_root", None) or Path.cwd()).resolve()
            roots = [workspace]
            roots.extend(
                Path(value).expanduser().resolve(strict=False)
                for value in (getattr(agent, "_authorized_local_paths", []) or [])
            )
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
        return tool_list_directory(tool_call.get("path", "."))
    elif tool == "inspect_directory":
        allowed_roots = list(getattr(agent, "_authorized_local_paths", []) or []) if agent else []
        if agent and getattr(agent, "workspace_root", None):
            allowed_roots.append(str(agent.workspace_root))
        return tool_inspect_directory(
            tool_call.get("paths", []),
            allowed_roots=allowed_roots,
            recursive=tool_call.get("recursive", True),
            sample_limit=tool_call.get("sample_limit", 20),
        )
    elif tool == "inspect_project_scope":
        workspace=_project_workspace(tool_call,agent)
        return json.dumps(inspect_project_scope(workspace),ensure_ascii=False,separators=(",",":"))
    elif tool == "compare_project_snapshot":
        current=inspect_project_scope(_project_workspace(tool_call,agent))
        return json.dumps(compare_project_snapshot(current,tool_call.get("known_snapshot")),ensure_ascii=False,separators=(",",":"))
    elif tool == "extract_project_dependencies":
        workspace=_project_workspace(tool_call,agent)
        snapshot=inspect_project_scope(workspace)
        return json.dumps(dependency_evidence(workspace,snapshot.get("files",[])),ensure_ascii=False,separators=(",",":"))
    elif tool == "build_project_bundle":
        workspace=_project_workspace(tool_call,agent)
        snapshot=inspect_project_scope(workspace)
        output_dir=tool_call.get("output_dir","") or str(Path(workspace)/".agents"/"project_sync"/snapshot.get("snapshot_id","unknown"))
        result=build_source_bundles(workspace,snapshot.get("files",[]),output_dir,max_bytes=int(tool_call.get("max_bytes",500000)),max_files=int(tool_call.get("max_files",50)))
        result["snapshot_id"]=snapshot.get("snapshot_id","")
        return json.dumps(result,ensure_ascii=False,separators=(",",":"))
    elif tool == "build_project_delta":
        workspace=_project_workspace(tool_call,agent)
        return json.dumps(build_project_delta(workspace,tool_call.get("base_snapshot_id","")),ensure_ascii=False,separators=(",",":"))
    elif tool == "project_sync":
        workspace=_project_workspace(tool_call,agent)
        result=build_atomic_project_sync(workspace,tool_call.get("strategy","FULL_BUNDLE"),base_snapshot_id=tool_call.get("base_snapshot_id",""),max_bytes=int(tool_call.get("max_bytes",500000)),max_files=int(tool_call.get("max_files",50)))
        if result.get("status")=="READY" and agent:
            paths=[x.get("path","") for x in result.get("source_bundles",[]) if x.get("path")]
            if paths: result["attachment_queue"]=agent.queue_attachments(paths)
        return json.dumps(result,ensure_ascii=False,separators=(",",":"))
    elif tool == "validate_edit_plan":
        workspace=_project_workspace(tool_call,agent)
        return json.dumps(validate_edit_plan(workspace,tool_call.get("plan",{})),ensure_ascii=False,separators=(",",":"))
    elif tool == "apply_edit_plan":
        workspace=_project_workspace(tool_call,agent)
        return json.dumps(apply_edit_plan(workspace,tool_call.get("plan",{})),ensure_ascii=False,separators=(",",":"))
    elif tool == "aggregate_verification":
        workspace=_project_workspace(tool_call,agent)
        return json.dumps(run_aggregated_verification(workspace,tool_call.get("commands",[]),timeout=int(tool_call.get("timeout",120))),ensure_ascii=False,separators=(",",":"))
    elif tool == "web_search":
        return tool_web_search(tool_call.get("query", ""))
    elif tool == "find_file":
        root = tool_call.get("root", "") or (str(agent.workspace_root) if agent and agent.workspace_root else ".")
        return tool_find_file(tool_call.get("name", ""), root=root)
    elif tool == "upload_file":
        if not agent:
            return "[錯誤] Agent 實例遺失，無法排程附件"
        return agent.queue_attachments([tool_call.get("path", "")])
    elif tool == "upload_files":
        if not agent:
            return "[錯誤] Agent 實例遺失，無法排程附件"
        return agent.queue_attachments(tool_call.get("paths", []))
    elif tool == "web_edit_file":
        if not agent:
            return "[WEB_DIRECT_EDIT_FAILED] Agent 實例遺失"
        return agent.web_edit_file(
            tool_call.get("path", ""),
            tool_call.get("instruction", ""),
            tool_call.get("output_path"),
        )
    elif tool == "download_artifact":
        if not agent:
            return "[ARTIFACT_DOWNLOAD_FAILED] Agent 實例遺失"
        return agent.web_download_artifact(
            tool_call.get("output_path", ""),
            expected_filename=tool_call.get("expected_filename", ""),
            timeout=int(tool_call.get("timeout", 45)),
        )
    elif tool == "execute_artifact_bundle":
        if not agent:
            return "[ARTIFACT_BUNDLE_FAILED] Agent instance is required"
        from .artifact_bundle_receiver import ArtifactBundleReceiver
        from .smartagent_protocol import validate_tool_envelope
        receiver = ArtifactBundleReceiver()
        return receiver.execute(
            tool_call.get("path", ""),
            expected_sha256=tool_call.get("expected_sha256", ""),
            validate_action=lambda action: validate_tool_envelope(
                action,
                raw_payload=json.dumps(action, ensure_ascii=False, separators=(",", ":")),
            ),
            execute_action=lambda action: execute_tool(action, agent=agent, models=models),
        )
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
