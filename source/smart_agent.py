#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smart_agent.py — 三層自適應 AI Agent
根據網路環境自動選擇最佳模型策略：

  Tier 1A (最佳) : 網路 + Playwright 已裝
                   決策 → 真實操作 ChatGPT / Gemini 網頁版（繞過 API 費用）
                   執行 → WebGPT

  Tier 1B (次佳) : 網路 + API Key
                   決策 → GPT-4o / Gemini API
                   執行 → WebGPT

  Tier 2 (良好)  : 有網路，無 Key，無 Playwright
                   決策 + 執行 → WebGPT

  Tier 3 (離線)  : 完全無網路
                   決策 + 執行 → 本地模型 (gemma4 / phi4-mini)
"""
import atexit
import io
import json
import hashlib
import os
import subprocess
import sys
import time
import datetime
import threading
import queue as thread_queue
import urllib.request
import uuid
import ssl
from pathlib import Path
from typing import Optional, Tuple, List
import inspect
import re
from agent_core.paths import (
    log_root,
    remote_events_path,
    remote_runtime_log_path,
    remote_tasks_path,
    remote_workers_root,
    runtime_root,
)

# Check Playwright availability (for web scraping)
PLAYWRIGHT_AVAILABLE = False
try:
    import playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass

dummy_except = True
if False:
    pass  # handled above

# Fix Windows console encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass

# ─── Model Configuration ──────────────────────────────────────────────────────

MODELS = {
    "web_chatgpt": {"provider": "web_scraper", "model": "chatgpt", "tier": 1, "type": "web", "desc": "Web ChatGPT"},
    "web_gemini": {"provider": "web_scraper", "model": "gemini", "tier": 1, "type": "web", "desc": "Web Gemini"},
    "web_claude": {"provider": "web_scraper", "model": "claude", "tier": 1, "type": "web", "desc": "Web Claude"},
}

# Web scraper mode flag
USE_WEB_SCRAPER = False
WEB_SCRAPER_SERVICE = "chatgpt"   # "chatgpt", "gemini", or "claude"
_web_scraper_instance = None
# Stage 3: when a conversation-level protocol session has already been
# negotiated, the first user turn must stay lightweight and must not resend
# the entire SmartAgent protocol body.
WEB_PROTOCOL_SESSION_ACTIVE = False
SMARTAGENT_PROTOCOL_NAME = "smart_agent"
# v8 is the only supported model-facing contract; runtime-owned correlation
# metadata is never authored by the model.
_requested_protocol_version = int(os.environ.get("SMARTAGENT_PROTOCOL_VERSION", "8") or 8)
if _requested_protocol_version != 8:
    raise RuntimeError("SMARTAGENT_PROTOCOL_V8_ONLY")
SMARTAGENT_PROTOCOL_VERSION = 8
SMARTAGENT_STAGED_PROTOCOL = str(os.environ.get("SMARTAGENT_STAGED_PROTOCOL", "on")).strip().lower()

# Strategy: 每個 tier 的 planner/executor 選擇
TIER_STRATEGY = {
    1: {
        "planner": "web_chatgpt",
        "executor": "web_chatgpt",
        "operator": "web_chatgpt",
        "label": "Tier 1 [WEB] ChatGPT Web",
        "use_web_scraper": True,
    },
}

WORKSPACE = Path(os.getcwd())

# ─── Network Detection ────────────────────────────────────────────────────────

def _http_check(url: str, timeout: int = 3) -> bool:
    """Test if URL is reachable (any HTTP response = reachable)."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"User-Agent": "SmartAgent/1.0"})
        urllib.request.urlopen(req, timeout=timeout, context=ctx)
        return True
    except urllib.error.HTTPError:
        return True   # HTTP error = server reached = internet works
    except Exception:
        return False

def detect_network_tier() -> Tuple[int, dict]:
    """Detect WebGPT availability for the lightweight web-only runtime."""
    global USE_WEB_SCRAPER
    status = {}
    status["internet"] = _http_check("https://www.google.com")
    status["playwright"] = PLAYWRIGHT_AVAILABLE
    status["chatgpt_reachable"] = _http_check("https://chatgpt.com") if status["internet"] else False
    status["gemini_reachable"] = _http_check("https://gemini.google.com") if status["internet"] else False
    if not status["internet"]:
        print("  [!] SmartAgentv1 lightweight mode requires network access.")
        return 1, status
    preferred = "chatgpt" if status["chatgpt_reachable"] else "gemini"
    if PLAYWRIGHT_AVAILABLE and (status["chatgpt_reachable"] or status["gemini_reachable"]):
        key = f"web_{preferred}"
        status["web_scraper_service"] = preferred
        USE_WEB_SCRAPER = True
        TIER_STRATEGY[1] = {"planner": key, "executor": key, "operator": key, "label": f"Tier 1 [WEB] {preferred} Web", "use_web_scraper": True}
        print(f"  [OK] WebGPT runtime available: {preferred}")
    else:
        print("  [!] Web runtime preflight incomplete; startup will fail closed if the selected conversation cannot open.")
    return 1, status

def call_openai(model: str, messages: list, temperature: float = 0.7) -> str:
    """Call OpenAI API."""
    from openai import OpenAI
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
    )
    return response.choices[0].message.content

def call_gemini(model: str, messages: list, temperature: float = 0.7) -> str:
    """Call Google Gemini API via OpenAI-compatible endpoint."""
    from openai import OpenAI
    client = OpenAI(
        api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
    )
    return response.choices[0].message.content

def _all_image_files(paths: list) -> bool:
    image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    return bool(paths) and all(Path(p).suffix.lower() in image_exts for p in paths)

def _infer_artifact_save_intent(user_text: str) -> bool:
    """Conservative task-level hint for generated media that must be saved.

    This hint only unlocks protocol recovery. The downloader still requires a
    request-scoped fresh artifact, so it can never authorize stale-file reuse.
    """
    text = str(user_text or "")
    generation = bool(re.search(
        r"(?i)(生成|產生|畫|繪製|做一張|建立圖片|create\s+(?:an?\s+)?image|generate\s+(?:an?\s+)?image|render\s+(?:an?\s+)?image)",
        text,
    ))
    save_word = bool(re.search(r"(?i)(下載|儲存|保存|存放|存到|放在|檔名|save|download|export)", text))
    explicit_image_path = bool(re.search(
        r"(?i)(?:[A-Z]:[\\/][^\r\n]*\.(?:png|jpe?g|webp|gif|bmp|avif)|/[^\r\n]*\.(?:png|jpe?g|webp|gif|bmp|avif))",
        text,
    ))
    return bool(generation and (save_word or explicit_image_path))


def call_web_scraper(service: str, messages: list, attachment_paths: Optional[list] = None,
                     protocol_expected: Optional[dict] = None, status_callback=None) -> str:
    """Call ChatGPT or Gemini via Playwright web scraper.

    attachment_paths are real local files queued by SmartAgent tools.  The
    scraper adapter is detected dynamically so this file remains compatible
    with the current image-only implementation and a future generic uploader.
    """
    from agent_core.web_runtime import get_manager

    last_msg = messages[-1]["content"]
    attachment_paths = [os.path.abspath(p) for p in (attachment_paths or []) if os.path.isfile(p)]

    # Backward compatibility: auto-attach image paths only from a genuine human
    # request. Stage 3.1 forbids interpreting internal tool/result/protocol text
    # as attachment intent; otherwise a failed download can cause an old file at
    # the destination path to be silently re-uploaded and mistaken for new output.
    internal_turn = bool(
        "工具執行結果:" in last_msg
        or "RESULT_ID=" in last_msg
        or "[SMARTAGENT_" in last_msg
        or "[SmartAgent protocol]" in last_msg
        or "[AGENT_PROTOCOL_" in last_msg
        or "[AGENT_SESSION_" in last_msg
    )
    if not internal_turn:
        img_pattern = r'([a-zA-Z]:\\[^\s*?"<>|]+\.(?:png|jpg|jpeg|gif|webp)|/[^\s*?"<>|]+\.(?:png|jpg|jpeg|gif|webp))'
        for candidate in re.findall(img_pattern, last_msg, re.IGNORECASE):
            if os.path.isfile(candidate):
                full = os.path.abspath(candidate)
                if full not in attachment_paths:
                    attachment_paths.append(full)


    if len(messages) <= 2 and not WEB_PROTOCOL_SESSION_ACTIVE:
        system_prompt = messages[0]["content"] if messages[0]["role"] == "system" else ""
        prompt = f"以下是你的系統設定與工具說明（這非常重要，請遵守）：\n{system_prompt}\n\n接下來是用戶的請求：\n{last_msg}"
    else:
        # Stage 3: Bootstrap/Session Attach already established the canonical
        # protocol at conversation scope. Only send the current runtime turn.
        prompt = last_msg

    manager = get_manager()
    ask_fn = manager.ask
    params = inspect.signature(ask_fn).parameters
    kwargs = {"new_conversation": False}
    if status_callback is not None and "status_callback" in params:
        kwargs["status_callback"] = status_callback
    if protocol_expected is not None and "protocol_expected" in params:
        kwargs["protocol_expected"] = dict(protocol_expected)

    if attachment_paths:
        # Preferred new interface. agent_core.web_runtime should expose
        # one of these generic names and upload every path through <input file>.
        if "attachment_paths" in params:
            kwargs["attachment_paths"] = attachment_paths
        elif "file_paths" in params:
            kwargs["file_paths"] = attachment_paths
        elif "image_paths" in params and _all_image_files(attachment_paths):
            # Legacy compatibility for the currently known scraper contract.
            kwargs["image_paths"] = attachment_paths
        else:
            raise RuntimeError(
                "agent_core.web_runtime.ask 尚未支援通用附件。"
                "請在 scraper 的 ask() 增加 attachment_paths（或 file_paths）參數；"
                "目前舊版只可相容 image_paths。"
            )

    return ask_fn(service, prompt, **kwargs)

def call_model(model_key: str, messages: list, temperature: float = 0.7,
               attachment_paths: Optional[list] = None, protocol_expected: Optional[dict] = None,
               status_callback=None) -> str:
    """Unified model caller."""
    cfg = MODELS[model_key]
    provider = cfg["provider"]
    model_id = cfg["model"]

    if provider == "openai":
        return call_openai(model_id, messages, temperature)
    elif provider == "gemini":
        return call_gemini(model_id, messages, temperature)
    elif provider == "web_scraper":
        return call_web_scraper(
            model_id, messages, attachment_paths=attachment_paths,
            protocol_expected=protocol_expected, status_callback=status_callback,
        )
    else:
        raise ValueError(f"Unknown provider: {provider}")

# ─── Shared Tool Core ─────────────────────────────────────────────────────────
from agent_core.tools import (
    _run_powershell_capture, _evaluate_verification_step,
    tool_run_command, run_verification_input_self_tests,
    tool_read_file, tool_write_file, tool_list_directory, tool_inspect_directory, tool_web_search,
    tool_find_file, tool_save_session_summary,
    execute_tool as _core_execute_tool,
)

def execute_tool(tool_call: dict, agent=None) -> str:
    """Compatibility facade; implementation lives in agent_core.tools."""
    return _core_execute_tool(tool_call, agent=agent, models=MODELS)

# ─── Shared SmartAgent Protocol Core ──────────────────────────────────────────
from agent_core.smartagent_protocol import (
    TOOL_FENCE, TOOL_ENVELOPE_MAX_BYTES, FINAL_RESPONSE_MAX_BYTES,
    RUN_COMMAND_MAX_CHARS, WRITE_FILE_CONTENT_MAX_CHARS,
    INLINE_SCRIPT_SOFT_CHARS, INLINE_SCRIPT_HARD_CHARS, INLINE_SCRIPT_RISK_THRESHOLD,
    INLINE_EXECUTOR_SPECS, TOOL_ENVELOPE_SCHEMAS, SYSTEM_PROMPT_TEMPLATE,
    _type_name, _matches_type, _diagnostic, _guess_tool_name,
    _decode_tool_candidate_detailed, _decode_tool_candidate,
    _extract_inline_executor, _command_complexity_signals, _validate_run_command_complexity,
    _extract_fenced_tool_payloads, _extract_dom_tool_payloads, _extract_tool_transport,
    _tool_payload_candidates,
    analyze_tool_transport, parse_tool_calls, get_tool_parse_diagnostics,
    format_tool_parse_diagnostics, looks_like_unparsed_tool_call,
    validate_tool_envelope, validate_ack_turn,
    run_tool_parser_self_tests, run_ack_protocol_self_tests,
)
from agent_core.stage_protocol import validate_stage_manifest, StageManifestError
from agent_core.stage_executor import preflight_stage
from agent_core.stage_result import classify_tool_result
from agent_core.result_store import ResultStore
from agent_core.result_exchange import prepare_tool_result
from agent_core.protocol_v8 import (
    ProtocolV8Error,
    V8RequestContext,
    action_digest as v8_action_digest,
    admit_action as v8_admit_action,
    build_result as v8_build_result,
    parse_v8_tool_transport,
    validate_model_commit as v8_validate_model_commit,
)
from agent_core.payload_budget import ROUND_INLINE_MAX_BYTES, utf8_size
from agent_core.attachment_staging import stage_attachments
from agent_core.test_signal_transport import TestEnvelope, TestSignalReceiver
from agent_core.routing import advisory_stage1_context
from agent_core.routing import lazy_context_sync_prompt

# ─── Shared Progress Core ─────────────────────────────────────────────────────
from agent_core.progress import (
    PROGRESS_EVENTS, _normalize_progress_text, _stable_progress_value,
    _fingerprint_progress_value, _compact_path_name, _tool_progress_descriptor,
    _derive_action_title, _progress_signature, _ProgressLoopGuard,
    _set_event_state, run_progress_control_self_tests,
)

# ─── Smart Agent ──────────────────────────────────────────────────────────────

from agent_core.task_state import ActionResultLedger
from agent_core.status_metadata import StatusPublisher
from agent_core.browser_operator import BrowserOperator
from agent_core.issue_recorder import IssueRecorder
from agent_core.task_checkpoint import TaskCheckpointStore
from agent_core.task_telemetry import TaskTelemetry

