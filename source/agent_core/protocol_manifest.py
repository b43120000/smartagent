"""Fail-closed protocol/source consistency manifest.

The three agent interfaces intentionally share the active ``agent_core``
implementation.  This module gives every process one cheap, deterministic
startup check so an old release copy cannot silently participate in a run.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

from .paths import install_root, protocol_manifest_path


MANIFEST_SCHEMA_VERSION = 1
PROTOCOL_FAMILY = "SMARTAGENT_V9"
PROTOCOL_VERSION = 9

# Keep this list small and explicit: these files define the wire/tool and
# project-sync behavior that must be identical in every process/package.
CORE_FILES = (
    'update.bat',
    'adapterUI.bat',
    'install_smart_agent.bat',
    'ACLstatus.bat',
    'reinstall_smart_agent.bat',
    'force_stop_all_agents.bat',
    'Edit_workspace.bat',
    'InstallCheckList.bat',
    'launch_remote_agent.bat',
    'launch_webcopilot_chatgpt.bat',
    'source/smart_agent.py',
    'install_smart_agent/bootstrap.ps1',
    'install_smart_agent/reinstall.ps1',
    'install_smart_agent/deploy_public_layout.ps1',
    'install_smart_agent/install.ps1',
    'install_smart_agent/configure_security.ps1',
    'install_smart_agent/acl_status.ps1',
    'install_smart_agent/security_acl_policy.ps1',
    'install_smart_agent/update.ps1',
    'install_smart_agent/manifest_update.ps1',
    'install_smart_agent/build_update_manifest.py',
    'install_smart_agent/bootstrap_webdirect.ps1',
    'install_smart_agent/prepare_install_instance.ps1',
    'install_smart_agent/complete_install.ps1',
    'install_smart_agent/finalize_provisioning.py',
    'install_smart_agent/install_milestones.ps1',
    'install_smart_agent/verify_webdirect_bootstrap.py',
    'source/agent_core/metadata.py',
    'source/agent_core/paths.py',
    'source/agent_core/path_cli.py',
    'source/agent_core/smartagent_protocol.py',
    'source/agent_core/protocol_v8.py',
    'source/agent_core/protocol_v9.py',
    'source/agent_core/narrative_bridge.py',
    'source/agent_core/capability_recovery.py',
    'source/agent_core/recovery_protocol.py',
    'source/agent_core/web_runtime.py',
    'source/agent_core/web_ui/contracts.py',
    'source/agent_core/web_ui/__init__.py',
    'source/agent_core/web_ui/provider_contract.py',
    'source/agent_core/web_ui/provider_registry.py',
    'source/agent_core/web_ui/base_adapter.py',
    'source/agent_core/web_ui/factory.py',
    'source/agent_core/web_ui/request_scope.py',
    'source/agent_core/web_ui/profile_store.py',
    'source/agent_core/ui_calibration.py',
    'source/agent_core/web_ui/providers/chatgpt/profiles.py',
    'source/agent_core/web_ui/providers/chatgpt/composer.py',
    'source/agent_core/web_ui/providers/chatgpt/compatibility.py',
    'source/agent_core/web_ui/providers/chatgpt/adapter.py',
    'source/agent_core/web_ui/providers/chatgpt/bridge.py',
    'source/agent_core/web_ui/providers/chatgpt/attachments.py',
    'source/agent_core/web_ui/providers/claude/profiles.py',
    'source/agent_core/web_ui/providers/claude/composer.py',
    'source/agent_core/web_ui/providers/claude/compatibility.py',
    'source/agent_core/web_ui/providers/claude/adapter.py',
    'source/agent_core/web_ui/providers/claude/bridge.py',
    'source/agent_core/web_ui/providers/claude/attachments.py',
    'source/agent_core/web_ui/providers/gemini/profiles.py',
    'source/agent_core/web_ui/providers/gemini/composer.py',
    'source/agent_core/web_ui/providers/gemini/compatibility.py',
    'source/agent_core/web_ui/providers/gemini/adapter.py',
    'source/agent_core/web_ui/providers/gemini/bridge.py',
    'source/agent_core/web_ui/providers/gemini/attachments.py',
    'source/agent_core/request_ownership.py',
    'source/agent_core/task_state.py',
    'source/agent_core/tool_capabilities.py',
    'source/agent_core/task_progress.py',
    'source/agent_core/path_security.py',
    'source/agent_core/security_config.py',
    'source/agent_core/security_context.py',
    'source/agent_core/workspace_access.py',
    'source/agent_core/workspace_manager.py',
    'source/agent_core/command_security.py',
    'source/agent_core/safe_file_operations.py',
    'source/agent_core/security_approval.py',
    'source/agent_core/windows_security.py',
    'source/agent_core/security_preflight.py',
    'source/agent_core/restricted_executor_client.py',
    'source/agent_core/restricted_executor_service.py',
    'source/agent_core/windows_process_job.py',
    'source/agent_core/attachment_transaction.py',
    'source/agent_core/remote_binding.py',
    'source/agent_core/remote_binding_manager.py',
    'source/agent_core/remote_skill_manager.py',
    'source/agent_core/tools.py',
    'source/agent_core/image_delivery.py',
    'source/agent_core/chunked_write.py',
    'source/agent_core/batch_apply.py',
    'source/agent_core/bounded_process.py',
    'source/agent_core/aggregated_verification.py',
    'source/agent_core/runtime_cleanup.py',
    'source/agent_core/force_stop_all_agents.py',
    'source/agent_core/remote_clean_start.py',
    'source/agent_core/host_supervisor.py',
    'source/agent_core/remote_execution_coordinator.py',
    'source/agent_core/remote_control_dispatcher.py',
    'source/agent_core/remote_events.py',
    'source/agent_core/google_drive.py',
    'source/agent_core/project_sync_message.py',
    'source/agent_core/project_access.py',
    'source/agent_core/project_sync_transaction.py',
    'source/agent_core/project_sync_protocol.py',
    'source/agent_core/project_sync_runner.py',
    'source/WebAgent/protocol.py',
    'source/WebAgent/protocol_loop.py',
    'source/WebAgent/controller.py',
    'source/WebAgent/tool_context.py',
    'source/RemoteAgent/telegram_webagent_worker.py',
    'source/RemoteAgent/telegram_transport.py',
    'source/RemoteAgent/telegram_artifacts.py',
    'source/RemoteAgent/telegram_delivery.py',
    'source/RemoteAgent/telegram_listener.py',
    'source/RemoteAgent/remote_feature_catalog.py',
    'source/RemoteAgent/remote_protocol.py',
    'source/RemoteAgent/remote_runtime.py',
    'source/agent_core/protocol_manifest.py',
)


class ProtocolManifestError(RuntimeError):
    """Raised when a process would run with an unverified protocol tree."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_path(root: str | Path) -> Path:
    return protocol_manifest_path(root)


