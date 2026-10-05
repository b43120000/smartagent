#!/usr/bin/env python3
"""Central semantic path accessors for SmartAgentv1 Prepare."""
from __future__ import annotations

from pathlib import Path

from .metadata import resolve_location


def source_root(root: str | Path | None = None) -> Path:
    return resolve_location("source_root", root)


def install_root(root: str | Path | None = None) -> Path:
    return resolve_location("install_root", root)


def local_data_root(root: str | Path | None = None) -> Path:
    return resolve_location("local_data_root", root)


def metadata_root(root: str | Path | None = None) -> Path:
    return resolve_location("metadata_root", root)


def secure_root(root: str | Path | None = None) -> Path:
    return resolve_location("secure_root", root)


def runtime_root(root: str | Path | None = None) -> Path:
    return resolve_location("runtime_root", root)


def cache_root(root: str | Path | None = None) -> Path:
    return resolve_location("cache_root", root)


def log_root(root: str | Path | None = None) -> Path:
    return resolve_location("log_root", root)


def protocol_manifest_path(root: str | Path | None = None) -> Path:
    return resolve_location("protocol_manifest_path", root)


def remote_binding_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_binding_path", root)



def remote_binding_external_update_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_binding_external_update_path", root)


def startup_preferences_path(root: str | Path | None = None) -> Path:
    return resolve_location("startup_preferences_path", root)


def status_metadata_path(root: str | Path | None = None) -> Path:
    return resolve_location("status_metadata_path", root)


def conversation_registry_path(root: str | Path | None = None) -> Path:
    return resolve_location("conversation_registry_path", root)

def workspace_registry_path(root: str | Path | None = None) -> Path:
    return resolve_location("workspace_registry_path", root)


def telegram_config_path(root: str | Path | None = None) -> Path:
    return resolve_location("telegram_config_path", root)


def telegram_pairing_path(root: str | Path | None = None) -> Path:
    return resolve_location("telegram_pairing_path", root)


def remote_binding_transactions_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_binding_transactions_path", root)


def workspace_manager_apply_log_path(root: str | Path | None = None) -> Path:
    return resolve_location("workspace_manager_apply_log_path", root)


def workspace_manager_launcher_log_path(root: str | Path | None = None) -> Path:
    """Prepare physical launcher log; callers must not compose .agents themselves."""
    return log_root(root) / "workspace_manager_apply.txt"


def request_ownership_state_path(root: str | Path | None = None) -> Path:
    return resolve_location("request_ownership_state_path", root)


def request_ownership_lock_path(root: str | Path | None = None) -> Path:
    return resolve_location("request_ownership_lock_path", root)


def conversation_ownership_path(root: str | Path | None = None) -> Path:
    return resolve_location("conversation_ownership_path", root)


def tri_one_runtime_root(root: str | Path | None = None) -> Path:
    return resolve_location("tri_one_runtime_root", root)


def tri_one_test_signals_root(root: str | Path | None = None) -> Path:
    return resolve_location("tri_one_test_signals_root", root)


def web_llm_debug_log_path(root: str | Path | None = None) -> Path:
    return resolve_location("web_llm_debug_log_path", root)


def remote_page_start_lock_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_page_start_lock_path", root)


def browser_operator_root(root: str | Path | None = None) -> Path:
    return resolve_location("browser_operator_root", root)


def remote_execution_page_lock_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_execution_page_lock_path", root)


def webcopilot_tasks_path(root: str | Path | None = None) -> Path:
    return resolve_location("webcopilot_tasks_path", root)


def webgpt_outbound_turns_path(root: str | Path | None = None) -> Path:
    return resolve_location("webgpt_outbound_turns_path", root)


def agent_host_state_path(root: str | Path | None = None) -> Path:
    return resolve_location("agent_host_state_path", root)


def remote_handoff_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_handoff_path", root)


def self_repair_root(root: str | Path | None = None) -> Path:
    return resolve_location("self_repair_root", root)


def web_ui_profile_root(root: str | Path | None = None) -> Path:
    return resolve_location("web_ui_profile_root", root)


def web_ui_calibration_lock_path(root: str | Path | None = None) -> Path:
    return resolve_location("web_ui_calibration_lock_path", root)


def remote_runtime_log_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_runtime_log_path", root)


def remote_runtime_state_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_runtime_state_path", root)


def remote_supervisor_state_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_supervisor_state_path", root)


def telegram_listener_state_path(root: str | Path | None = None) -> Path:
    return resolve_location("telegram_listener_state_path", root)


def dispatcher_state_path(root: str | Path | None = None) -> Path:
    return resolve_location("dispatcher_state_path", root)


def dispatcher_lock_path(root: str | Path | None = None) -> Path:
    return resolve_location("dispatcher_lock_path", root)


def remote_supervisor_lock_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_supervisor_lock_path", root)


def webgpt_submit_lock_path(root: str | Path | None = None) -> Path:
    return resolve_location("webgpt_submit_lock_path", root)


def remote_control_output_root(root: str | Path | None = None) -> Path:
    return resolve_location("remote_control_output_root", root)


def remote_tasks_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_tasks_path", root)


def remote_events_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_events_path", root)


def remote_transport_sessions_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_transport_sessions_path", root)


def telegram_offset_path(root: str | Path | None = None) -> Path:
    return resolve_location("telegram_offset_path", root)


def remote_test_runs_root(root: str | Path | None = None) -> Path:
    return resolve_location("remote_test_runs_root", root)


def remote_restart_log_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_restart_log_path", root)


def remote_workers_root(root: str | Path | None = None) -> Path:
    return resolve_location("remote_workers_root", root)


def task_progress_root(root: str | Path | None = None) -> Path:
    return resolve_location("task_progress_root", root)


def remote_skill_context_root(root: str | Path | None = None) -> Path:
    return resolve_location("remote_skill_context_root", root)


def remote_webagent_state_root(root: str | Path | None = None) -> Path:
    return resolve_location("remote_webagent_state_root", root)


def artifact_bundle_state_root(root: str | Path | None = None) -> Path:
    return resolve_location("artifact_bundle_state_root", root)



def browser_profile_root(root: str | Path | None = None) -> Path:
    return resolve_location("browser_profile_root", root)


def webgpt_rate_state_path(root: str | Path | None = None) -> Path:
    return resolve_location("webgpt_rate_state_path", root)


def webgpt_rate_state_lock_path(root: str | Path | None = None) -> Path:
    return resolve_location("webgpt_rate_state_lock_path", root)


def remote_control_state_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_control_state_path", root)


def remote_control_lock_path(root: str | Path | None = None) -> Path:
    return resolve_location("remote_control_lock_path", root)


def force_stop_flag_path(root: str | Path | None = None) -> Path:
    return runtime_root(root) / "force_stop_all_agents.flag"


def workspace_state_root(workspace: str | Path) -> Path:
    return Path(workspace).expanduser().resolve() / ".agents"


def workspace_runtime_root(workspace: str | Path) -> Path:
    return workspace_state_root(workspace) / "runtime"


def workspace_telegram_inbox_root(workspace: str | Path) -> Path:
    return workspace_state_root(workspace) / "telegram_inbox"


def workspace_project_context(workspace: str | Path) -> Path:
    return workspace_state_root(workspace) / "project_context"


def workspace_project_sync_transactions(workspace: str | Path) -> Path:
    return workspace_state_root(workspace) / "project_sync_transactions"


__all__ = [name for name in globals() if not name.startswith("_") and name not in {"Path", "resolve_location"}]