class SmartAgent:
    def __init__(self, tier: int, strategy: dict):
        self.tier = tier
        self.planner_key = strategy["planner"]
        self.executor_key = strategy["executor"]
        self.operator_key = strategy.get("operator", "web_chatgpt")
        self.planner_model = MODELS[self.planner_key]["model"]
        self.executor_model = MODELS[self.executor_key]["model"]
        self.operator_model = MODELS[self.operator_key]["model"]
        self.tier_label = strategy["label"]
        self._models_registry = MODELS
        self.conversation_history = []
        self.session_start = datetime.datetime.now()
        self.workspace_root: Optional[Path] = None
        self.pending_attachments: List[str] = []
        self._attachment_sha_ledger: set[str] = set()
        self.attached_files: List[str] = []
        self.last_verification_status: Optional[str] = None
        self.session_summary_saved_this_task = False
        self._loaded_summary_for_workspace: Optional[str] = None
        self.current_run_id: Optional[str] = None
        self.web_edit_attempted_this_task = False
        self.web_edit_succeeded_this_task = False
        self.web_edit_failed_this_task = False
        self._emergency_cap = 150
        self._stall_threshold = 3
        self._pause_event = threading.Event()
        self._abort_event = threading.Event()
        self._console_pause_enabled = False
        self._active_iteration = 0
        self._active_action_title = "Idle"
        self._protocol_turn_seq = 0
        self._pending_result_ack_id = ""
        self._pending_web_ack_id = ""
        self._active_protocol_expected: Optional[dict] = None
        self._aborted_protocol_nonces: set[str] = set()
        self._seen_web_ack_ids: set[str] = set()
        self._accepted_web_ack_ids: list[str] = []
        self._task_uploaded_attachments: list[str] = []
        self._action_result_ledger = ActionResultLedger()
        self._artifact_save_expected = False
        self._active_route_context: dict = {}
        self._active_stage_manifest: dict | None = None
        self._stage1_context: dict = {}
        self._result_store = ResultStore(runtime_root() / "results")
        self._v8_admitted_actions: dict[str, dict] = {}
        self._v8_action_results: dict[str, dict] = {}
        self._attachment_session_id = uuid.uuid4().hex
        self.status_publisher = StatusPublisher()
        self.browser_operator = BrowserOperator(self.operator_key, self.operator_model)
        self.issue_recorder = IssueRecorder()
        from agent_core.deferred_repair import DeferredRepairQueue
        self.deferred_repair_queue = DeferredRepairQueue()
        self.checkpoint_store = TaskCheckpointStore()
        self.task_telemetry = TaskTelemetry(log_root() / "telemetry")
        self._system_pause_reason = ""
        self._status_identity: dict = {}
        self.interface_name = "local"
        self.security_approval_chat_id = ""
        self.security_approval_timeout_sec = 300.0
        self.security_approval_ledger_root = Path(__file__).resolve().parent
        self._security_approval_notifier = self._prompt_security_approval

        self.system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            tier_label=self.tier_label,
            planner_model=self.planner_model,
            executor_model=self.executor_model,
        )

    def notify_security_approval(self, record: dict, manifest: dict) -> None:
        self._security_approval_notifier(record, manifest)

    def _prompt_security_approval(self, record: dict, manifest: dict) -> None:
        from agent_core.security_approval import SecurityApprovalLedger
        print("\n[安全確認] Agent 要求刪除：", flush=True)
        print(f"  路徑：{manifest.get('target', '')}", flush=True)
        print(f"  項目：{manifest.get('entry_count', 0)}，大小：{manifest.get('total_bytes', 0)} bytes", flush=True)
        answer = input(f"輸入 {record.get('approval_id')} 才會執行；其他輸入視為拒絕：").strip()
        SecurityApprovalLedger(self.security_approval_ledger_root).decide(
            str(record.get("approval_id", "")),
            approve=answer == str(record.get("approval_id", "")),
            chat_id="", actor="local-console",
        )

    def configure_status(self, **identity) -> None:
        self._status_identity = dict(identity)
        self.status_publisher.configure(**identity)

    def _publish_status(self, stage: str, **kwargs):
        return self.status_publisher.publish(stage, **kwargs)

    def _web_status(self, stage: str, **kwargs) -> None:
        state = "FAILED" if stage == "FAILED" else "RUNNING"
        snapshot = self._publish_status(stage, state=state, actor="WEB_RUNTIME", **kwargs)
        if str(stage).upper() != "UI_ESCALATION_REQUIRED":
            return None
        result = self.browser_operator.handle(
            snapshot,
            model_call=lambda messages: self._call(self.operator_key, messages),
        )
        print(
            f"  [Agent 2] model={self.operator_model} action={result.get('action')} "
            f"reason={result.get('reason')}", flush=True,
        )
        self._publish_status(
            "UI_ESCALATION_RESULT", actor="BROWSER_OPERATOR_2",
            message=f"Agent 2：{result.get('action', 'INSPECT_ONLY')}",
            task_phase=str(snapshot.get("task_phase", "")),
            observed_state=str(snapshot.get("observed_state", "")),
            ui_confidence="CONFIRMED" if result.get("model_response_valid") else "UNCERTAIN",
            primary_method="AGENT2_MODEL", detail=json.dumps(result, ensure_ascii=False),
        )
        if result.get("invoked") and result.get("action") == "SAFE_STOP":
            self._record_issue(
                RuntimeError(f"Agent 2 recovery exhausted: {snapshot.get('error_code', 'UNKNOWN')}"),
                actor="BROWSER_OPERATOR_2", stage="UI_ESCALATION_RESULT",
                error_code=str(snapshot.get("error_code", "")), detail=json.dumps(result, ensure_ascii=False),
            )
        elif result.get("invoked") and result.get("action") in {"PRESS_ESCAPE", "DISMISS_DIALOG", "RETRY_ONCE"}:
            self._record_issue(
                RuntimeError(f"Agent 2 recovered UI issue: {snapshot.get('error_code', 'UNKNOWN')}"),
                actor="BROWSER_OPERATOR_2", stage="UI_ESCALATION_RESULT",
                error_code=str(snapshot.get("error_code", "")), detail=json.dumps(result, ensure_ascii=False),
                classification="LOCALAGENT_RECOVERABLE", disposition="DEFERRED",
            )
        return result

    def _record_issue(self, exc: BaseException, **context):
        """Best-effort Stage 11.1 recording; never replaces the original failure."""
        try:
            issue = self.issue_recorder.record(
                exc, run_id=self.current_run_id or "",
                task_id=str(self._status_identity.get("task_id", "") or ""),
                workspace=str(self.workspace_root or ""),
                iteration=self._active_iteration,
                status=self.status_publisher.snapshot(), **context,
            )
            print(f"  [Agent 2 Issue] {issue['issue_id']} → {issue['document']}", flush=True)
            self._publish_status(
                "ISSUE_DETECTED", actor="BROWSER_OPERATOR_2",
                message=f"已記錄 issue：{issue['issue_id']}",
                error_code=str(context.get("error_code", "")), detail=issue["classification"],
            )
            if issue.get("classification") == "LOCALAGENT_DEFECT" and self.current_run_id:
                try:
                    from agent_core.host_supervisor import (
                        HostSupervisor, SELF_REPAIR_STALLED, META_ACTIVE,
                        META_RESTARTING, META_RESUMING, NEEDS_HUMAN, META_COMPLETED,
                    )
                    supervisor = HostSupervisor()
                    meta = supervisor.load_meta_recovery()
                    if meta.get("state") not in {SELF_REPAIR_STALLED, META_ACTIVE, META_RESTARTING, META_RESUMING, NEEDS_HUMAN}:
                        self.request_system_pause(f"meta_recovery:{issue['issue_id']}")
                        from agent_core.self_repair_coordinator import SelfRepairCoordinator
                        coordinator = SelfRepairCoordinator(checkpoint_store=self.checkpoint_store)
                        active = coordinator.load()
                        if active.get("state") in {"", "COMPLETED", "FAILED", "CANCELLED", NEEDS_HUMAN, META_COMPLETED}:
                            coordinator.transition(
                                SELF_REPAIR_STALLED,
                                issue_id=issue["issue_id"],
                                repair_id="META-" + uuid.uuid4().hex[:12].upper(),
                                run_id=self.current_run_id,
                                recovery_kind="meta_recovery",
                                resume_order=["self_repair", "user_task"],
                            )
                        supervisor.record_self_repair_stall(
                            reason="localagent_defect",
                            detail=f"issue_id={issue['issue_id']}",
                            stage=str(context.get("stage", "LOCAL_AGENT")),
                            evidence={
                                "reason": "localagent_defect",
                                "stage": str(context.get("stage", "LOCAL_AGENT")),
                                "issue": {k: issue.get(k) for k in (
                                    "issue_id","fingerprint","classification","exception_type","message",
                                    "traceback","stage","tool","action_id","error_code","source_file",
                                    "source_line","source_symbol","detail","status"
                                ) if k in issue},
                                "status": self.status_publisher.snapshot(),
                            },
                        )
                except Exception as bridge_exc:
                    print(f"  [Meta Recovery] bridge failed: {type(bridge_exc).__name__}: {bridge_exc}", flush=True)
            return issue
        except Exception as recorder_exc:
            print(f"  [Agent 2 Issue] recorder failed: {type(recorder_exc).__name__}: {recorder_exc}", flush=True)
            return None

    def enable_console_pause(self) -> bool:
        """Enable ESC/R/Q cooperative pause controls for an interactive Windows CLI."""
        enabled = bool(
            sys.platform == "win32"
            and getattr(sys.stdin, "isatty", lambda: False)()
        )
        self._console_pause_enabled = enabled
        if enabled:
            print("[*] 任務執行控制：ESC=暫停，暫停後 R=繼續、Q=中止目前任務")
        return enabled

    def request_pause(self) -> None:
        _set_event_state(self._pause_event, True)

    def request_system_pause(self, reason: str) -> None:
        self._system_pause_reason = str(reason or "system_pause")
        if self.current_run_id:
            self.checkpoint_store.request_pause(self.current_run_id, self._system_pause_reason)
        self.request_pause()

    def resume_task(self) -> None:
        _set_event_state(self._pause_event, False)

    def abort_task(self) -> None:
        self._abort_event.set()
        _set_event_state(self._pause_event, False)
        manager = getattr(self, "_chunked_write_manager", None)
        if manager is not None:
            try:
                manager.abort_open_sessions("agent_cancelled")
            except Exception as exc:
                print(f"  [ChunkedWrite] cancellation cleanup failed: {type(exc).__name__}", flush=True)

    def cancel_current_web_request(self) -> dict:
        """Cooperatively stop only the current Web Planner generation.

        Ctrl+C should not close the persistent ChatGPT browser/session.  The
        scraper first clicks the WebGPT Stop control, then falls back to Escape
        and same-page reload.  Only if those fail may ScraperManager recreate
        the browser context; the cancelled prompt is never resent.
        """
        self.abort_task()
        if self._active_protocol_expected:
            nonce = str(self._active_protocol_expected.get("local_nonce", "") or "")
            if nonce:
                self._aborted_protocol_nonces.add(nonce)

        if MODELS.get(self.planner_key, {}).get("provider") != "web_scraper":
            return {
                "status": "local_abort",
                "stopped": True,
                "restarted": False,
            }

        service = MODELS[self.planner_key]["model"]
        try:
            from agent_core.web_runtime import get_manager
            result = get_manager().cancel_current_generation(
                service,
                restart_on_failure=True,
            )
            if not isinstance(result, dict):
                result = {"status": str(result)}
            return result
        except Exception as exc:
            return {
                "status": "cancel_failed",
                "stopped": False,
                "restarted": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _poll_escape_key(self) -> None:
        """Convert a buffered ESC key into a pause Event at a safe checkpoint."""
        if not self._console_pause_enabled or sys.platform != "win32":
            return
        try:
            import msvcrt
            while msvcrt.kbhit():
                key = msvcrt.getwch()
                if key == "\x1b":
                    self.request_pause()
                    break
                if key in ("\x00", "\xe0") and msvcrt.kbhit():
                    msvcrt.getwch()
        except Exception:
            return

    def _emit_iteration_status(self, iteration: int, action_title: str, status_callback=None) -> None:
        self._active_iteration = int(iteration)
        self._active_action_title = str(action_title or "等待下一步")
        message = (
            f"[Iteration {iteration}] {self._active_action_title} "
            f"| RUN_ID={self.current_run_id}"
        )
        print(f"\n  {message}", flush=True)
        if status_callback:
            try:
                status_callback(message)
            except Exception as e:
                print(f"  [!] status callback failed: {e}", flush=True)

    def _pause_checkpoint(self, iteration: int, action_title: str, status_callback=None) -> bool:
        """Cooperative safe point. False means the user chose Q/abort."""
        if self._abort_event.is_set():
            return False
        if not self._console_pause_enabled:
            return True

        self._poll_escape_key()
        if not self._pause_event.is_set():
            return True

        paused_message = (
            f"[PAUSED] Iteration {iteration} | {action_title} "
            f"| RUN_ID={self.current_run_id} | R=resume Q=abort"
        )
        print(f"\n  {paused_message}", flush=True)
        if status_callback:
            try:
                status_callback(paused_message)
            except Exception as e:
                print(f"  [!] status callback failed: {e}", flush=True)

        try:
            import msvcrt
            while self._pause_event.is_set() and not self._abort_event.is_set():
                if msvcrt.kbhit():
                    key = msvcrt.getwch().lower()
                    if key == "r":
                        self.resume_task()
                        resumed = (
                            f"[RESUMED] Iteration {iteration} | {action_title} "
                            f"| RUN_ID={self.current_run_id}"
                        )
                        print(f"\n  {resumed}", flush=True)
                        if status_callback:
                            try:
                                status_callback(resumed)
                            except Exception:
                                pass
                        return True
                    if key == "q":
                        self.abort_task()
                        break
                    if key in ("\x00", "\xe0") and msvcrt.kbhit():
                        msvcrt.getwch()
                time.sleep(0.05)
        except Exception:
            self.resume_task()
            return True

        return not self._abort_event.is_set()

    def _controlled_stop(self, marker: str, iteration: int, action_title: str, detail: str) -> str:
        message = (
            f"[{marker}] {detail}\n"
            f"Iteration {iteration} | {action_title} | RUN_ID={self.current_run_id}"
        )
        self.conversation_history.append({"role": "assistant", "content": message})
        self.save_project_history()
        self.task_telemetry.event("task_stopped", marker=marker, iteration=iteration)
        self.task_telemetry.save()
        return message


    def _call(self, model_key: str, messages: list, attachment_paths: Optional[list] = None, protocol_expected: Optional[dict] = None) -> str:
        try:
            return call_model(
                model_key, messages, attachment_paths=attachment_paths,
                protocol_expected=protocol_expected, status_callback=self._web_status,
            )
        except Exception as e:
            print(f"\n  [!] {MODELS[model_key]['model']} 失敗: {e}")
            # A web model is the decision authority in web-planner mode.
            # Browser/DOM/timeout failures are transport failures, not a reason
            # to silently hand the decision to a local model. Preserve the
            # selected web conversation and surface the error to the caller.
            if MODELS[model_key].get("provider") == "web_scraper":
                raise
            raise

    def _log_protocol_replay(self, action_id: str, tool_name: str) -> None:
        print(
            f"  [ACK EXACTLY-ONCE] action_id={action_id} tool={tool_name} 已執行過；重用 cached result，不再次執行。",
            flush=True,
        )

    def _new_local_commit(self) -> dict:
        self._protocol_turn_seq += 1
        commit = {
            "run_id": str(self.current_run_id or ""),
            "turn_id": self._protocol_turn_seq,
            "local_nonce": uuid.uuid4().hex,
            "ack_result_id": str(self._pending_result_ack_id or ""),
            # Strict alternating ACK chain: this acknowledges the last WebGPT
            # turn_commit that LocalAgent accepted. First turn is empty.
            "ack_web_ack_id": str(self._pending_web_ack_id or ""),
        }
        if getattr(self, "_request_intent_digest", ""):
            commit["intent_digest"] = self._request_intent_digest
        return commit

    @staticmethod
    def _local_commit_line(expected: dict) -> str:
        payload = {"protocol_name": "smartagent", "protocol_version": 8}
        return "[SMARTAGENT_V8_LOCAL_COMMIT] " + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )

    def _messages_with_local_commit(self, messages: list, expected: dict) -> list:
        prepared = [dict(message) for message in messages]
        if not prepared:
            return prepared
        reminder = (
            "[SMARTAGENT_V8_REQUIRED] 只輸出 smartagent_tool blocks。每個 action/final_response 必須有 "
            "唯一 action_id；最後一個 block 必須是 {\"tool\":\"turn_commit\",\"action_count\":N}。"
            "不要輸出 request_id、task_id、task_epoch、intent_digest、action_digest、result_id、"
            "turn_id、nonce、ACK IDs 或其他 runtime-owned 欄位；runtime 會自行建立並驗證這些欄位。"
            "若收到 FIELD_REPAIR，只補指定欄位，不得修改既有 action 欄位。"
        )
        if SMARTAGENT_STAGED_PROTOCOL in {"shadow", "on"}:
            reminder += (" Protocol v8 execution is runtime-validated: keep one complete stage in trailing turn_commit.stage; "
                         "use SEQUENTIAL + stop_on_error=true, include every action_id once with explicit depends_on, and batch "
                         "project_sync/apply_edit_plan/aggregate_verification rather than requesting files one at a time.")
        prepared[-1]["content"] = str(prepared[-1].get("content", "")) + "\n\n" + reminder + "\n" + self._local_commit_line(expected)
        return prepared

    def _action_signature(self, call: dict) -> str:
        return v8_action_digest(call)

    def _validate_planner_ack(self, tool_calls: list, expected: dict) -> tuple[list, list[dict]]:
        return self._validate_v8_planner_ack(tool_calls, expected)
        self._active_stage_manifest = None
        raw_commit = tool_calls[-1] if tool_calls and tool_calls[-1].get("tool") == "turn_commit" else {}
        actions, commit, diagnostics = validate_ack_turn(tool_calls, expected)
        nonce = str(expected.get("local_nonce", ""))
        if nonce and nonce in self._aborted_protocol_nonces:
            diagnostics.append(_diagnostic(
                "[SMARTAGENT_ACK_REJECTED]", "aborted_turn", tool="turn_commit",
                detail=f"local_nonce={nonce} was invalidated by cancel/abort",
                suggestion="不要執行這個 delayed response；開始新的 turn。",
            ))
            actions = []

        web_ack_id = str((commit or {}).get("web_ack_id", "") or "").strip()
        if not diagnostics and web_ack_id:
            if web_ack_id in self._seen_web_ack_ids:
                diagnostics.append(_diagnostic(
                    "[SMARTAGENT_ACK_REJECTED]", "replayed_web_ack_id", tool="turn_commit",
                    detail=f"web_ack_id={web_ack_id} was already accepted in this RUN_ID",
                    suggestion="每個 WebGPT turn 必須產生新的 web_ack_id；不得重播舊 ACK。",
                ))
                actions = []
            else:
                self._seen_web_ack_ids.add(web_ack_id)
                self._accepted_web_ack_ids.append(web_ack_id)
                # This is LocalAgent's acceptance of the Web ACK.  The next
                # Local Commit carries it back as ack_web_ack_id, completing the
                # strict WebACK -> LocalACK -> WebACK chain.
                self._pending_web_ack_id = web_ack_id
                # The current turn_commit has now acknowledged the previous
                # Local RESULT_ID.  Do not require the same result ACK again in
                # the next Local Commit; a newly executed action will install a
                # fresh RESULT_ID later in this iteration.
                self._pending_result_ack_id = ""
        stage = raw_commit.get("stage") if isinstance(raw_commit, dict) else None
        v7_mutations = {"update_semantic_map", "update_semantic_map_file", "propose_task_plan", "repair_task_plan", "propose_task_plan_file", "execute_frozen_plan", "apply_edit_plan", "aggregate_verification"}
        if not diagnostics and SMARTAGENT_PROTOCOL_VERSION >= 7 and SMARTAGENT_STAGED_PROTOCOL == "on":
            if any(str(action.get("tool", "")) == "apply_edit_plan" for action in actions):
                diagnostics.append(_diagnostic("[SMARTAGENT_STAGE_REJECTED]", "v7_requires_frozen_plan", tool="apply_edit_plan", detail="direct apply_edit_plan is disabled in v7 on mode", suggestion="先凍結 TASK_PLAN_V1，再使用 execute_frozen_plan。"))
                actions = []
            elif any(str(action.get("tool", "")) in v7_mutations for action in actions) and stage is None:
                diagnostics.append(_diagnostic("[SMARTAGENT_STAGE_REJECTED]", "v7_mutation_requires_stage", tool="turn_commit", detail="v7 mutation has no stage manifest", suggestion="在 turn_commit.stage 宣告完整 CONTEXT_SYNC / EXECUTE_VERIFY / REPAIR stage。"))
                actions = []
        if stage is not None and not diagnostics:
            if SMARTAGENT_STAGED_PROTOCOL in {"off", "shadow"}:
                # Compatibility modes must never reject or alter v5 actions.
                try:
                    candidate = validate_stage_manifest(stage, actions)
                    self.task_telemetry.event("stage_shadow", would_pass=True, stage_id=candidate.get("stage_id", ""))
                except StageManifestError as exc:
                    self.task_telemetry.event("stage_shadow", would_pass=False, diagnostic=str(exc)[:240])
            elif SMARTAGENT_PROTOCOL_VERSION < 6:
                diagnostics.append(_diagnostic("[SMARTAGENT_STAGE_REJECTED]", "stage_requires_protocol_v6", tool="turn_commit", detail="SMARTAGENT_PROTOCOL_VERSION must be >= 6", suggestion="使用 v5 ACK 不帶 stage，或明確啟用 protocol v6。"))
                actions = []
            else:
                try:
                    self._active_stage_manifest = preflight_stage(stage, actions, workspace=str(self.workspace_root or ""))
                    self.checkpoint_store.admit_stage(self.current_run_id, self._active_stage_manifest, actions)
                except (StageManifestError, ValueError) as exc:
                    diagnostics.append(_diagnostic("[SMARTAGENT_STAGE_REJECTED]", "stage_admission_failed", tool="turn_commit", detail=str(exc), suggestion="修正完整 stage manifest 後重送；本輪不會執行 action。"))
                    actions = []
        return actions, diagnostics

    def _v8_request_context(self, expected: dict) -> V8RequestContext:
        return V8RequestContext(
            request_id=str(expected.get("request_id") or self.current_run_id or "REQ-UNKNOWN"),
            task_id=str(expected.get("task_id") or self._status_identity.get("task_id") or self.current_run_id or "TASK-UNKNOWN"),
            task_epoch=str(expected.get("task_epoch") or self.current_run_id or "EPOCH-UNKNOWN"),
            intent_digest=str(expected.get("intent_digest") or getattr(self, "_request_intent_digest", "") or "intent-unknown"),
        )

    def _validate_v8_planner_ack(self, tool_calls: list, expected: dict) -> tuple[list, list[dict]]:
        """Admit the compact v8 model response and synthesize runtime metadata."""
        diagnostics: list[dict] = []
        self._active_stage_manifest = None
        if not tool_calls or tool_calls[-1].get("tool") != "turn_commit":
            diagnostics.append(_diagnostic(
                "[SMARTAGENT_V8_REJECTED]", "missing_turn_commit", tool="turn_commit",
                detail="v8 response must end with a compact turn_commit",
                suggestion="最後輸出 {\"tool\":\"turn_commit\",\"action_count\":N}。",
            ))
            return [], diagnostics

        actions = list(tool_calls[:-1])
        try:
            v8_validate_model_commit(tool_calls[-1], len(actions))
        except ProtocolV8Error as exc:
            diagnostics.append(_diagnostic(
                "[SMARTAGENT_V8_REJECTED]", exc.code, tool="turn_commit",
                detail=exc.detail, suggestion="只修正 compact v8 turn_commit，不要重做已提供的 action。",
            ))
            return [], diagnostics

        context = self._v8_request_context(expected)
        admitted: dict[str, dict] = {}
        for index, action in enumerate(actions, 1):
            try:
                record = v8_admit_action(action, context)
            except ProtocolV8Error as exc:
                diagnostics.append(_diagnostic(
                    "[SMARTAGENT_V8_REJECTED]", exc.code,
                    tool=str(action.get("tool", "")),
                    detail=(
                        f"action_index={index};action_id={action.get('action_id', '')};{exc.detail}"
                    ),
                    suggestion="只補缺少的決策欄位；不要修改已提供欄位。",
                    block_index=index,
                ))
                continue
            action_id = record["action_id"]
            if action_id in admitted:
                diagnostics.append(_diagnostic(
                    "[SMARTAGENT_V8_REJECTED]", "duplicate_action_id",
                    tool=record["tool"], detail=f"action_id={action_id}", block_index=index,
                ))
                continue
            admitted[action_id] = record

        if diagnostics:
            return [], diagnostics

        self._v8_admitted_actions = admitted
        # The compact model commit is accepted as an ACK for the current
        # runtime-owned turn. Web ACK tokens stay internal to LocalAgent.
        self._pending_result_ack_id = ""
        self._pending_web_ack_id = "WEBACK-V8-" + uuid.uuid4().hex[:16].upper()
        return actions, []

    def set_workspace_root(self, path: str) -> dict:
        """Explicitly set Workspace Root from Web UI or another trusted caller.

        The path must resolve to an existing directory.  This public method is
        intentionally deterministic: it does not create directories and does not
        infer/trim prose.  The existing _detect_workspace() behavior remains as
        a fallback for user messages that mention a local path.
        """
        raw = str(path or "").strip()
        if not raw:
            return {
                "success": False,
                "error": "workspace path is empty",
                "workspace_root": str(self.workspace_root) if self.workspace_root else None,
            }

        candidate = Path(raw).expanduser()
        try:
            resolved = candidate.resolve()
        except Exception as e:
            return {
                "success": False,
                "error": f"workspace path resolve failed: {e}",
                "workspace_root": str(self.workspace_root) if self.workspace_root else None,
            }

        if not resolved.exists():
            return {
                "success": False,
                "error": f"workspace directory does not exist: {resolved}",
                "workspace_root": str(self.workspace_root) if self.workspace_root else None,
            }

        if not resolved.is_dir():
            return {
                "success": False,
                "error": f"workspace path is not a directory: {resolved}",
                "workspace_root": str(self.workspace_root) if self.workspace_root else None,
            }

        changed = self.workspace_root != resolved
        self.workspace_root = resolved
        # Workspace-link mode keeps the same WebGPT conversation as the primary
        # continuity channel.  Do not auto-upload agent_session_summary.md.

        return {
            "success": True,
            "changed": changed,
            "workspace_root": str(self.workspace_root),
        }

    def _detect_workspace(self, text: str) -> None:
        """Remember a local directory mentioned by the user as Workspace Root."""
        # Windows absolute paths; quoted paths with spaces are supported.
        candidates = re.findall(r'"([A-Za-z]:\\[^"\r\n]+)"|([A-Za-z]:\\[^\r\n]+)', text)
        flat = []
        for a, b in candidates:
            raw = (a or b).strip().rstrip(' .,;')
            # Unquoted match may include prose after the path. Try the full text
            # first, then progressively trim trailing words.
            flat.append(raw)
        for raw in flat:
            probes = [raw]
            parts = raw.split()
            probes.extend(' '.join(parts[:i]) for i in range(len(parts)-1, 0, -1))
            for candidate in probes:
                p = Path(candidate)
                if p.exists() and p.is_dir():
                    resolved = p.resolve()
                    changed = self.workspace_root != resolved
                    self.workspace_root = resolved
                    # Do not auto-upload agent_session_summary.md; linked WebGPT
                    # conversation/history is the normal continuity mechanism.
                    return

    def _agents_dir(self) -> Optional[Path]:
        if not self.workspace_root:
            return None
        d = self.workspace_root / ".agents"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_existing_project_summary(self) -> None:
        """Queue prior summary once when entering a workspace."""
        d = self._agents_dir()
        if not d:
            return
        summary = d / "agent_session_summary.md"
        key = str(summary.resolve())
        if summary.exists() and self._loaded_summary_for_workspace != key:
            self.queue_attachments([str(summary)])
            self._loaded_summary_for_workspace = key

    def save_project_history(self) -> None:
        """Persist exact Planner↔Agent history continuously for audit/recovery."""
        d = self._agents_dir()
        if not d:
            return
        payload = {
            "updated_at": datetime.datetime.now().isoformat(),
            "session_start": self.session_start.isoformat(),
            "tier": self.tier,
            "planner": self.planner_model,
            "executor": self.executor_model,
            "operator": self.operator_model,
            "workspace_root": str(self.workspace_root),
            "attached_files": self.attached_files,
            "last_verification_status": self.last_verification_status,
            "messages": self.conversation_history,
        }
        (d / "agent_session_history.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def save_project_summary(self, payload: dict) -> str:
        """Write Planner-authored semantic context for the next work session."""
        d = self._agents_dir()
        if not d:
            return "[錯誤] 無 Workspace Root"
        def bullets(value):
            if value is None:
                return "- (none)"
            if isinstance(value, str):
                value = [value]
            return "\n".join(f"- {x}" for x in value) if value else "- (none)"
        content = f"""# SmartAgent Project Session Summary

Updated: {datetime.datetime.now().isoformat(timespec='seconds')}
Workspace: `{self.workspace_root}`
Planner: `{self.planner_model}`
Executor: `{self.executor_model}`

## Summary
{payload.get('summary', '').strip() or '(none)'}

## Important decisions
{bullets(payload.get('decisions'))}

## Modified / relevant files
{bullets(payload.get('modified_files'))}

## Verification evidence
{bullets(payload.get('verification'))}

## Pending / risks
{bullets(payload.get('pending'))}

## Next steps
{bullets(payload.get('next_steps'))}
"""
        path = d / "agent_session_summary.md"
        path.write_text(content, encoding="utf-8")
        self.session_summary_saved_this_task = True
        self.save_project_history()
        return f"[成功] 已保存 project session summary: {path}"

    def queue_attachments(self, paths: list, trace_id: str = "") -> str:
        """Adapt LocalAgent identity/status to shared attachment staging."""
        if not isinstance(paths, list):
            paths = [paths]

        queued, duplicates, errors = [], [], []
        cache_base = self.workspace_root if self.workspace_root else Path.cwd()
        conversation_id = str(getattr(self, "conversation_id", "") or "local")
        request_id = str(trace_id or self.current_run_id or uuid.uuid4().hex)

        for ordinal, raw in enumerate(paths, start=1):
            if not raw:
                continue
            try:
                item = stage_attachments(
                    cache_base,
                    conversation_id,
                    self._attachment_session_id,
                    request_id,
                    [raw],
                    ordinal_start=ordinal,
                )[0]
                self.task_telemetry.inc("attachment_cache_hit_count" if item.cache_hit else "attachment_cache_miss_count")
                self.task_telemetry.event("attachment_cache", hit=item.cache_hit, sha256=item.sha256, bytes=item.size_bytes)
            except Exception as e:
                errors.append(f"附件暫存失敗: {raw}: {e}")
                self.task_telemetry.event("attachment_staging_failed", error=type(e).__name__)
                self.task_telemetry.save()
                continue
            if item.sha256 in self._attachment_sha_ledger:
                duplicates.append((item.source_path, item.sha256))
                continue
            self._attachment_sha_ledger.add(item.sha256)
            self.pending_attachments.append(item.staged_path)
            queued.append(item)

        lines = ["[附件已排程，將在下一次 Web Planner 呼叫真正上傳]"]
        lines += [
            f"+ 原始: {item.source_path} -> 上傳: {item.upload_name} SHA256={item.sha256}"
            for item in queued
        ]
        lines += [f"= 內容重複略過: {src} SHA256={digest}" for src, digest in duplicates]
        lines += [f"! {x}" for x in errors]
        if not queued and not errors:
            lines.append("(沒有可排程的附件)")
        return "\n".join(lines)

    def web_download_artifact(self, output_path: str, expected_filename: str = "", timeout: int = 45) -> str:
        """Download the latest WebGPT-produced file/image through shared artifact core."""
        if MODELS.get(self.planner_key, {}).get("provider") != "web_scraper":
            return f"[ARTIFACT_DOWNLOAD_UNAVAILABLE] RUN_ID={self.current_run_id} 目前 Planner 不是 Web Scraper。"
        if not output_path:
            return "[ARTIFACT_DOWNLOAD_FAILED] download_artifact 需要 output_path。"
        try:
            from agent_core.web_runtime import get_manager
            service = MODELS[self.planner_key]["model"]
            result = get_manager().download_latest_artifact(
                service, output_path, expected_filename=expected_filename, timeout_sec=float(timeout)
            )
        except Exception as exc:
            return f"[ARTIFACT_DOWNLOAD_FAILED] RUN_ID={self.current_run_id} {type(exc).__name__}: {exc}"

        status = str((result or {}).get("status", "ARTIFACT_DOWNLOAD_FAILED"))
        if status == "ARTIFACT_DOWNLOAD_SUCCESS":
            return (
                f"[ARTIFACT_DOWNLOAD_SUCCESS] RUN_ID={self.current_run_id} "
                f"path={(result or {}).get('path', '')}\n"
                f"method={(result or {}).get('method', '')}\n"
                f"size={(result or {}).get('size', 0)}\n"
                f"sha256={(result or {}).get('sha256', '')}"
            )
        return (
            f"[ARTIFACT_DOWNLOAD_FAILED] RUN_ID={self.current_run_id} "
            f"{(result or {}).get('error', '未知下載錯誤')}"
        )

    def run_project_sync_transaction(self, plan: dict, *, project_root=None) -> dict:
        """Supply LocalAgent's WebGPT transport to the shared sync runner."""
        from agent_core.project_sync_runner import run_project_sync_transaction

        def send(prompt: str, attachments: list[str]) -> str:
            return self._call(
                self.planner_key,
                [{"role": "user", "content": prompt}],
                attachment_paths=attachments,
                protocol_expected=None,
            )

        from agent_core.project_sync_receiver import bind_receiver_transport, browser_receiver_identity
        from agent_core.web_runtime import get_manager

        config = MODELS.get(self.planner_key, {})
        if config.get("provider") == "web_scraper":
            scraper = get_manager().get_or_create(config["model"])
            identity_provider = lambda: browser_receiver_identity(scraper)
        else:
            identity_provider = lambda: str(getattr(self, "conversation_id", "") or "local")
        identity, bound_send = bind_receiver_transport(send, identity_provider)
        return run_project_sync_transaction(
            project_root or self.workspace_root or Path.cwd(),
            plan,
            bound_send,
            interface_name="local",
            conversation_id=identity,
            session_id=self._attachment_session_id,
            request_id=str(getattr(self, "current_request_id", "") or getattr(self, "current_run_id", "")),
        )

    def web_edit_file(self, path: str, instruction: str, output_path: Optional[str] = None) -> str:
        """Ask the selected web planner to directly edit one uploaded file.

        The original file is not replaced until the web UI produces a non-empty
        downloadable artifact. Capability failures are returned to the Planner so
        it can fall back to the existing JSON/Executor implementation path.
        """
        self.web_edit_attempted_this_task = True
        if MODELS.get(self.planner_key, {}).get("provider") != "web_scraper":
            self.web_edit_failed_this_task = True
            return f"[WEB_DIRECT_EDIT_UNAVAILABLE] RUN_ID={self.current_run_id} 目前 Planner 不是 Web Scraper。"
        if not path or not instruction:
            return "[WEB_DIRECT_EDIT_UNAVAILABLE] web_edit_file 需要 path 與 instruction。"

        source = Path(path).expanduser()
        if not source.is_absolute() and self.workspace_root:
            source = self.workspace_root / source
        try:
            source = source.resolve()
        except Exception:
            source = source.absolute()

        if not source.exists() or not source.is_file():
            return f"[WEB_DIRECT_EDIT_UNAVAILABLE] 找不到可修改檔案: {source}"

        target = Path(output_path).expanduser() if output_path else source
        if not target.is_absolute() and self.workspace_root:
            target = self.workspace_root / target
        try:
            target = target.resolve()
        except Exception:
            target = target.absolute()

        try:
            from agent_core.web_runtime import get_manager
            service = MODELS[self.planner_key]["model"]
            result = get_manager().edit_file(
                service,
                str(source),
                instruction,
                output_path=str(target),
                run_id=self.current_run_id,
            )
        except Exception as e:
            self.web_edit_failed_this_task = True
            return f"[WEB_DIRECT_EDIT_FAILED] RUN_ID={self.current_run_id} {e}"

        status = str((result or {}).get("status", "WEB_DIRECT_EDIT_FAILED"))
        detail = (result or {}).get("error") or (result or {}).get("message") or ""
        if status == "WEB_DIRECT_EDIT_SUCCESS":
            self.web_edit_succeeded_this_task = True
            self.web_edit_failed_this_task = False
            actual = (result or {}).get("download_path", str(target))
            before_hash = (result or {}).get("source_hash_before", "")
            after_hash = (result or {}).get("output_hash_after", "")
            artifact_hash = (result or {}).get("artifact_hash", "")
            return (
                f"[WEB_DIRECT_EDIT_SUCCESS] RUN_ID={self.current_run_id} 網頁模型已直接修改並下載檔案: {actual}\n"
                f"SOURCE_HASH_BEFORE={before_hash}\nARTIFACT_HASH={artifact_hash}\nOUTPUT_HASH_AFTER={after_hash}"
            )
        self.web_edit_failed_this_task = True
        if status == "WEB_DIRECT_EDIT_UNAVAILABLE":
            return f"[WEB_DIRECT_EDIT_UNAVAILABLE] RUN_ID={self.current_run_id} {detail or '最新 assistant 回應沒有可下載的完整修改後檔案。'}"
        return f"[WEB_DIRECT_EDIT_FAILED] RUN_ID={self.current_run_id} {detail or '未知錯誤'}"

    def chat(self, user_input: str, status_callback=None) -> str:
        self.session_summary_saved_this_task = False
        self.last_verification_status = None
        self.current_run_id = "SA-" + uuid.uuid4().hex[:10].upper()
        from agent_core.request_ownership import digest_intent
        self._request_intent_digest = digest_intent(user_input)
        self.task_telemetry.begin(self.current_run_id)
        self.checkpoint_store.begin_task(
            self.current_run_id, goal=user_input, workspace=str(self.workspace_root or ""),
            route_context=self._active_route_context,
        )
        self.status_publisher.configure(run_id=self.current_run_id)
        self._publish_status("LOCAL_PREPARING", message="LocalAgent 準備任務中")
        self.web_edit_attempted_this_task = False
        self.web_edit_succeeded_this_task = False
        self.web_edit_failed_this_task = False
        self._abort_event.clear()
        self._pause_event.clear()
        self._active_iteration = 0
        self._active_action_title = "等待 Planner 決策"
        self._protocol_turn_seq = 0
        self._pending_result_ack_id = ""
        self._pending_web_ack_id = ""
        self._active_protocol_expected = None
        self._aborted_protocol_nonces.clear()
        self._seen_web_ack_ids.clear()
        self._accepted_web_ack_ids.clear()
        self._v8_admitted_actions.clear()
        self._v8_action_results.clear()
        self._task_uploaded_attachments.clear()
        self._attachment_sha_ledger.clear()
        self._action_result_ledger.clear()
        self._artifact_save_expected = _infer_artifact_save_intent(user_input)
        # Local enforcement for Tool Gateway reads: WebGPT may only inspect
        # absolute Windows paths the human explicitly supplied in this turn.
        self._authorized_local_paths = [
            match.group(0).strip().rstrip('.,;，；。')
            for match in re.finditer(r"[A-Za-z]:[\\/][^\r\n]+", user_input)
        ]

        self._detect_workspace(user_input)
        # Stage 1A remains local by default: it informs telemetry and local
        # admission only. Sending workspace metadata to a web planner requires
        # a separate explicit user-authorized sync action.
        if SMARTAGENT_STAGED_PROTOCOL in {"shadow", "on"}:
            self._stage1_context = advisory_stage1_context(user_input, self.workspace_root, include_snapshot=False)
            self.task_telemetry.event("stage1_context", task_size=self._stage1_context.get("task_size"))
            self.task_telemetry.inc("stage_count")
        else:
            self._stage1_context = {}
        workspace_note = ""
        if self.workspace_root:
            workspace_note = f"\n\n[SmartAgent Workspace Root]\n{self.workspace_root}\n[SmartAgent RUN_ID]\n{self.current_run_id}"
            if SMARTAGENT_STAGED_PROTOCOL in {"shadow", "on"}:
                workspace_note += "\n\n" + lazy_context_sync_prompt(
                    user_input,
                    self.workspace_root,
                    requested_strategy=(
                        self._active_route_context.get("context_sync", {}).get(
                            "selected_strategy", "AUTO"
                        )
                        if self._active_route_context else "AUTO"
                    ),
                )
                self.task_telemetry.event(
                    "context_sync_deferred",
                    context_state="NOT_LOADED",
                    selection_owner="WEBGPT",
                )
        else:
            workspace_note = f"\n\n[SmartAgent RUN_ID]\n{self.current_run_id}"
        route_note = ""
        if self._active_route_context:
            from agent_core.routing import format_route_context
            route_note = (
                "\n\n" + format_route_context(
                    self._active_route_context,
                    heading="SmartAgent Trusted Route Context",
                )
                + "\n此區塊由本機 Router 產生；請沿用現有 SmartAgent Tool Envelope 執行流程，"
                  "並在 final_response 簡短標示本輪 Mode 與 Carrier。"
            )
        self.conversation_history.append({
            "role": "user",
            "content": user_input + route_note + workspace_note,
        })
        self.save_project_history()

        progress_guard = _ProgressLoopGuard(self._stall_threshold)

        for iteration in range(1, self._emergency_cap + 1):
            action_title = _derive_action_title(branch="WAIT_PLANNER")
            self._emit_iteration_status(iteration, action_title, status_callback)

            # Safe point before the Planner call.
            if not self._pause_checkpoint(iteration, action_title, status_callback):
                return self._controlled_stop(
                    "ABORTED", iteration, action_title, "使用者已中止目前任務。"
                )

            messages = [{"role": "system", "content": self.system_prompt}] + self.conversation_history
            protocol_expected = self._new_local_commit()
            # Runtime-only hint; _local_commit_line() excludes this field, so
            # the SmartAgent ACK wire format is unchanged.
            protocol_expected["artifact_save_expected"] = bool(self._artifact_save_expected)
            self._active_protocol_expected = protocol_expected
            planner_messages = self._messages_with_local_commit(messages, protocol_expected)
            attachments = list(self.pending_attachments)
            planner_started = time.perf_counter()
            self.task_telemetry.inc("web_round_trip_count")
            self.task_telemetry.event("planner_request_start", iteration=iteration, attachment_count=len(attachments))
            try:
                response_text = self._call(
                    self.planner_key,
                    planner_messages,
                    attachment_paths=attachments or None,
                    protocol_expected=(
                        protocol_expected
                        if MODELS.get(self.planner_key, {}).get("provider") == "web_scraper"
                        else None
                    ),
                )
            except Exception as exc:
                self.task_telemetry.event("planner_failed", iteration=iteration, error=type(exc).__name__)
                self.task_telemetry.save()
                raise
            finally:
                # A failed upload/planner call must never leak into the next
                # turn. Browser-side cleanup is owned by WebRuntime; this owns
                # the local pending queue/cache half of the transaction.
                if attachments:
                    self.pending_attachments.clear()
                    for attachment in attachments:
                        try:
                            cached = Path(attachment)
                            cached.unlink(missing_ok=True)
                            cached.parent.rmdir()
                        except OSError:
                            pass
            self.task_telemetry.event("planner_response_end", iteration=iteration, duration_ms=round((time.perf_counter()-planner_started)*1000,3), response_chars=len(response_text or ""))
            self._publish_status("VALIDATING_PROTOCOL", message="驗證 SmartAgent 協議")
            self._active_protocol_expected = None
            if attachments:
                self.attached_files.extend(x for x in attachments if x not in self.attached_files)
                self._task_uploaded_attachments.extend(
                    x for x in attachments if x not in self._task_uploaded_attachments
                )

            response_probe = response_text or ""
            response_head = repr(response_probe[:500])
            if SMARTAGENT_PROTOCOL_VERSION >= 8:
                v8_calls, v8_diagnostics = parse_v8_tool_transport(response_probe)
                parse_report = {
                    "calls": v8_calls,
                    "diagnostics": [
                        _diagnostic(
                            "[SMARTAGENT_V8_PARSE_ERROR]",
                            item.get("reason", "v8_parse_error"),
                            detail=item.get("detail", ""),
                        )
                        for item in v8_diagnostics
                    ],
                }
            else:
                parse_report = analyze_tool_transport(response_probe)
            tool_calls = parse_report["calls"]
            parse_diagnostics = list(parse_report["diagnostics"])
            if not parse_diagnostics and (
                SMARTAGENT_PROTOCOL_VERSION >= 8
                or MODELS.get(self.planner_key, {}).get("provider") == "web_scraper"
            ):
                tool_calls, ack_diagnostics = self._validate_planner_ack(tool_calls, protocol_expected)
                parse_diagnostics.extend(ack_diagnostics)
            unparsed_hint = bool(parse_diagnostics)

            print(
                f"\n  [PLANNER RESPONSE TRACE] RUN_ID={self.current_run_id} "
                f"chars={len(response_probe)} head={response_head}",
                flush=True,
            )
            parsed_tool_names = [str(call.get("tool", "")) for call in tool_calls]
            print(
                f"  [PARSER TRACE] tool_calls={len(tool_calls)} "
                f"tools={parsed_tool_names} "
                f"looks_like_unparsed={unparsed_hint} "
                f"diagnostics={len(parse_diagnostics)}",
                flush=True,
            )

            # final_response is a protocol control message, not a local action.
            # It must be the sole valid envelope in the Planner response.
            if len(tool_calls) == 1 and tool_calls[0].get("tool") == "final_response":
                action_title = _derive_action_title(branch="FINAL_RESPONSE")
                self._emit_iteration_status(iteration, action_title, status_callback)
                print(
                    f"  [BRANCH TRACE] RUN_ID={self.current_run_id} branch=FINAL_RESPONSE",
                    flush=True,
                )

                if self.last_verification_status in ("FAIL", "UNVERIFIED"):
                    branch_name = "VERIFICATION_GATE"
                    action_title = _derive_action_title(branch=branch_name)
                    self._emit_iteration_status(iteration, action_title, status_callback)
                    gate_evidence = {"status": self.last_verification_status}
                    signature = _progress_signature(
                        action_title,
                        branch=branch_name,
                        result_evidence=gate_evidence,
                    )
                    if progress_guard.record(signature):
                        return self._controlled_stop(
                            "STALLED",
                            iteration,
                            action_title,
                            (
                                f"連續 {self._stall_threshold} 輪沒有新的 verification evidence；"
                                f"狀態持續為 {self.last_verification_status}。"
                            ),
                        )
                    self.conversation_history.append({"role": "assistant", "content": response_text})
                    self.conversation_history.append({
                        "role": "user",
                        "content": (
                            f"[SmartAgent verification gate] 最近一次 run_command 的 VERIFICATION_STATUS="
                            f"{self.last_verification_status}。目前不能 final_response 宣告完成；"
                            "請只輸出必要的修正/驗證 action envelope，直到 PASS，之後再單獨輸出 final_response。"
                        ),
                    })
                    self.save_project_history()
                    if not self._pause_checkpoint(iteration, action_title, status_callback):
                        return self._controlled_stop(
                            "ABORTED", iteration, action_title, "使用者已中止目前任務。"
                        )
                    continue

                final_content = str(tool_calls[0].get("content", ""))
                from agent_core.request_ownership import release_active_request
                release_active_request(self.current_run_id)
                self._pending_result_ack_id = ""
                self.conversation_history.append({"role": "assistant", "content": response_text})
                self.save_project_history()
                deferred = self.deferred_repair_queue.finalize_run(self.current_run_id)
                self.task_telemetry.event("task_completed", iteration=iteration)
                self.task_telemetry.save()
                if deferred.get("count", 0):
                    print(f"  [Agent 2 Issue] deferred bundle: {deferred.get('bundle')}", flush=True)
                return final_content

            if tool_calls:
                action_title = _derive_action_title(tool_calls)
                self._emit_iteration_status(iteration, action_title, status_callback)
                print(
                    f"  [BRANCH TRACE] RUN_ID={self.current_run_id} branch=EXECUTE_TOOLS",
                    flush=True,
                )
                tool_names = [str(call.get("tool", "")) for call in tool_calls]
                parser_msg = (
                    f"[Iteration {iteration}] AGENT parser 已收到 {len(tool_calls)} 個 Tool Envelope："
                    + ", ".join(tool_names)
                )
                print(
                    f"\n  [AGENT PARSER] RUN_ID={self.current_run_id} "
                    f"收到 {len(tool_calls)} 個 tool: {', '.join(tool_names)}",
                    flush=True,
                )
                if status_callback:
                    try:
                        status_callback(parser_msg)
                    except Exception as e:
                        print(f"  [!] status callback failed: {e}", flush=True)

            if not tool_calls:
                # Parser/validator retry branches also participate in stall
                # detection so a malformed transport cannot run to the cap.
                if unparsed_hint:
                    diagnostic_text = format_tool_parse_diagnostics(parse_diagnostics)
                    has_json_error = any(
                        d.get("marker") == "[TOOL_ENVELOPE_PARSE_ERROR]"
                        for d in parse_diagnostics
                    )
                    branch_name = (
                        "MALFORMED_TOOL_RETRY"
                        if has_json_error
                        else "REJECTED_TOOL_RETRY"
                    )
                    action_title = _derive_action_title(branch=branch_name)
                    self._emit_iteration_status(iteration, action_title, status_callback)
                    print(
                        f"  [BRANCH TRACE] RUN_ID={self.current_run_id} branch={branch_name}",
                        flush=True,
                    )
                    print(
                        f"\n  [AGENT PARSER FAIL] RUN_ID={self.current_run_id}\n{diagnostic_text}",
                        flush=True,
                    )

                    signature = _progress_signature(
                        action_title,
                        branch=branch_name,
                        result_evidence=diagnostic_text,
                        diagnostics=parse_diagnostics,
                    )
                    if progress_guard.record(signature):
                        return self._controlled_stop(
                            "STALLED",
                            iteration,
                            action_title,
                            (
                                f"連續 {self._stall_threshold} 輪沒有新的 state/evidence progress；"
                                "相同 parser diagnostic 持續重複。"
                            ),
                        )

                    if status_callback:
                        try:
                            status_callback(
                                f"[Iteration {iteration}] AGENT 已 deterministic 拒絕不合法的 "
                                "Tool Envelope；正在要求 Planner 保留原決策，只修正 transport/schema/complexity。"
                            )
                        except Exception as e:
                            print(f"  [!] status callback failed: {e}", flush=True)

                    self.conversation_history.append({"role": "assistant", "content": response_text})
                    oversized_route = ""
                    if "write_file_content_too_large" in diagnostic_text:
                        oversized_route = (
                            "\n[SMARTAGENT_OVERSIZED_WRITE_ROUTE] 原決策若是建立大型新檔，必須改成 "
                            "begin_file_write → 多輪 sequential write_file_chunk → commit_file_write；"
                            "不得再重送 oversized write_file。只提供各段 content；chunk index/offset/size/hash "
                            "與完整 size/hash 均由 LocalAgent 依實際 UTF-8 bytes 計算。每輪仍須 matching turn_commit。"
                        )
                    v8_repair_route = ""
                    if SMARTAGENT_PROTOCOL_VERSION >= 8 and any(
                        d.get("reason") == "MISSING_OR_INVALID_FIELD" for d in parse_diagnostics
                    ):
                        v8_repair_route = (
                            "\n[SMARTAGENT_V8_FIELD_REPAIR] 這是局部欄位修正；只補 diagnostic 指定的缺失欄位，"
                            "保留原 tool、action_id 與所有已提供欄位，不得改寫原 action。補齊後仍須輸出最後的 "
                            "{\"tool\":\"turn_commit\",\"action_count\":N}。"
                        )
                    self.conversation_history.append({
                        "role": "user",
                        "content": (
                            "[SmartAgent] 你剛才的 Tool Envelope 未通過 deterministic parser/validator，"
                            "因此本輪沒有執行任何 tool。請不要改變原本任務決策，只修正 transport/schema/ACK/complexity "
                            "後重送同一個或等價的短小 ```smartagent_tool``` envelope；"
                            "若任務其實已完成，請改成單獨的 final_response envelope；"
                            "整個回覆不得包含其他文字，Windows 路徑優先使用 C:/...。\n"
                            + diagnostic_text + oversized_route + v8_repair_route
                        )
                    })
                    self.save_project_history()

                    if not self._pause_checkpoint(iteration, action_title, status_callback):
                        return self._controlled_stop(
                            "ABORTED", iteration, action_title, "使用者已中止目前任務。"
                        )
                    continue

                # Plain prose is no longer a valid Planner transport.  Preserve
                # the Planner's decision and ask only for protocol re-packaging.
                if self.last_verification_status in ("FAIL", "UNVERIFIED"):
                    branch_name = "VERIFICATION_GATE"
                    action_title = _derive_action_title(branch=branch_name)
                    self._emit_iteration_status(iteration, action_title, status_callback)
                    gate_evidence = {"status": self.last_verification_status}
                    signature = _progress_signature(
                        action_title,
                        branch=branch_name,
                        result_evidence=gate_evidence,
                    )
                    if progress_guard.record(signature):
                        return self._controlled_stop(
                            "STALLED",
                            iteration,
                            action_title,
                            (
                                f"連續 {self._stall_threshold} 輪沒有新的 verification evidence；"
                                f"狀態持續為 {self.last_verification_status}。"
                            ),
                        )
                    self.conversation_history.append({"role": "assistant", "content": response_text})
                    self.conversation_history.append({
                        "role": "user",
                        "content": (
                            f"[SmartAgent verification gate] 最近一次 run_command 的 VERIFICATION_STATUS="
                            f"{self.last_verification_status}。請只輸出必要修正/驗證的 smartagent_tool action envelope；"
                            "只有 PASS 後才能單獨輸出 final_response。不得直接輸出普通文字。"
                        ),
                    })
                    self.save_project_history()
                    if not self._pause_checkpoint(iteration, action_title, status_callback):
                        return self._controlled_stop(
                            "ABORTED", iteration, action_title, "使用者已中止目前任務。"
                        )
                    continue

                branch_name = "FINAL_RESPONSE_PROTOCOL_RETRY"
                action_title = "將一般回覆封裝為 final_response"
                self._emit_iteration_status(iteration, action_title, status_callback)
                print(
                    f"  [BRANCH TRACE] RUN_ID={self.current_run_id} branch={branch_name}",
                    flush=True,
                )
                signature = _progress_signature(
                    action_title,
                    branch=branch_name,
                    result_evidence=response_text,
                )
                if progress_guard.record(signature):
                    return self._controlled_stop(
                        "STALLED",
                        iteration,
                        action_title,
                        (
                            f"連續 {self._stall_threshold} 輪 Planner 都未使用 SmartAgent transport；"
                            "無法取得合法 final_response/action envelope。"
                        ),
                    )
                self.conversation_history.append({"role": "assistant", "content": response_text})
                self.conversation_history.append({
                    "role": "user",
                    "content": (
                        "[SmartAgent protocol] 你剛才直接輸出了普通文字，但 WebGPT↔LocalAgent 現在只允許 "
                        "smartagent_tool transport。請保持原本決策與語意不變：如果那段文字就是要給使用者的最終回覆，"
                        "請把它封裝成且只輸出一個 ```smartagent_tool```："
                        "{\"tool\":\"final_response\",\"content\":\"原本回覆內容\"}。"
                        "如果原本其實要執行本機 action，則只輸出對應 action envelope。區塊外不得有其他文字。"
                    ),
                })
                self.save_project_history()
                if not self._pause_checkpoint(iteration, action_title, status_callback):
                    return self._controlled_stop(
                        "ABORTED", iteration, action_title, "使用者已中止目前任務。"
                    )
                continue

            # Execute tools.  Stall detection happens only after actual
            # result/evidence exists, replacing the old pre-execution breaker.
            tool_results = []
            raw_results = []
            active_stage = self._active_stage_manifest if (SMARTAGENT_PROTOCOL_VERSION >= 6 and SMARTAGENT_STAGED_PROTOCOL == "on") else None
            stage_outcomes = {}
            stage_details = {}
            stage_dependencies = {
                item["action_id"]: list(item.get("depends_on", []))
                for item in (active_stage or {}).get("actions", [])
            }
            if active_stage:
                self.task_telemetry.event("stage_admitted", stage_id=active_stage["stage_id"], seq=active_stage["seq"], task_size=active_stage["task_size"])

            for idx, call in enumerate(tool_calls, 1):
                tool_name = str(call.get("tool", "") or "(unknown)")
                tool_action_title = _derive_action_title([call])

                # Safe point immediately before tool execution.
                if not self._pause_checkpoint(iteration, tool_action_title, status_callback):
                    return self._controlled_stop(
                        "ABORTED", iteration, tool_action_title, "使用者已中止目前任務。"
                    )

                receive_msg = (
                    f"[Iteration {iteration}] AGENT 已收到 Tool Envelope "
                    f"[{idx}/{len(tool_calls)}]：{tool_action_title}"
                )
                print(
                    f"\n  [AGENT 收到] RUN_ID={self.current_run_id} "
                    f"{tool_name} | {tool_action_title}",
                    flush=True,
                )
                if status_callback:
                    try:
                        status_callback(receive_msg)
                    except Exception as e:
                        print(f"  [!] status callback failed: {e}", flush=True)

                if status_callback:
                    try:
                        status_callback(
                            f"[Iteration {iteration}] AGENT 現在執行：{tool_action_title}"
                        )
                    except Exception as e:
                        print(f"  [!] status callback failed: {e}", flush=True)

                action_id = str(call.get("action_id", "") or "").strip()
                if SMARTAGENT_PROTOCOL_VERSION >= 8:
                    admitted = self._v8_admitted_actions.get(action_id)
                    if not admitted or admitted.get("action_digest") != v8_action_digest(call):
                        return self._controlled_stop(
                            "PROTOCOL_VIOLATION", iteration, tool_action_title,
                            f"v8 action admission mismatch: action_id={action_id}",
                        )
                if active_stage:
                    unmet = [dep for dep in stage_dependencies.get(action_id, []) if stage_outcomes.get(dep) != "COMMITTED"]
                    failed = any(status == "FAILED" for status in stage_outcomes.values())
                    if unmet or (active_stage["stop_on_error"] and failed):
                        skipped = {"status": "SKIPPED_DEPENDENCY", "depends_on": unmet or ["stage_fail_stop"]}
                        stage_outcomes[action_id] = "SKIPPED_DEPENDENCY"
                        stage_details[action_id] = skipped
                        raw_results.append(skipped)
                        tool_results.append(f"[{tool_name} 結果]\n" + json.dumps(skipped, ensure_ascii=False, separators=(",", ":")))
                        continue
                action_signature = self._action_signature(call)
                checkpoint_action_id = action_id or f"ITER-{iteration}-TOOL-{idx}"
                self.checkpoint_store.prepare_action(
                    self.current_run_id, iteration=iteration, action_id=checkpoint_action_id,
                    tool=tool_name, signature=action_signature,
                )
                resume_result = None
                if active_stage:
                    try:
                        resume_policy, resume_result = self.checkpoint_store.stage_resume_policy(self.current_run_id, active_stage["stage_id"], call)
                    except ValueError as exc:
                        return self._controlled_stop("PROTOCOL_VIOLATION", iteration, tool_action_title, str(exc))
                    if resume_policy == "RECONCILE_REQUIRED":
                        return self._controlled_stop("RECONCILE_REQUIRED", iteration, tool_action_title, "stage action 曾 STARTED_UNCONFIRMED；為避免重複 mutation 不會自動重播。")
                cached = self._action_result_ledger.get(action_id) if action_id else None
                if cached and cached.get("signature") != action_signature:
                    return self._controlled_stop(
                        "PROTOCOL_VIOLATION", iteration, tool_action_title,
                        f"action_id={action_id} 被重用但 payload 不同；為避免重複/錯誤執行已停止。",
                    )

                started = time.time()
                try:
                    self._publish_status(
                        "EXECUTING_TOOL", actor="LOCAL_TOOL",
                        message=f"執行本機工具：{tool_name}",
                        progress={"current": len(raw_results) + 1, "total": len(tool_calls), "item": tool_name},
                    )
                    if resume_result is not None:
                        result = resume_result
                    elif cached:
                        result = cached.get("result", "")
                        self._log_protocol_replay(action_id, tool_name)
                    else:
                        self.checkpoint_store.mark_started(self.current_run_id, checkpoint_action_id)
                        raw_result = execute_tool(call, agent=self)
                        result = prepare_tool_result(call, raw_result, self)
                        if SMARTAGENT_PROTOCOL_VERSION >= 8:
                            action_result_id = "RES-ACTION-" + uuid.uuid4().hex[:12].upper()
                            self._v8_action_results[action_id] = v8_build_result(
                                admitted, action_result_id, "COMMITTED", result,
                            )
                        if action_id:
                            self._action_result_ledger[action_id] = {
                                "signature": action_signature,
                                "result": result,
                            }
                        self.checkpoint_store.mark_committed(self.current_run_id, checkpoint_action_id, result)
                    elapsed = time.time() - started
                    self.task_telemetry.inc("tool_action_count")
                    if tool_name == "read_file": self.task_telemetry.inc("file_read_count")
                    if tool_name == "upload_files": self.task_telemetry.inc("uploaded_file_count", len(call.get("paths", []) or []))
                    self.task_telemetry.event("tool_action_end", tool=tool_name, duration_ms=round(elapsed*1000,3), cached=bool(cached))
                    if tool_name == "run_command":
                        command_text = str(call.get("command", "") or "").lower()
                        if any(token in command_text for token in ("cmake --build", "msbuild", "ninja", "gradle", "gradlew", "make ", "dotnet build")):
                            self.task_telemetry.inc("build_count")
                            self.task_telemetry.event("build_end", duration_ms=round(elapsed*1000,3))
                    raw_results.append(result)
                    tool_results.append(f"[{tool_name} 結果]\n{result}")
                    if active_stage:
                        classified = classify_tool_result(tool_name, result)
                        stage_outcomes[action_id] = classified["status"]
                        stage_details[action_id] = {"status":classified["status"], "reason":classified["reason"], "evidence":classified["evidence"], "result":result}
                        if classified["status"] == "FAILED":
                            self.checkpoint_store.mark_committed(self.current_run_id, checkpoint_action_id, {"status":"FAILED","reason":classified["reason"],"result":result})
                    print(f"  [AGENT 完成] {tool_name} ({elapsed:.2f}s)", flush=True)
                    self._publish_status(
                        "TOOL_COMPLETED", actor="LOCAL_TOOL",
                        message=f"本機工具完成：{tool_name}",
                    )
                    if status_callback:
                        try:
                            status_callback(
                                f"[Iteration {iteration}] AGENT 完成：{tool_action_title}；"
                                "準備把結果交回 Planner。"
                            )
                        except Exception as e:
                            print(f"  [!] status callback failed: {e}", flush=True)
                except Exception as e:
                    elapsed = time.time() - started
                    self._record_issue(
                        e, actor="LOCAL_TOOL", stage="EXECUTING_TOOL", tool=tool_name,
                        action_id=action_id, detail=f"elapsed_sec={elapsed:.3f}",
                    )
                    print(f"  [AGENT 失敗] {tool_name} ({elapsed:.2f}s): {e}", flush=True)
                    if status_callback:
                        try:
                            status_callback(
                                f"[Iteration {iteration}] AGENT 執行失敗："
                                f"{tool_action_title} → {e}"
                            )
                        except Exception as cb_e:
                            print(f"  [!] status callback failed: {cb_e}", flush=True)
                    if not active_stage:
                        self.task_telemetry.event("task_failed", iteration=iteration, tool=tool_name, error=type(e).__name__)
                        self.task_telemetry.save()
                        raise
                    stage_outcomes[action_id] = "FAILED"
                    failure = {"status": "FAILED", "error": f"{type(e).__name__}: {e}"}
                    stage_details[action_id] = failure
                    raw_results.append(failure)
                    tool_results.append(f"[{tool_name} 結果]\n" + json.dumps(failure, ensure_ascii=False, separators=(",", ":")))
                    continue

                # ESC pressed while a long-running command/tool is active does
                # not kill it; the buffered key is handled here after return.
                if not self._pause_checkpoint(iteration, tool_action_title, status_callback):
                    return self._controlled_stop(
                        "ABORTED", iteration, tool_action_title, "使用者已中止目前任務。"
                    )

            tool_text = "\n\n".join(tool_results)
            if not active_stage and utf8_size(tool_text) > ROUND_INLINE_MAX_BYTES:
                tool_text = prepare_tool_result(
                    {"tool": "round_results", "action_id": f"ROUND-{iteration}", "full_result_required": True},
                    tool_text,
                    self,
                    force_attachment=True,
                )
            if active_stage:
                stage_result = {
                    "schema": "SMARTAGENT_STAGE_RESULT_V1", "stage_id": active_stage["stage_id"],
                    "seq": active_stage["seq"], "task_size": active_stage["task_size"],
                    "status": "PASS" if stage_outcomes and all(x == "COMMITTED" for x in stage_outcomes.values()) else "FAIL",
                    "actions": stage_details,
                }
                try:
                    stored = self._result_store.put(stage_result)
                except Exception as exc:
                    self.task_telemetry.event("result_store_failed", error=type(exc).__name__)
                    self.task_telemetry.save()
                    return self._controlled_stop("RESULT_STORE_FAILED", iteration, action_title, str(exc))
                compact = self._result_store.compact(stage_result, stored)
                self.task_telemetry.inc("stage_result_bytes", stored["result_bytes"])
                self.task_telemetry.event("stage_result", stage_id=active_stage["stage_id"], status=stage_result["status"], result_bytes=stored["result_bytes"])
                tool_text = "[SMARTAGENT_STAGE_COMPACT_RESULT]\n" + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
            result_id = "RES-" + uuid.uuid4().hex[:12].upper()
            self._pending_result_ack_id = result_id
            self.conversation_history.append({"role": "assistant", "content": response_text})
            self.conversation_history.append({
                "role": "user",
                "content": (
                    f"RUN_ID={self.current_run_id}\nRESULT_ID={result_id}\n工具執行結果:\n{tool_text}\n\n"
                    "請根據結果決定下一步；所有回覆都只能是 smartagent_tool envelope。"
                    "下一輪 turn_commit 必須 ack_result_id=上述 RESULT_ID，並原樣 ACK Local Commit 的 ack_web_ack_id；"
                    "同時產生新的唯一 web_ack_id。每個 action/final_response 必須帶唯一 action_id。"
                    "嚴格維持 WebGPT ACK → LocalAgent ACK → WebGPT ACK 的交替鏈；若還需要 action，輸出對應 action；"
                    "若任務已完成，輸出 final_response，最後再輸出 matching turn_commit。"
                    "不得直接輸出普通文字。"
                )
            })
            self.save_project_history()

            signature = _progress_signature(
                action_title,
                tool_calls,
                result_evidence=raw_results,
                diagnostics=parse_diagnostics,
            )
            if progress_guard.record(signature):
                return self._controlled_stop(
                    "STALLED",
                    iteration,
                    action_title,
                    (
                        f"連續 {self._stall_threshold} 輪的 action/target/result/evidence 都相同，"
                        "判定為無意義遞迴。若 tool result、verification、parser diagnostic、command、"
                        "target 或檔案內容/hash 有任何變化，counter 會自動重置。"
                    ),
                )

            # Safe point before starting the next Planner iteration.
            if not self._pause_checkpoint(iteration, action_title, status_callback):
                return self._controlled_stop(
                    "ABORTED", iteration, action_title, "使用者已中止目前任務。"
                )

        final_msg = (
            f"[EMERGENCY_STOP] 達到 emergency hard cap ({self._emergency_cap} iterations)。"
            "這不是一般任務完成條件，而是避免極端 runaway 的最後保護。\n"
            f"Iteration {self._emergency_cap} | {self._active_action_title} "
            f"| RUN_ID={self.current_run_id}"
        )
        self.conversation_history.append({"role": "assistant", "content": final_msg})
        self.save_project_history()
        self.task_telemetry.event("task_stopped", marker="EMERGENCY_STOP", iteration=self._emergency_cap)
        self.task_telemetry.save()
        return final_msg


    def switch_tier(self, new_tier: int) -> str:
        if new_tier not in TIER_STRATEGY:
            return f"[錯誤] 無效的 Tier: {new_tier}"
        self.tier = new_tier
        strategy = TIER_STRATEGY[new_tier]
        self.planner_key = strategy["planner"]
        self.executor_key = strategy["executor"]
        self.planner_model = MODELS[self.planner_key]["model"]
        self.executor_model = MODELS[self.executor_key]["model"]
        self.tier_label = strategy["label"]
        self.system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            tier_label=self.tier_label,
            planner_model=self.planner_model,
            executor_model=self.executor_model,
        )
        return f"[OK] 已切換到 {strategy['label']}"

    def save_history(self, path: str):
        data = {
            "session_start": self.session_start.isoformat(),
            "tier": self.tier,
            "planner": self.planner_model,
            "executor": self.executor_model,
            "workspace_root": str(self.workspace_root) if self.workspace_root else None,
            "attached_files": self.attached_files,
            "messages": self.conversation_history
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[OK] 已儲存對話到: {path}")

class Spinner:
    FRAMES = ["|", "/", "-", "\\"]

    def __init__(self, label):
        self.label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            label = self.label() if callable(self.label) else self.label
            print(f"\r  {self.FRAMES[i%4]} {label}...", end="", flush=True)
            i += 1
            time.sleep(0.15)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join()
        print("\r" + " " * 70 + "\r", end="", flush=True)

# ─── Main ─────────────────────────────────────────────────────────────────────

HELP_TEXT = """
指令:
  /tier          顯示目前策略層級
  /tier 1|2|3    手動切換策略層級
  /redetect      重新偵測網路環境
  /models        顯示各 tier 使用的模型
  /clear         清除對話記憶
  /history       顯示對話記錄
  /save [檔案]   儲存對話
  /run <指令>    直接執行系統指令
  /file <路徑>   載入檔案給 Agent 分析
  /tools         顯示可用工具
  /exit          退出

Tier 說明:
  Tier 1 = WebGPT only (ChatGPT/Gemini web runtime)
"""

def print_banner(tier: int, strategy: dict):
    planner = MODELS[strategy["planner"]]["model"]
    executor = MODELS[strategy["executor"]]["model"]
    operator = MODELS[strategy.get("operator", "web_chatgpt")]["model"]
    print(f"""
+================================================================+
|   SMART AI AGENT  v2.0  —  自適應三層策略                     |
+================================================================+
|  目前策略: Tier {tier}                                              |
|  決策模型: {planner:<52}|
|  執行模型: {executor:<52}|
|  Agent 2 : {operator:<52}|
+================================================================+
""")

from agent_core.workspace import (
    normalize_web_conversation_url as _normalize_web_conversation_url,
    normalize_workspace_path as _normalize_workspace_path,
)
from agent_core.web_provider_routing import web_model_key_for_url, provider_for_url
from agent_core.conversation_registry import (
    ConversationRegistry,
    DEFAULT_CONVERSATION_STORE as SMARTAGENT_CONVERSATION_STORE,
)
from agent_core.session_protocol import (
    SessionProtocol, BOOTSTRAP, SESSION_ATTACH,
)
from agent_core.startup_preferences import (
    load_startup_preferences as _load_startup_preferences,
    save_startup_preferences as _save_startup_preferences,
)

def _conversation_registry() -> ConversationRegistry:
    registry = ConversationRegistry(SMARTAGENT_CONVERSATION_STORE)
    registry.load()
    return registry

def _load_workspace_links() -> list[dict]:
    return _conversation_registry().list_bindings()

def _save_workspace_links(profiles: list[dict]) -> None:
    registry = _conversation_registry()
    registry.replace_bindings(profiles)

def _refresh_missing_conversation_names(profiles: list[dict]) -> list[dict]:
    """Resolve missing ChatGPT titles before rendering the startup menu."""
    missing = [p for p in profiles if not str(p.get("display_name", "") or "").strip()]
    if not missing:
        return profiles
    print("\n[*] 正在同步 ChatGPT 對話名稱，請稍候...")
    try:
        from agent_core import web_runtime
        # Start the authenticated persistent ChatGPT profile on one registered
        # conversation. The API lookup below does not navigate through every chat.
        manager = web_runtime.get_manager()
        registry = _conversation_registry()
        updated = 0
        for profile in missing:
            provider = provider_for_url(profile["gpt_url"])
            web_runtime.SERVICE_CONFIG[provider]["url"] = profile["gpt_url"]
            scraper = manager.get_or_create(provider)
            title = scraper.get_conversation_display_name_for_url(profile["gpt_url"]) if provider == "chatgpt" else ""
            if not title:
                # ChatGPT may reject the history-detail endpoint for project
                # conversations. On this one-time recovery path, navigate each
                # missing binding and read its visible/document title instead.
                scraper.navigate_to_conversation(profile["gpt_url"])
                title = scraper.get_conversation_display_name()
            if not title:
                continue
            registry.set_display_name(profile["gpt_url"], title)
            profile["display_name"] = title
            updated += 1
        print(f"[*] ChatGPT 對話名稱同步完成：{updated}/{len(missing)}")
    except Exception as exc:
        print(f"[!] 對話名稱同步失敗，仍可繼續選擇：{type(exc).__name__}: {exc}")
    return profiles

def _prompt_new_workspace_link(existing: list[dict]) -> dict:
    """Interactively collect one valid workspace + ChatGPT URL pair and persist it."""
    while True:
        workspace_raw = input("\nWorkspace 路徑: ").strip()
        try:
            workspace = _normalize_workspace_path(workspace_raw)
            break
        except ValueError as exc:
            print(f"  [!] {exc}")

    while True:
        gpt_url_raw = input("ChatGPT URL: ").strip()
        try:
            gpt_url = _normalize_web_conversation_url(gpt_url_raw)
            break
        except ValueError as exc:
            print(f"  [!] {exc}")

    normalized_key = (os.path.normcase(workspace), gpt_url)
    for profile in existing:
        key = (os.path.normcase(profile["workspace"]), profile["gpt_url"])
        if key == normalized_key:
            print("  [i] 此 Workspace + GPT URL 已存在，直接使用既有設定。")
            return profile

    profile = {"workspace": workspace, "gpt_url": gpt_url}
    existing.append(profile)
    _save_workspace_links(existing)
    print(f"  [OK] 已保存到 {SMARTAGENT_CONVERSATION_STORE}")
    return profile


def _prompt_delete_workspace_link(existing: list[dict]) -> bool:
    """Remove one saved menu binding without deleting its Workspace files."""
    if not existing:
        print("  [i] 目前沒有可刪除的專案設定。")
        return False

    print("\n  選擇要從啟動清單移除的專案：")
    for idx, profile in enumerate(existing, 1):
        display = str(profile.get("display_name", "") or "").strip() or "（名稱尚未同步）"
        print(f"  [{idx}] {display}")
        print(f"      Workspace: {profile['workspace']}")
        print(f"      GPT URL  : {profile['gpt_url']}")
    print("  [0] 取消")

    choice = input("\n請選擇要移除的編號: ").strip()
    try:
        selected = int(choice)
    except ValueError:
        print("  [!] 請輸入有效的數字。")
        return False
    if selected == 0:
        return False
    if not 1 <= selected <= len(existing):
        print("  [!] 選項不存在。")
        return False

    target = existing[selected - 1]
    display = str(target.get("display_name", "") or "").strip() or target["workspace"]
    confirm = input(f"確定從 CMD 清單移除「{display}」？不會刪除硬碟檔案。(y/N): ").strip().lower()
    if confirm not in {"y", "yes"}:
        print("  [i] 已取消。")
        return False

    del existing[selected - 1]
    _save_workspace_links(existing)
    print("  [OK] 已從啟動清單移除；Workspace 檔案未刪除。")
    return True


def _select_workspace_link() -> dict:
    """Resolve the startup workspace/ChatGPT pair before any model selection."""
    default_mode = (
        "--default-profile" in sys.argv[1:]
        or os.environ.get("SMARTAGENT_DEFAULT_PROFILE", "").strip() == "1"
    )

    if default_mode:
        workspace_raw = os.environ.get(
            "SMARTAGENT_DEFAULT_WORKSPACE",
            str(Path(__file__).resolve().parent),
        )
        gpt_url_raw = os.environ.get(
            "SMARTAGENT_DEFAULT_GPT_URL",
            "https://chatgpt.com/",
        )
        workspace = _normalize_workspace_path(workspace_raw)
        gpt_url = _normalize_web_conversation_url(gpt_url_raw)
        print("\n[SmartAgent Default Profile]")
        return {"workspace": workspace, "gpt_url": gpt_url, "default_mode": True}

    profiles = _refresh_missing_conversation_names(_load_workspace_links())
    while True:
        print("\n" + "=" * 74)
        print("  SmartAgent — 選擇 Workspace + ChatGPT 對話")
        print("=" * 74)
        if profiles:
            for idx, profile in enumerate(profiles, 1):
                display = str(profile.get("display_name", "") or "").strip()
                if not display:
                    display = "（名稱尚未同步）"
                print(f"  [{idx}] {display}")
                print(f"      Workspace: {profile['workspace']}")
                print(f"      GPT URL  : {profile['gpt_url']}")
        else:
            print("  (目前沒有已保存的 Workspace + GPT URL)")

        add_index = len(profiles) + 1
        delete_index = add_index + 1
        print(f"  [{add_index}] ＋ 新增 Workspace + GPT URL")
        print(f"  [{delete_index}] － 刪除專案（只從 CMD 清單移除）")

        choice = input("\n請選擇: ").strip()
        try:
            selected = int(choice)
        except ValueError:
            print("  [!] 請輸入有效的數字。")
            continue

        if 1 <= selected <= len(profiles):
            return {**profiles[selected - 1], "default_mode": False}
        if selected == add_index:
            profile = _prompt_new_workspace_link(profiles)
            return {**profile, "default_mode": False}
        if selected == delete_index:
            _prompt_delete_workspace_link(profiles)
            continue

        print("  [!] 選項不存在。")


def _configure_web_start_url(gpt_url: str) -> None:
    """Configure the URL-derived Web provider before any browser is created."""
    from agent_core import web_runtime

    provider = provider_for_url(gpt_url)
    cfg = web_runtime.SERVICE_CONFIG.get(provider)
    if not isinstance(cfg, dict):
        raise RuntimeError(f"agent_core.web_runtime missing SERVICE_CONFIG for {provider}")
    cfg["url"] = gpt_url


def _validated_saved_startup() -> dict:
    """Return the shared last selection only while every identity is still valid."""
    saved = _load_startup_preferences()
    migrated = False
    if not saved:
        return {}
    workspace = str(saved.get("workspace", "") or "")
    gpt_url = str(saved.get("gpt_url", "") or "")
    planner_key = str(saved.get("planner_key", "") or "")
    executor_key = str(saved.get("executor_key", "") or "")
    if not str(saved.get("operator_key", "") or "").strip():
        saved["operator_key"] = planner_key if planner_key in MODELS else "web_chatgpt"
        migrated = True
    operator_key = str(saved.get("operator_key", planner_key or "web_chatgpt") or planner_key or "web_chatgpt")
    try:
        workspace = _normalize_workspace_path(workspace)
        gpt_url = _normalize_web_conversation_url(gpt_url)
    except ValueError:
        return {}
    if planner_key not in MODELS or executor_key not in MODELS or operator_key not in MODELS:
        return {}
    if _conversation_registry().find(workspace, gpt_url) is None:
        return {}
    desired_web_key = web_model_key_for_url(gpt_url)
    if planner_key.startswith("web_") and planner_key != desired_web_key:
        planner_key = desired_web_key
        migrated = True
    if executor_key.startswith("web_") and executor_key != desired_web_key:
        executor_key = desired_web_key
        migrated = True
    if operator_key.startswith("web_") and operator_key != desired_web_key:
        operator_key = desired_web_key
        migrated = True
    if planner_key.startswith("web_") and not PLAYWRIGHT_AVAILABLE:
        return {}
    if migrated:
        try:
            _save_startup_preferences(
                workspace=workspace,
                gpt_url=gpt_url,
                planner_key=planner_key,
                executor_key=executor_key,
                operator_key=operator_key,
            )
        except OSError:
            # Read-only/locked preference stores must not prevent startup; the
            # in-memory default remains effective and a later configure run can
            # persist it.
            pass
    return {
        "workspace": workspace,
        "gpt_url": gpt_url,
        "planner_key": planner_key,
        "executor_key": executor_key,
        "operator_key": operator_key,
    }

def _ensure_web_protocol_session(
    *,
    service: str,
    workspace: str,
    gpt_url: str,
    protocol_body: str,
) -> dict:
    """Bootstrap or attach the conversation-level SmartAgent protocol session.

    This is deliberately performed before the normal user task loop. A matching
    stored protocol identity gets a lightweight SESSION_ATTACH; a new/stale or
    version/hash-mismatched conversation gets the full canonical bootstrap.
    """
    global WEB_PROTOCOL_SESSION_ACTIVE
    from agent_core.web_runtime import get_manager

    registry = _conversation_registry()
    registry.upsert_binding(workspace, gpt_url)
    session = SessionProtocol(
        SMARTAGENT_PROTOCOL_NAME,
        SMARTAGENT_PROTOCOL_VERSION,
        protocol_body,
    )
    stored = registry.get_protocol_state(workspace, gpt_url, SMARTAGENT_PROTOCOL_NAME)
    decision = session.decide(stored)
    manager = get_manager()

    def bootstrap_once(reason: str) -> dict:
        prompt = session.bootstrap_prompt(session_id=decision.session_id)
        print(
            f"[*] SmartAgent Protocol Bootstrap: {session.identity.protocol_name} "
            f"v{session.identity.protocol_version} ({reason})"
        )
        response = manager.ask(service, prompt, new_conversation=False)
        if not session.parse_protocol_ready(response, session_id=decision.session_id):
            failed = session.failed_state(session_id=decision.session_id)
            registry.set_protocol_state(
                workspace, gpt_url, SMARTAGENT_PROTOCOL_NAME, failed
            )
            raise RuntimeError(
                "WebGPT 未回傳 matching AGENT_PROTOCOL_READY；conversation 保持 NOT ARMED。"
            )
        state = session.armed_state(session_id=decision.session_id)
        registry.set_protocol_state(workspace, gpt_url, SMARTAGENT_PROTOCOL_NAME, state)
        WEB_PROTOCOL_SESSION_ACTIVE = True
        return {"mode": BOOTSTRAP, "state": state}

    if decision.action == BOOTSTRAP:
        return bootstrap_once(decision.reason)

    print(
        f"[*] SmartAgent Session Attach: {session.identity.protocol_name} "
        f"v{session.identity.protocol_version}"
    )
    response = manager.ask(
        service,
        session.session_attach_prompt(session_id=decision.session_id),
        new_conversation=False,
    )
    if session.parse_session_ready(response, session_id=decision.session_id):
        state = session.armed_state(session_id=decision.session_id)
        registry.set_protocol_state(workspace, gpt_url, SMARTAGENT_PROTOCOL_NAME, state)
        WEB_PROTOCOL_SESSION_ACTIVE = True
        return {"mode": SESSION_ATTACH, "state": state}

    # A conversation may have lost/compacted the old protocol even though the
    # registry says ARMED. Re-bootstrap once rather than silently trusting it.
    print("  [!] SESSION_READY 驗證失敗，改做一次 Full Bootstrap。")
    decision.action = BOOTSTRAP
    return bootstrap_once("session_attach_failed")

def _remote_worker_cli() -> dict:
    args = sys.argv[1:]
    def value(flag: str) -> str:
        try:
            return args[args.index(flag) + 1]
        except (ValueError, IndexError):
            return ""
    return {
        "task_id": value("--remote-worker-task"),
        "token": value("--remote-worker-token"),
        "cdp": value("--remote-cdp"),
    }


def _enable_remote_worker_console(task_id: str) -> None:
    if os.name != "nt" or os.environ.get("SMARTAGENT_REMOTE_WORKER_CONSOLE", "0") != "1":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.AllocConsole()
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleTitleW(f"SmartAgent Remote Agent1 Worker — {task_id}")
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except Exception:
        pass


def _remote_telegram_get_file_fast_result(request: str, workspace: str):
    match = re.fullmatch(r"\s*/get\s+(.+?)\s*", str(request or ""), re.IGNORECASE)
    if not match: return "", None
    raw = match.group(1).strip().strip("`\"'")
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("/", "\\")):
        raise ValueError("/get 只允許 workspace-relative 路徑")
    root = Path(workspace).resolve(); target = (root / raw).resolve()
    try: relative = target.relative_to(root)
    except ValueError as exc: raise ValueError("/get 路徑超出授權 workspace") from exc
    if not target.is_file(): raise FileNotFoundError(f"/get 找不到檔案: {relative}")
    kind = "photo" if target.suffix.lower() in {".jpg", ".jpeg", ".png"} else "document"
    return f"準備傳送檔案：{relative.as_posix()}", {"path":str(target),"kind":kind,"name":target.name,"workspace":str(root)}


