#!/usr/bin/env python3
"""SmartAgentv1 Prepare location metadata authority.

Phase 1 intentionally preserves the existing physical layout.  Callers use
semantic location keys instead of deriving state paths themselves.  Phase 2
may remap these keys without changing the callers.
"""
from __future__ import annotations

from pathlib import Path
import os
from typing import Any

METADATA_SCHEMA = "SMARTAGENT_METADATA_V1"
LAYOUT_VERSION = 1
LAYOUT_PHASE = "SMARTAGENTV1"
DEFAULT_APP_ROOT = Path(__file__).resolve().parents[2]


def _root(root: str | Path | None = None) -> Path:
    candidate = Path(root or DEFAULT_APP_ROOT).expanduser()
    if root is not None and candidate.name.casefold() == "source" and (candidate / "agent_core").is_dir() and (candidate.parent / "localdata").is_dir():
        candidate = candidate.parent
    if os.name == "nt":
        # Win32 normal paths treat trailing dots/spaces on a path component as
        # aliases of the same physical component. Batch callers commonly pass
        # "%~dp0."; normalize that terminal alias before semantic paths are
        # derived so Python and cmd.exe share one authority string.
        raw = str(candidate)
        drive, tail = os.path.splitdrive(raw)
        if tail and not tail.endswith(("\\", "/")):
            tail = tail.rstrip(" .")
        candidate = Path(drive + tail)
    return candidate.resolve()


def build_location_metadata(root: str | Path | None = None) -> dict[str, Any]:
    base = _root(root)
    source = base / "source"
    localdata = base / "localdata"
    metadata = localdata / "metadata"
    secure = localdata / "secure"
    bindings = localdata / "bindings"
    persistent = localdata / "persistent"
    runtime = localdata / "runtime"
    cache = localdata / "cache"
    logs = localdata / "logs"
    return {
        "schema": METADATA_SCHEMA,
        "layout_version": LAYOUT_VERSION,
        "layout_phase": LAYOUT_PHASE,
        "source_root": str(source),
        "install_root": str(base),
        "local_data_root": str(localdata),
        "metadata_root": str(metadata),
        "secure_root": str(secure),
        "runtime_root": str(runtime),
        "cache_root": str(cache),
        "log_root": str(logs),
        "protocol_manifest_path": str(base / "config" / "protocol_manifest.json"),
        "remote_binding_path": str(bindings / "remote_primary_binding.json"),
        "remote_binding_external_update_path": str(runtime / "remote_binding_external_update.json"),
        "startup_preferences_path": str(metadata / "startup_preferences.json"),
        "status_metadata_path": str(metadata / "status_metadata.json"),
        "conversation_registry_path": str(bindings / "conversations.json"),
        "workspace_registry_path": str(bindings / "workspaces.json"),
        "telegram_config_path": str(secure / "telegram.enc"),
        "telegram_pairing_path": str(bindings / "telegram_pairing.json"),
        "remote_binding_transactions_path": str(runtime / "remote_binding_transactions.json"),
        "workspace_manager_apply_log_path": str(logs / "workspace_manager_apply.log"),
        "request_ownership_state_path": str(runtime / "request_ownership.json"),
        "request_ownership_lock_path": str(runtime / "request_ownership.lock"),
        "conversation_ownership_path": str(runtime / "webgpt_conversation_owners.json"),
        "tri_one_runtime_root": str(runtime / "tri_one_runtime"),
        "tri_one_test_signals_root": str(runtime / "tri_one_test_signals"),
        "web_llm_debug_log_path": str(logs / "web_llm_scraper_debug.log"),
        "remote_page_start_lock_path": str(runtime / "remote_page_start.lock"),
        "browser_operator_root": str(runtime / "browser_operator"),
        "remote_execution_page_lock_path": str(runtime / "remote_execution_page.lock"),
        "webcopilot_tasks_path": str(runtime / "webcopilot_tasks.json"),
        "webgpt_outbound_turns_path": str(runtime / "webgpt_outbound_turns.jsonl"),
        "agent_host_state_path": str(runtime / "agent_host_state.json"),
        "remote_handoff_path": str(runtime / "remote_handoff.json"),
        "self_repair_root": str(persistent / "self_repair"),
        "web_ui_profile_root": str(persistent / "web_ui_profiles"),
        "web_ui_calibration_lock_path": str(runtime / "web_ui_calibration.lock"),
        "remote_runtime_log_path": str(logs / "remote_runtime.jsonl"),
        "remote_runtime_state_path": str(runtime / "remote_runtime_state.json"),
        "remote_supervisor_state_path": str(runtime / "remote_supervisor_state.json"),
        "telegram_listener_state_path": str(runtime / "telegram_listener_state.json"),
        "dispatcher_state_path": str(runtime / "dispatcher_state.json"),
        "dispatcher_lock_path": str(runtime / "dispatcher.lock"),
        "remote_supervisor_lock_path": str(runtime / "remote_supervisor.lock"),
        "webgpt_submit_lock_path": str(runtime / "webgpt_submit.lock"),
        "remote_control_output_root": str(runtime / "remote_control_output"),
        "remote_tasks_path": str(runtime / "remote_tasks.json"),
        "remote_events_path": str(runtime / "remote_events.json"),
        "remote_transport_sessions_path": str(runtime / "remote_transport_sessions.json"),
        "telegram_offset_path": str(runtime / "remote_telegram_state.json"),
        "remote_test_runs_root": str(runtime / "remote_test_runs"),
        "remote_restart_log_path": str(logs / "remote_restart.txt"),
        "remote_workers_root": str(runtime / "remote_workers"),
        "task_progress_root": str(runtime / "task_progress"),
        "remote_skill_context_root": str(runtime / "skill_context"),
        "remote_webagent_state_root": str(runtime / "remote_webagent_state"),
        "artifact_bundle_state_root": str(persistent / "artifact_bundles"),
        "browser_profile_root": str(Path(os.environ.get("APPDATA", Path.home())) / "WebLLMScraper"),
        "webgpt_rate_state_path": str(runtime / "webgpt_rate_state.json"),
        "webgpt_rate_state_lock_path": str(runtime / "webgpt_rate_state.json.lock"),
        "remote_control_state_path": str(runtime / "remote_control.json"),
        "remote_control_lock_path": str(runtime / "remote_control.json.lock"),
    }

def resolve_location(key: str, root: str | Path | None = None) -> Path:
    metadata = build_location_metadata(root)
    if key not in metadata or not key.endswith(("_root", "_path")):
        raise KeyError(f"unknown_smartagent_location:{key}")
    return Path(str(metadata[key])).expanduser().resolve()


__all__ = [
    "LAYOUT_PHASE",
    "LAYOUT_VERSION",
    "METADATA_SCHEMA",
    "build_location_metadata",
    "resolve_location",
]