def _software_only_mode_enabled(root: str | Path) -> bool:
    base = install_root(root)
    mode_path = base / "localdata" / "secure" / "windows_security" / "acl_mode.json"
    if mode_path.is_file():
        try:
            value = json.loads(mode_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema") != "SMARTAGENT_ACL_MODE_V1":
                return False
            mode = str(value.get("mode", "") or "").strip().lower()
            if mode not in {"on", "off"}:
                return False
            recorded = Path(str(value.get("install_root", "") or "")).expanduser()
            if not recorded.is_absolute() or recorded.resolve() != base.resolve():
                return False
            return mode == "off"
        except Exception:
            return False
    options_path = base / "localdata" / "metadata" / "provisioning_options.json"
    try:
        value = json.loads(options_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(value, dict) and value.get("schema") == "SMARTAGENT_PROVISIONING_OPTIONS_V1" and value.get("configure_security") is False


def build_protocol_manifest(root: str | Path) -> dict:
    base = install_root(root)
    files: dict[str, str] = {}
    missing: list[str] = []
    for relative in CORE_FILES:
        path = base / relative
        if not path.is_file():
            missing.append(relative)
            continue
        files[relative] = _sha256(path)
    return {
        "schema": "SMARTAGENT_PROTOCOL_MANIFEST_V1",
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "protocol_family": PROTOCOL_FAMILY,
        "protocol_version": PROTOCOL_VERSION,
        "core_files": files,
        "missing_files": missing,
    }


def write_protocol_manifest(root: str | Path, output: str | Path | None = None) -> Path:
    destination = Path(output).expanduser().resolve() if output else manifest_path(root)
    manifest = build_protocol_manifest(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{id(manifest)}.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(destination)
    return destination


def validate_protocol_manifest(
    root: str | Path,
    manifest: Mapping[str, object] | None = None,
    *,
    manifest_file: str | Path | None = None,
) -> dict:
    base = install_root(root)
    path = Path(manifest_file).expanduser().resolve() if manifest_file else manifest_path(base)
    if manifest is None:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ProtocolManifestError(f"protocol_manifest_unreadable:{path}") from exc
    if not isinstance(manifest, Mapping):
        raise ProtocolManifestError("protocol_manifest_invalid_root")
    if manifest.get("schema") != "SMARTAGENT_PROTOCOL_MANIFEST_V1":
        raise ProtocolManifestError("protocol_manifest_schema_mismatch")
    if int(manifest.get("manifest_schema_version", 0) or 0) != MANIFEST_SCHEMA_VERSION:
        raise ProtocolManifestError("protocol_manifest_schema_version_mismatch")
    if manifest.get("protocol_family") != PROTOCOL_FAMILY:
        raise ProtocolManifestError("protocol_manifest_family_mismatch")
    if int(manifest.get("protocol_version", 0) or 0) != PROTOCOL_VERSION:
        raise ProtocolManifestError("protocol_manifest_protocol_version_mismatch")
    expected = manifest.get("core_files")
    if not isinstance(expected, Mapping):
        raise ProtocolManifestError("protocol_manifest_core_files_missing")
    actual = build_protocol_manifest(base)
    if actual.get("missing_files"):
        raise ProtocolManifestError(
            "protocol_manifest_files_missing:" + ",".join(actual["missing_files"])
        )
    mismatches = [
        relative
        for relative in CORE_FILES
        if str(expected.get(relative, "")).lower() != str(actual["core_files"].get(relative, "")).lower()
    ]
    software_only = _software_only_mode_enabled(base)
    if mismatches and not software_only:
        raise ProtocolManifestError("protocol_manifest_hash_mismatch:" + ",".join(mismatches))
    return {
        "status": "PASS",
        "manifest_path": str(path),
        "protocol_family": PROTOCOL_FAMILY,
        "protocol_version": PROTOCOL_VERSION,
        "core_file_count": len(CORE_FILES),
        "hash_verification": "BYPASSED_ACL_OFF" if mismatches and software_only else "PASS",
        "hash_mismatches": mismatches,
    }


def require_protocol_manifest(root: str | Path, *, manifest_file: str | Path | None = None) -> dict:
    """Validate and return metadata, raising before any agent work starts."""
    return validate_protocol_manifest(root, manifest_file=manifest_file)


__all__ = [
    "CORE_FILES",
    "MANIFEST_SCHEMA_VERSION",
    "PROTOCOL_FAMILY",
    "PROTOCOL_VERSION",
    "ProtocolManifestError",
    "build_protocol_manifest",
    "manifest_path",
    "require_protocol_manifest",
    "validate_protocol_manifest",
    "write_protocol_manifest",
]