def _remote_workspace_list_fast_result(request: str, workspace: str) -> str:
    """List one workspace-contained directory without invoking the planner."""
    text = str(request or "")
    lowered = text.lower()
    wants_list = (
        "list" in lowered
        or "列出" in text
        or any(token in text for token in ("有哪些檔案", "有哪些資料夾", "檔案有哪些", "資料夾有哪些", "目錄內容"))
    )
    wants_directory = any(token in lowered for token in ("workspace", "directory", "folder")) or any(
        token in text for token in ("路徑", "目錄", "檔案", "資料夾")
    )
    if not (wants_list and wants_directory):
        return ""
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise RuntimeError(f"remote workspace 不存在: {root}")

    requested_path = None
    absolute_match = re.search(r"(?<![A-Za-z0-9_])([A-Za-z]:[\\/][^\r\n*?<>|]+)", text)
    if absolute_match:
        requested_path = Path(absolute_match.group(1).rstrip(" \t，。；;"))
    else:
        quoted_match = re.search(r'["“「『]([^"”」』]+)["”」』]', text)
        if quoted_match:
            candidate = Path(quoted_match.group(1).strip())
            if candidate.is_absolute() or candidate.drive:
                requested_path = candidate
            else:
                requested_path = root / candidate

    target = (requested_path or root).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("RemoteAgent 列舉目標超出授權 workspace")
    if not target.is_dir():
        raise ValueError(f"指定的 workspace 子資料夾不存在: {target}")
    listing = tool_list_directory(str(target))
    return f"已完成 {target} 第一層內容列舉：\n{listing}"


_REMOTE_CONTROL_STOP_RE = re.compile(
    r"^\[(STALLED|ABORTED|EMERGENCY_STOP|PROTOCOL_VIOLATION|"
    r"RECONCILE_REQUIRED|RESULT_STORE_FAILED)\](?:\s|$)",
    re.IGNORECASE,
)


def _remote_control_stop_reason(response: str) -> str:
    """Return a failure reason when an agent control stop is not a real result."""
    text = str(response or "").strip()
    match = _REMOTE_CONTROL_STOP_RE.match(text)
    if not match:
        return ""
    marker = match.group(1).upper()
    first_line = text.splitlines()[0] if text else marker
    return f"remote_agent_control_stop:{marker}: {first_line}"


def _remote_create_file_fast_result(request: str, workspace: str) -> str:
    """Create one explicitly named file inside the authorized workspace.

    Existing files are verified but never overwritten. This narrow path keeps a
    simple RemoteAgent request independent of planner Tool Envelope formatting.
    """
    text = str(request or "")
    create_match = re.search(
        r"(?:生成|建立|新增|創建|create|generate|make)\s*"
        r"(?:一個\s*)?(?:檔案\s*|file\s*)?"
        r"[`\"']?([A-Za-z0-9_./\\: -]+\.[A-Za-z0-9]{1,12})[`\"']?",
        text,
        re.IGNORECASE,
    )
    if not create_match:
        return ""

    root = Path(workspace).resolve()
    if not root.is_dir():
        raise RuntimeError(f"remote workspace 不存在: {root}")

    relative_name = create_match.group(1).strip()
    requested_path = Path(relative_name)
    if not relative_name or requested_path.is_absolute() or requested_path.drive:
        raise ValueError("RemoteAgent 建立檔案只接受 workspace 內的相對路徑")
    target = (root / requested_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("RemoteAgent 建立檔案目標超出授權 workspace")
    if not target.parent.is_dir():
        raise ValueError(f"目標子資料夾不存在: {target.parent}")

    created = False
    try:
        with target.open("x", encoding="utf-8", newline=""):
            pass
        created = True
    except FileExistsError:
        if not target.is_file():
            raise RuntimeError(f"目標已存在但不是一般檔案: {target}")

    if not target.is_file():
        raise RuntimeError(f"檔案建立後驗證失敗: {target}")
    action = "已建立並驗證" if created else "檔案已存在，已驗證且未覆寫"
    return f"{action}：{target}"


def main():
    from agent_core.protocol_manifest import require_protocol_manifest
    from agent_core.paths import install_root
    require_protocol_manifest(install_root())
    remote_worker = _remote_worker_cli()
    remote_worker_mode = bool(remote_worker["task_id"])
    adopted_remote_task = None
    worker_startup_heartbeat_stop = None
    worker_runtime_log = None
    worker_status_publisher = None
    if remote_worker_mode:
        _enable_remote_worker_console(remote_worker["task_id"])
        from agent_core.remote_runtime_log import RemoteRuntimeLog
        worker_dir = remote_workers_root() / remote_worker["task_id"]
        worker_runtime_log = RemoteRuntimeLog(
            remote_runtime_log_path(),
            mirror_console=True,
            mirror_paths=[worker_dir / "runtime.jsonl"],
            snapshot_path=worker_dir / "lifecycle.json",
        )
        worker_status_publisher = StatusPublisher(worker_dir / "status.json")
        worker_status_publisher.configure(task_id=remote_worker["task_id"], source="REMOTE_AGENT")
        def _mirror_worker_status(snapshot):
            worker_runtime_log.write(
                "STATUS", component="worker_status",
                stage=str(snapshot.get("stage", "") or ""),
                state=str(snapshot.get("state", "") or ""),
                task_id=str(snapshot.get("task_id", remote_worker["task_id"]) or ""),
                request_id=str(snapshot.get("request_id", "") or ""),
                actor=str(snapshot.get("actor", "") or ""),
                message=str(snapshot.get("message", "") or ""),
                detail=str(snapshot.get("detail", "") or "")[:1000],
                error=str(snapshot.get("error", "") or "")[:2000],
            )
        worker_status_publisher.subscribe(_mirror_worker_status)
        worker_status_publisher.publish(
            "PROCESS_START", message="Agent1 worker 已啟動",
            detail=(
                f"pid={os.getpid()} browser_mode="
                f"{'ISOLATED_CHROMIUM' if os.environ.get('SMARTAGENT_ISOLATED_BROWSER') == '1' else 'LEGACY_CDP'}"
            ),
        )
        worker_runtime_log.write(
            "CONNECT", component="remote_worker", stage="PROCESS_START",
            task_id=remote_worker["task_id"], cdp=remote_worker["cdp"],
            browser_mode=(
                "ISOLATED_CHROMIUM"
                if os.environ.get("SMARTAGENT_ISOLATED_BROWSER", "") == "1"
                else "LEGACY_CDP"
            ),
        )
        if remote_worker["cdp"]:
            os.environ["SMARTAGENT_CHATGPT_CDP"] = remote_worker["cdp"]
        if os.environ.get("SMARTAGENT_ISOLATED_BROWSER", "") != "1":
            os.environ["SMARTAGENT_ATTACH_CDP"] = "1"
        os.environ["SMARTAGENT_REMOTE_WORKER_TASK"] = remote_worker["task_id"]
    force_configuration = "--configure-startup" in sys.argv[1:]
    saved_startup = {} if force_configuration else _validated_saved_startup()
    try:
        if remote_worker_mode:
            from agent_core.task_state import RemoteTaskQueue, TaskStateStore
            worker_store = TaskStateStore(remote_tasks_path())
            worker_task = worker_store.get(remote_worker["task_id"])
            if worker_task is None:
                raise RuntimeError(f"remote worker task 不存在: {remote_worker['task_id']}")
            if not saved_startup:
                raise RuntimeError("remote worker 缺少已保存的模型設定")
            # Adopt before network detection, CDP attachment, or
            # WebGPT protocol setup. Those startup stages can legitimately take
            # longer than the dispatch lease and must not leave worker_pid=0.
            startup_queue = RemoteTaskQueue(worker_store)
            adopted_remote_task = startup_queue.adopt_dispatched(
                remote_worker["task_id"], remote_worker["token"], worker_pid=os.getpid()
            )
            worker_runtime_log.write(
                "TASK_STARTED", component="remote_worker", stage="ADOPTED",
                task_id=adopted_remote_task.task_id,
                request_id=adopted_remote_task.request_id,
            )
            worker_status_publisher.configure(
                request_id=adopted_remote_task.request_id,
                workspace=adopted_remote_task.workspace,
                conversation_url=adopted_remote_task.conversation_url,
            )
            worker_status_publisher.publish(
                "TASK_ADOPTED", message="已接手 RemoteAgent 任務",
                detail=f"request_id={adopted_remote_task.request_id}",
            )
            worker_startup_heartbeat_stop = threading.Event()
            def _worker_startup_heartbeat():
                while not worker_startup_heartbeat_stop.wait(5.0):
                    try:
                        startup_queue.heartbeat(adopted_remote_task.task_id, worker_pid=os.getpid())
                    except Exception as exc:
                        worker_runtime_log.write(
                            "ERROR", component="remote_worker", stage="HEARTBEAT_RETRY",
                            task_id=adopted_remote_task.task_id,
                            error=f"{type(exc).__name__}: {exc}",
                        )
            threading.Thread(
                target=_worker_startup_heartbeat,
                name=f"remote-startup-lease-{adopted_remote_task.request_id}",
                daemon=True,
            ).start()
            from agent_core.task_transport import resolve_task_execution_chatgpt_url
            worker_execution_url = resolve_task_execution_chatgpt_url(
                worker_task, registry=_conversation_registry()
            )
            worker_runtime_log.write(
                "CONNECT", component="remote_worker", stage="EXECUTION_ROUTE",
                task_id=adopted_remote_task.task_id,
                source_conversation=adopted_remote_task.conversation_url,
                execution_url=worker_execution_url,
            )
            startup_profile = {
                "workspace": _normalize_workspace_path(worker_task.workspace),
                # The source conversation remains Agent0's inbox/reply route.
                # Agent1 starts a separate new chat (inside the same project
                # when available), so its internal turns cannot loop to ingress.
                "gpt_url": worker_execution_url,
                "default_mode": False,
            }
        elif saved_startup:
            startup_profile = {
                "workspace": saved_startup["workspace"],
                "gpt_url": saved_startup["gpt_url"],
                "default_mode": False,
            }
        else:
            startup_profile = _select_workspace_link()
        _configure_web_start_url(startup_profile["gpt_url"])
    except (ValueError, RuntimeError) as exc:
        print(f"\n[錯誤] SmartAgent 啟動設定無效: {exc}")
        sys.exit(2)

    selected_workspace = startup_profile["workspace"]
    selected_gpt_url = startup_profile["gpt_url"]
    if not remote_worker_mode:
        from agent_core.self_repair_protocol import DEFAULT_REPAIR_URL
        _conversation_registry().upsert_binding(
            selected_workspace, DEFAULT_REPAIR_URL, purpose="self_repair"
        )
    print(f"  Workspace : {selected_workspace}")
    print(f"  GPT URL   : {selected_gpt_url}")
    startup_mode = "remembered" if saved_startup else (
        "default" if startup_profile.get("default_mode") else "configured"
    )
    print(f"  Mode      : {startup_mode}")
    print()

    # Detect network tier
    print("[*] 偵測網路環境與可用 API...")
    _, net_status = detect_network_tier()
    
    has_int = net_status["internet"]
    if not has_int:
        print("\n[!] 偵測不到網路連線，已為您過濾，僅顯示純離線本地模型。")

    if not has_int:
        raise RuntimeError("SmartAgentv1 lightweight mode requires internet access; local-model fallback is disabled.")
    preferred_key = web_model_key_for_url(selected_gpt_url)
    planner_key = preferred_key
    executor_key = preferred_key
    operator_key = preferred_key
    _save_startup_preferences(
        workspace=selected_workspace,
        gpt_url=selected_gpt_url,
        planner_key=planner_key,
        executor_key=executor_key,
        operator_key=operator_key,
    )
    print(f"[*] SmartAgentv1 lightweight WebGPT mode: {MODELS[planner_key]['desc']}")

    p_cfg = MODELS[planner_key]
    e_cfg = MODELS[executor_key]
    tier = p_cfg["tier"]
    
    strategy = {
        "planner": planner_key,
        "executor": executor_key,
        "operator": operator_key,
        "label": f"Tier {tier} [{p_cfg['type'].upper()}] 決策:{p_cfg['model']} / 執行:{e_cfg['model']}",
        "use_web_scraper": (p_cfg["type"] == "web")
    }
    
    TIER_STRATEGY.clear()
    TIER_STRATEGY[1] = strategy

    integrated_host = None

    # 若選擇網頁版，立即啟動瀏覽器並建立/驗證 conversation-level
    # protocol session。Bootstrap 成功後，第一個真正 user turn 不再重送
    # 完整 SYSTEM_PROMPT_TEMPLATE。
    if p_cfg["type"] == "web":
        service = p_cfg["model"]
        print(f"\n[*] 正在啟動 {service} 網頁瀏覽器，請稍候...")
        from agent_core.web_runtime import get_manager
        if worker_runtime_log is not None:
            worker_runtime_log.write(
                "CONNECT", component="remote_worker", stage="BROWSER_START",
                task_id=remote_worker["task_id"], service=service,
            )
        def browser_status_callback(stage, **kwargs):
            if worker_status_publisher is not None:
                return worker_status_publisher.publish(stage, **kwargs)
            return None
        web_scraper = get_manager().get_or_create(
            service, status_callback=browser_status_callback if remote_worker_mode else None
        )
        if worker_runtime_log is not None:
            worker_runtime_log.write(
                "CONNECT", component="remote_worker", stage="BROWSER_READY",
                task_id=remote_worker["task_id"], page_url=str(getattr(web_scraper._page,"url","") or ""),
            )
        if remote_worker_mode:
            # A request-scoped worker owns exactly one CDP page.  Process exit
            # closes that page through WebLLMScraper.close(), while the shared
            # LocalAgent browser context and its foreground page remain alive.
            atexit.register(get_manager().close_all)
        if service == "chatgpt" and "/c/" in selected_gpt_url:
            try:
                web_scraper.navigate_to_conversation(selected_gpt_url)
                title = web_scraper.get_conversation_display_name()
                if title and not remote_worker_mode:
                    _conversation_registry().set_display_name(selected_gpt_url, title)
                    print(f"[*] ChatGPT 對話: {title}")
            except Exception as exc:
                if remote_worker_mode:
                    worker_runtime_log.write(
                        "ERROR", component="remote_worker", stage="NAVIGATION",
                        task_id=remote_worker["task_id"], target_url=selected_gpt_url,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    raise RuntimeError(
                        f"remote_worker_navigation_failed: {type(exc).__name__}: {exc}"
                    ) from exc
        rendered_protocol = SYSTEM_PROMPT_TEMPLATE.format(
            tier_label=strategy["label"],
            planner_model=MODELS[strategy["planner"]]["model"],
            executor_model=MODELS[strategy["executor"]]["model"],
        )
        if service == "chatgpt":
            session_result = _ensure_web_protocol_session(
                service=service,
                workspace=selected_workspace,
                gpt_url=selected_gpt_url,
                protocol_body=rendered_protocol,
            )
            print(f"[*] 網頁準備就緒！Session mode={session_result['mode']}")

            # Stage 7: LocalAgent owns the visible browser lifecycle; the hidden
            # RemoteAgent-0 receiver only attaches to that authenticated browser
            # over localhost CDP and performs history API polling.  Starting it
            # after protocol readiness prevents bootstrap traffic from becoming
            # part of the initial receiver baseline.
            from agent_core.agent_host import IntegratedAgentHost
            if not remote_worker_mode:
                integrated_host = IntegratedAgentHost(
                    workspace=selected_workspace,
                    conversation_url=selected_gpt_url,
                )
                supervisor_result = integrated_host.start_hidden_supervisor(
                    get_manager().get_or_create(service)
                )
                atexit.register(integrated_host.stop)
                if supervisor_result.get("started"):
                    print(
                        "[*] RemoteAgent-0 狀態窗已啟動 "
                        f"(PID={supervisor_result.get('pid')})"
                    )
                elif supervisor_result.get("reason") == "remote_autostart_disabled":
                    print(
                        "[*] LocalAgent 已就緒；RemoteAgent-0 未啟動。"
                        "需要遠端接收時請另開 launch_remote_agent.bat。"
                    )
                else:
                    print(
                        "[!] RemoteAgent-0 未啟動；LocalAgent 繼續運作。"
                        f"原因={supervisor_result.get('reason', 'unknown')}"
                    )
        else:
            # Stage 3 conversation registry is keyed by ChatGPT conversation URL.
            # Preserve legacy full-prompt behavior for Gemini until it gets its
            # own conversation binding model.
            print("[*] 網頁準備就緒！Session mode=legacy (Gemini)")

    strategy = TIER_STRATEGY[tier]
    print_banner(tier, strategy)

    agent = SmartAgent(tier, strategy)
    if remote_worker_mode:
        agent.status_publisher = worker_status_publisher
    workspace_result = agent.set_workspace_root(selected_workspace)
    if not workspace_result.get("success"):
        print(f"\n[錯誤] 無法設定 Workspace Root: {workspace_result.get('error', 'unknown error')}")
        sys.exit(2)
    print(f"[*] Workspace Root 已設定: {workspace_result['workspace_root']}")
    agent.enable_console_pause()
    from agent_core.self_repair_coordinator import SelfRepairCoordinator
    if not remote_worker_mode:
        startup_repair_root = os.environ.get("SMARTAGENT_SELF_REPAIR_ROOT", "").strip()
        startup_project_root = os.environ.get("SMARTAGENT_PROJECT_ROOT", "").strip()
        startup_coordinator = SelfRepairCoordinator(startup_repair_root, project_root=startup_project_root or AGENT_PROJECT_ROOT) if startup_repair_root else SelfRepairCoordinator()
        startup_recovery = startup_coordinator.recover_after_restart()
        startup_meta = startup_coordinator.sync_meta_resume_state()
        if startup_recovery.get("state") not in {"NO_RECOVERY", None}:
            print(f"[*] Self-repair startup recovery: {startup_recovery.get('state')}")
        if startup_meta.get("state"):
            print(f"[*] Meta Recovery startup state: {startup_meta.get('state')}")

    # LocalAgent reads only its local console. Agent0 reserves remote tasks and
    # launches one request-scoped Agent1 worker process/page per reservation;
    # this main process never claims or executes RemoteAgent work.
    from agent_core.task_state import RemoteTaskQueue, TaskStateStore
    remote_queue = RemoteTaskQueue(
        TaskStateStore(remote_tasks_path())
    )
    from agent_core.remote_events import RemoteEventStore
    remote_event_store = RemoteEventStore(remote_events_path())
    startup_workspace = selected_workspace
    startup_gpt_url = selected_gpt_url
    from agent_core.routing import build_route_context, route_context_lines
    from agent_core.task_transport import validate_task_transport

    def _registered_remote_bindings():
        return [
            (item["workspace"], item["gpt_url"])
            for item in _conversation_registry().list_bindings()
        ]

    def _activate_remote_context(task):
        transport_context = validate_task_transport(
            task, registry=_conversation_registry()
        )
        if remote_worker_mode:
            workspace_switch = agent.set_workspace_root(task.workspace)
            if not workspace_switch.get("success"):
                raise RuntimeError(f"remote_workspace_switch_failed: {workspace_switch.get('error', 'unknown')}")
            return transport_context
        if not transport_context.is_webgpt:
            raise RuntimeError(
                f"remote_transport_requires_worker:{transport_context.transport}"
            )
        if integrated_host is None:
            raise RuntimeError("remote_browser_host_unavailable")
        switched = integrated_host.activate_context(
            workspace=task.workspace,
            conversation_url=task.conversation_url,
        )
        if not switched.get("activated"):
            raise RuntimeError(f"remote_context_switch_failed: {switched.get('reason', 'unknown')}")
        # Every linked conversation owns its own protocol state.  Cross-session
        # routing must arm/upgrade the source conversation before asking WebGPT
        # to use tools introduced after that conversation was last attached.
        remote_protocol = _ensure_web_protocol_session(
            service=provider_for_url(task.conversation_url),
            workspace=task.workspace,
            gpt_url=task.conversation_url,
            protocol_body=rendered_protocol,
        )
        if remote_protocol.get("mode") not in {"BOOTSTRAP", "SESSION_ATTACH"}:
            raise RuntimeError("remote_protocol_not_ready")
        workspace_switch = agent.set_workspace_root(task.workspace)
        if not workspace_switch.get("success"):
            raise RuntimeError(
                f"remote_workspace_switch_failed: {workspace_switch.get('error', 'unknown')}"
            )
        return transport_context

    def _restore_startup_context():
        if remote_worker_mode:
            return
        agent.set_workspace_root(startup_workspace)
        if integrated_host is not None:
            restored = integrated_host.restore_foreground_context()
            if not restored.get("restored"):
                print(f"[!] 無法恢復原本對話: {restored.get('reason', 'unknown')}")
    console_events = thread_queue.Queue()
    test_signal_receiver = None
    if (
        not remote_worker_mode
        and os.environ.get("SMARTAGENT_TEST_INGRESS", "0") == "1"
    ):
        test_signal_receiver = TestSignalReceiver(
            Path(__file__).resolve().parent,
            "local",
            generation_id=str(
                os.environ.get("SMARTAGENT_RUNTIME_GENERATION", "") or ""
            ),
        )
        generation_id = test_signal_receiver.start()
        atexit.register(test_signal_receiver.close)
        print(
            f"[LocalAgent Test Ingress] READY generation_id={generation_id}",
            flush=True,
        )

    def _console_reader():
        while True:
            try:
                console_events.put(("input", input("\n你> ").strip()))
            except (EOFError, KeyboardInterrupt) as exc:
                console_events.put(("error", exc))
                return

    if not remote_worker_mode:
        threading.Thread(target=_console_reader, name="localagent-console", daemon=True).start()

    remote_worker_consumed = False
    while True:
        remote_task = None
        remote_transport_context = None
        test_envelope: TestEnvelope | None = None
        try:
            if remote_worker_mode:
                if remote_worker_consumed:
                    break
                remote_task = adopted_remote_task
                if remote_task is None:
                    raise RuntimeError("remote worker startup adoption missing")
                remote_worker_consumed = True
                user_input = remote_task.request.strip()
                if str(remote_task.metadata.get("transport", "")).upper() == "TELEGRAM":
                    paths=[str(x.get("local_path","") or "") for x in list(remote_task.metadata.get("attachments") or []) if isinstance(x,dict) and str(x.get("local_path","") or "")]
                    if paths: user_input += "\n\n[Telegram inbound attachments]\n" + "\n".join("- "+x for x in paths)
                print(
                    f"\n[RemoteAgent Worker] 已領取 {remote_task.request_id} "
                    f"({remote_task.task_id})"
                )
                remote_event_store.emit("TASK_STARTED", remote_task, status="RUNNING", payload={})
                print(f"[RemoteAgent Worker] 任務: {user_input}")
                try:
                    remote_transport_context = _activate_remote_context(remote_task)
                except Exception as exc:
                    failed_task = remote_queue.fail(remote_task.task_id, f"{type(exc).__name__}: {exc}")
                    remote_event_store.emit("TASK_FAILED", failed_task, status="FAILED", payload={"error": failed_task.error})
                    print(f"[RemoteAgent] 無法切換至來源對話: {exc}")
                    _restore_startup_context()
                    break
            else:
                try:
                    event_kind, event_value = console_events.get(timeout=0.25)
                except thread_queue.Empty:
                    signal = (
                        test_signal_receiver.poll_one()
                        if test_signal_receiver is not None
                        else None
                    )
                    if signal is None:
                        continue
                    event_kind, event_value = "test", signal
                if event_kind == "error":
                    if isinstance(event_value, EOFError):
                        print("\n[再見]")
                        break
                    raise KeyboardInterrupt
                if event_kind == "test":
                    test_envelope = event_value
                    user_input = test_envelope.message
                    if test_envelope.attachment_paths:
                        queued = agent.queue_attachments(
                            list(test_envelope.attachment_paths)
                        )
                        if "附件暫存失敗" in queued or "(沒有可排程的附件)" in queued:
                            raise RuntimeError(queued)
                    print(
                        f"\n[LocalAgent Test Ingress] REQUEST_RECEIVED "
                        f"request_id={test_envelope.request_id}",
                        flush=True,
                    )
                else:
                    user_input = str(event_value or "").strip()
            if not user_input:
                continue

            if user_input.startswith("/"):
                parts = user_input.split(maxsplit=1)
                cmd = parts[0].lower()
                arg = parts[1] if len(parts) > 1 else ""

                if cmd in ("/exit", "/quit", "/q"):
                    print("\n[再見]")
                    break
                elif cmd == "/help":
                    print(HELP_TEXT)
                elif cmd == "/tier":
                    if arg.isdigit() and int(arg) in TIER_STRATEGY:
                        print(agent.switch_tier(int(arg)))
                    else:
                        s = TIER_STRATEGY[agent.tier]
                        print(f"目前 Tier {agent.tier}: {s['label']}")
                        print(f"  決策: {MODELS[agent.planner_key]['model']}")
                        print(f"  執行: {MODELS[agent.executor_key]['model']}")
                elif cmd == "/redetect":
                    print("[*] 重新偵測網路環境...")
                    tier, net_status = detect_network_tier()
                    strategy = TIER_STRATEGY[tier]
                    print(agent.switch_tier(tier))
                    print_banner(tier, strategy)
                elif cmd == "/models":
                    print("\n可用模型設定：")
                    for t in [1, 2, 3]:
                        s = TIER_STRATEGY[t]
                        p = MODELS[s['planner']]['model']
                        e = MODELS[s['executor']]['model']
                        mark = " <-- 目前" if t == agent.tier else ""
                        print(f"  Tier {t}: 決策={p} / 執行={e}{mark}")
                elif cmd == "/clear":
                    agent.conversation_history = []
                    print("[OK] 對話記憶已清除")
                elif cmd == "/history":
                    for i, msg in enumerate(agent.conversation_history):
                        role = "你" if msg["role"] == "user" else "Agent"
                        print(f"\n[{i+1}] {role}:\n{msg['content'][:400]}...")
                elif cmd == "/save":
                    fname = arg or f"chat_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                    agent.save_history(fname)
                elif cmd == "/run":
                    if arg:
                        print(tool_run_command(arg, "GENERAL"))
                    else:
                        print("用法: /run <指令>")
                elif cmd == "/file":
                    if arg:
                        content = tool_read_file(arg)
                        user_input = f"[檔案: {arg}]\n{content}"
                        print(f"[OK] 載入 {arg} ({len(content)} chars)")
                    else:
                        print("用法: /file <路徑>")
                        continue
                elif cmd == "/tools":
                    print("""
可用工具:
  run_command    - 執行 PowerShell 指令
  read_file      - 讀取本地檔案（Web Planner 模式會自動改道成附件上傳）
  write_file     - 寫入本地檔案
  list_directory - 列出目錄
  inspect_directory - 一次統計多個目錄
  find_file      - 從 Workspace Root 尋找檔案
  upload_file    - 將單一檔案附加到目前 Web AI 對話
  upload_files   - 將多個檔案附加到目前 Web AI 對話
  web_search     - 網路搜尋（有網路時自動啟用）
                    """)
                    continue
                else:
                    print(f"未知指令: {cmd}")
                    continue

                if cmd == "/file" and arg:
                    pass
                else:
                    continue

            # Chat
            route_context = build_route_context(
                request=user_input,
                source=(remote_transport_context.route_source if remote_transport_context is not None else "local"),
                conversation_url=(
                    remote_transport_context.chatgpt_url if remote_transport_context is not None else ""
                ),
                requested_mode=(
                    remote_task.metadata.get("route_context", {})
                    .get("mode", {}).get("requested_mode", "AUTO")
                    if remote_task is not None else "AUTO"
                ),
                requested_carrier=(
                    remote_task.metadata.get("route_context", {})
                    .get("carrier", {}).get("requested_carrier", "AUTO")
                    if remote_task is not None else "AUTO"
                ),
            )
            # Interface-only milestone: the context is observable and durable
            # for Remote work, while agent.chat() remains the sole executor.
            agent._active_route_context = route_context
            mode_context = route_context.get("mode", {})
            carrier_context = route_context.get("carrier", {})
            agent.configure_status(
                request_id=(
                    remote_task.request_id
                    if remote_task is not None
                    else test_envelope.request_id if test_envelope is not None else ""
                ),
                task_id=remote_task.task_id if remote_task is not None else "",
                source=(
                    remote_transport_context.route_source
                    if remote_transport_context is not None
                    else "local_test" if test_envelope is not None else "local"
                ),
                mode=mode_context.get("selected_mode", "GENERAL_AGENT"),
                carrier=carrier_context.get("selected_carrier", "LOCAL_CONSOLE"),
                conversation_url=remote_task.conversation_url if remote_task is not None else startup_gpt_url,
            )
            agent._publish_status("ROUTING", message="任務路由已確認")
            print("\n[Router] 本輪路由")
            for route_line in route_context_lines(route_context):
                print(f"  {route_line}")
            planner_name = MODELS[agent.planner_key]['model']
            spinner = Spinner(agent.status_publisher.display_text)
            spinner.start()
            start_time = time.time()

            try:
                if integrated_host is not None:
                    integrated_host.set_local_busy(
                        True,
                        workspace=remote_task.workspace if remote_task is not None else startup_workspace,
                        conversation_url=remote_task.conversation_url if remote_task is not None else startup_gpt_url,
                    )
                remote_monitor_stop = threading.Event()
                remote_cancel_seen = threading.Event()
                if remote_task is not None:
                    if worker_startup_heartbeat_stop is not None:
                        worker_startup_heartbeat_stop.set()
                    def _remote_lease_monitor():
                        while not remote_monitor_stop.wait(5.0):
                            try:
                                remote_queue.heartbeat(remote_task.task_id, worker_pid=os.getpid())
                                if remote_queue.cancellation_requested(remote_task.task_id):
                                    remote_cancel_seen.set()
                                    agent.cancel_current_web_request()
                                    return
                            except Exception as exc:
                                if worker_runtime_log is not None:
                                    worker_runtime_log.write(
                                        "ERROR", component="remote_worker",
                                        stage="HEARTBEAT_RETRY",
                                        task_id=remote_task.task_id,
                                        error=f"{type(exc).__name__}: {exc}",
                                    )
                                continue
                    threading.Thread(
                        target=_remote_lease_monitor,
                        name=f"remote-lease-{remote_task.request_id}",
                        daemon=True,
                    ).start()
                deterministic_action = ""
                deterministic_response = ""
                remote_artifacts = []
                if (
                    remote_worker_mode
                    and remote_task is not None
                    and str(remote_task.metadata.get("transport", "")).upper() == "TELEGRAM"
                ):
                    deterministic_response, artifact = _remote_telegram_get_file_fast_result(
                        remote_task.request.strip(), remote_task.workspace
                    )
                    if artifact is not None:
                        remote_artifacts = [artifact]
                        deterministic_action = "telegram_get_file"
                if deterministic_response:
                    agent._publish_status(
                        "TOOL_COMPLETED", actor="LOCAL_TOOL",
                        message=f"本機工具完成：{deterministic_action}",
                    )
                    if worker_runtime_log is not None:
                        worker_runtime_log.write(
                            "TASK_STARTED", component="remote_worker",
                            stage="TELEGRAM_GET_FILE_COMPLETED",
                            task_id=remote_task.task_id,
                        )
                    response = deterministic_response
                else:
                    response = agent.chat(user_input)
            except KeyboardInterrupt:
                print(
                    "\n[*] Ctrl+C：正在中止目前 LocalAgent request；"
                    "優先停止 WebGPT 目前生成，不關閉瀏覽器...",
                    flush=True,
                )
                cancel_result = agent.cancel_current_web_request()
                status = cancel_result.get("status", "unknown")
                if status in ("stopped", "already_idle", "reloaded", "not_running", "local_abort"):
                    print(
                        f"[*] 本輪已中止（WebGPT recovery={status}）。"
                        "瀏覽器/對話保留，CMD 已回到可輸入狀態。",
                        flush=True,
                    )
                elif status == "restarted":
                    print(
                        "[!] WebGPT 的 Stop/頁面恢復失敗，因此已做最後手段 browser restart；"
                        "本輪 prompt 不會自動重送。",
                        flush=True,
                    )
                else:
                    print(
                        f"[!] 本輪已中止，但 WebGPT recovery 狀態={status}: "
                        f"{cancel_result.get('error') or cancel_result.get('restart_error') or ''}",
                        flush=True,
                    )
                if remote_task is not None:
                    failed_task = remote_queue.fail(remote_task.task_id, "remote_task_interrupted_by_operator")
                    remote_event_store.emit("TASK_FAILED", failed_task, status="FAILED", payload={"error":"任務已由 1 號機操作員中止。"})
                    print(f"[RemoteAgent] 任務已中止: {remote_task.request_id}")
                    _restore_startup_context()
                if remote_worker_mode:
                    break
                if test_envelope is not None and test_signal_receiver is not None:
                    test_signal_receiver.complete(
                        test_envelope,
                        status="ABORTED",
                        error="local_request_interrupted_by_operator",
                    )
                continue
            except Exception as e:
                agent._record_issue(e, actor="LOCAL_AGENT", stage="TASK_BOUNDARY")
                agent._publish_status(
                    "FAILED", state="FAILED", message="任務失敗",
                    error=f"{type(e).__name__}: {e}",
                )
                if remote_task is not None:
                    failed_task = remote_queue.fail(remote_task.task_id, f"{type(e).__name__}: {e}")
                    remote_event_store.emit("TASK_FAILED", failed_task, status="FAILED", payload={"error": failed_task.error})
                    print(f"\n[RemoteAgent] 任務失敗: {remote_task.request_id}")
                    _restore_startup_context()
                print(f"\n[錯誤] {e}")
                print("提示：輸入 /tier 3 切換到離線模式")
                if test_envelope is not None and test_signal_receiver is not None:
                    test_signal_receiver.complete(
                        test_envelope,
                        status="FAILED",
                        error=f"{type(e).__name__}: {e}",
                    )
                if remote_worker_mode:
                    break
                continue
            finally:
                if 'remote_monitor_stop' in locals():
                    remote_monitor_stop.set()
                if integrated_host is not None:
                    integrated_host.set_local_busy(
                        False,
                        workspace=remote_task.workspace if remote_task is not None else startup_workspace,
                        conversation_url=remote_task.conversation_url if remote_task is not None else startup_gpt_url,
                    )
                spinner.stop()

            elapsed = time.time() - start_time
            print(f"Agent [Tier {agent.tier}] ({elapsed:.1f}s)")
            print("-" * 64)
            print(response)
            print("-" * 64)
            if remote_task is not None:
                if 'remote_cancel_seen' in locals() and remote_cancel_seen.is_set():
                    agent._publish_status(
                        "CANCELLED", state="CANCELLED", message="任務已取消"
                    )
                    cancelled_task = remote_queue.mark_cancelled(remote_task.task_id)
                    remote_event_store.emit("TASK_INTERRUPTED", cancelled_task, status="CANCELLED", payload={"error":"任務已取消。"})
                    print(f"[RemoteAgent] 任務已由手機取消: {remote_task.request_id}")
                    _restore_startup_context()
                    if remote_worker_mode:
                        break
                    continue
                control_stop_reason = _remote_control_stop_reason(response)
                if control_stop_reason:
                    failed_task = remote_queue.fail(remote_task.task_id, control_stop_reason)
                    remote_event_store.emit(
                        "TASK_FAILED", failed_task, status="FAILED",
                        payload={"error": control_stop_reason},
                    )
                    agent._publish_status(
                        "FAILED", state="FAILED", message="任務未完成",
                        error=control_stop_reason,
                    )
                    if worker_runtime_log is not None:
                        worker_runtime_log.write(
                            "ERROR", component="remote_worker",
                            stage="CONTROL_STOP_REJECTED_AS_RESULT",
                            task_id=remote_task.task_id,
                            error=control_stop_reason,
                        )
                    print(f"[RemoteAgent] 任務未完成: {remote_task.request_id}")
                    _restore_startup_context()
                    if remote_worker_mode:
                        break
                    continue
                agent._publish_status("COMPLETED", state="COMPLETED", message="任務完成")
                completed_task = remote_queue.complete(
                    remote_task.task_id,
                    result={"response": response, "worker_pid": os.getpid(), "elapsed_sec": round(elapsed, 3)},
                )
                completed_payload={"summary":response}
                if remote_artifacts:
                    completed_payload["workspace"]=str(Path(remote_task.workspace).resolve())
                    completed_payload["artifacts"]=remote_artifacts
                remote_event_store.emit("TASK_COMPLETED", completed_task, status="COMPLETED", payload=completed_payload)
                print(f"[RemoteAgent] 任務完成: {remote_task.request_id}")
                agent._publish_status("COMPLETED", state="COMPLETED", message="任務完成，等待回傳")
                _restore_startup_context()
                if remote_worker_mode:
                    break
            else:
                agent._publish_status("COMPLETED", state="COMPLETED", message="任務完成")
                if test_envelope is not None and test_signal_receiver is not None:
                    test_signal_receiver.complete(
                        test_envelope,
                        status="COMPLETED",
                        completion_text=f"已完成\n{response}",
                        metadata={
                            "ack_ids": list(agent._accepted_web_ack_ids),
                            "uploaded_attachments": list(agent._task_uploaded_attachments),
                            "verification_status": agent.last_verification_status,
                        },
                    )

        except KeyboardInterrupt:
            print("\n\n(Ctrl+C。輸入 /exit 退出)")
        except EOFError:
            print("\n[再見]")
            break

if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        worker = _remote_worker_cli()
        if worker.get("task_id"):
            try:
                from agent_core.task_state import RemoteTaskQueue, TaskStateStore
                from agent_core.remote_events import RemoteEventStore
                root = runtime_root()
                exit_queue = RemoteTaskQueue(TaskStateStore(remote_tasks_path()))
                failed = exit_queue.fail_running(worker["task_id"], f"{type(exc).__name__}: {exc}")
                if failed is not None:
                    RemoteEventStore(remote_events_path()).emit(
                        "TASK_FAILED", failed, status="FAILED", payload={"error": failed.error}
                    )
            except Exception:
                pass
            try:
                import traceback
                from agent_core.remote_runtime_log import RemoteRuntimeLog
                worker_dir = remote_workers_root() / worker["task_id"]
                RemoteRuntimeLog(
                    remote_runtime_log_path(),
                    mirror_console=True,
                    mirror_paths=[worker_dir / "runtime.jsonl"],
                    snapshot_path=worker_dir / "lifecycle.json",
                ).write(
                    "ERROR", component="remote_worker", stage="UNHANDLED_EXIT",
                    task_id=worker["task_id"],
                    error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc()[-4000:],
                )
                StatusPublisher(worker_dir / "status.json").publish(
                    "FAILED", state="FAILED", message="Agent1 worker 啟動或執行失敗",
                    error=f"{type(exc).__name__}: {exc}",
                    detail=f"task_id={worker['task_id']} pid={os.getpid()}",
                )
            except Exception:
                pass
        raise
