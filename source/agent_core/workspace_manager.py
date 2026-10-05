"""Unified human-facing workspace/access and WebGPT binding manager."""
from __future__ import annotations

import argparse
import base64
import ctypes
import getpass
import os
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from RemoteAgent.local_telegram_config import default_store as default_telegram_store

from .conversation_registry import ConversationRegistry
from .remote_binding import DEFAULT_SKILL_PATH, load as load_remote, save as save_remote, submit_external_update
from .startup_preferences import load_startup_preferences, save_startup_preferences
from .workspace import AGENT_PROJECT_ROOT, normalize_web_conversation_url
from .workspace_access import (
    access_snapshot,
    require_writable_workspace,
    save_workspace_access_policy,
    software_only_mode_enabled,
)
from .windows_security import acl_mode, default_profile_path
from .paths import remote_binding_path, telegram_config_path, telegram_pairing_path, workspace_manager_apply_log_path


def _apply_log_path() -> Path:
    path = workspace_manager_apply_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_apply_log(message: str) -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with _apply_log_path().open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def _install_apply_crash_logger() -> None:
    def handle_exception(exc_type, exc_value, exc_traceback):
        import traceback
        rendered = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        _write_apply_log(f"UNHANDLED_EXCEPTION\n{rendered}")
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
    sys.excepthook = handle_exception


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _acl_off_enabled() -> bool:
    return software_only_mode_enabled()


def _ensure_off_workspace_registered(workspace: str | Path) -> str:
    candidate = str(Path(workspace).expanduser().resolve())
    if not _acl_off_enabled():
        raise RuntimeError("custom_workspace_requires_acl_off")
    candidate_path = Path(candidate)
    if not candidate_path.is_dir():
        raise ValueError(f"workspace_unavailable:{candidate}")
    install_root = AGENT_PROJECT_ROOT.resolve()
    if (
        candidate_path == install_root
        or candidate_path in install_root.parents
        or install_root in candidate_path.parents
    ):
        raise ValueError(f"workspace_overlaps_install_root:{candidate}")
    try:
        with tempfile.NamedTemporaryFile(prefix=".smartagent-write-check-", dir=candidate, delete=True):
            pass
    except OSError as exc:
        raise ValueError(f"workspace_not_writable:{candidate}") from exc
    return require_writable_workspace(candidate)


def _remote_runtime_live() -> bool:
    from .runtime_cleanup import RuntimeCleanupManager
    state = RuntimeCleanupManager(AGENT_PROJECT_ROOT, "remote").read_state()
    return (
        str(state.get("status", "")).upper() == "RUNNING"
        and time.time() - float(state.get("heartbeat_at", 0) or 0) < 10.0
    )


def _management_access_snapshot() -> dict:
    if _acl_off_enabled():
        snapshot = access_snapshot()
        result = dict(snapshot)
        result["security_status"] = "SOFTWARE_ONLY"
        return result
    try:
        snapshot = access_snapshot()
    except RuntimeError as exc:
        # Missing security is a valid first-run management state. Any other
        # profile/load failure remains fail-closed and is intentionally raised.
        if not str(exc).startswith("security_profile_missing:"):
            raise
        software_only = _acl_off_enabled()
        return {
            "security_status": "SOFTWARE_ONLY" if software_only else "NOT_CONFIGURED",
            "security_error": str(exc),
            "profile": {},
            "writable_workspaces": (),
            "read_only_roots": (),
        }
    result = dict(snapshot)
    result["security_status"] = "CONFIGURED"
    return result


def _binding_snapshot() -> dict:
    access = _management_access_snapshot()
    try:
        remote = load_remote(AGENT_PROJECT_ROOT)
    except Exception:
        remote = {}
    return {
        "security_status": access["security_status"],
        "writable_workspaces": list(access["writable_workspaces"]),
        "read_only_roots": list(access["read_only_roots"]),
        "local": load_startup_preferences(),
        "remote": remote,
    }


def _save_binding(mode: str, workspace: str, url: str) -> dict:
    workspace = require_writable_workspace(workspace)
    url = normalize_web_conversation_url(url)
    registry = ConversationRegistry(); registry.load()
    if mode == "local":
        current = load_startup_preferences()
        saved = save_startup_preferences(
            workspace=workspace, gpt_url=url,
            planner_key=str(current.get("planner_key") or "web_chatgpt"),
            executor_key=str(current.get("executor_key") or "cloud_gptoss"),
            operator_key=str(current.get("operator_key") or "cloud_gptoss"),
        )
        registry.upsert_binding(workspace, url)
        return {"mode": mode, "status": "APPLIED", "binding": saved}

    try:
        current_remote = load_remote(AGENT_PROJECT_ROOT)
        skill_path = current_remote.get("skill_path", str(DEFAULT_SKILL_PATH))
    except Exception:
        skill_path = str(DEFAULT_SKILL_PATH)
    candidate = {"workspace": workspace, "gpt_url": url, "skill_path": skill_path}
    if _remote_runtime_live():
        row = submit_external_update(candidate, AGENT_PROJECT_ROOT)
        return {"mode": mode, "status": "PENDING_IDLE_APPLY", "request_id": row["request_id"], "binding": candidate}
    settings_path = remote_binding_path()
    registry_path = Path(registry.path)
    telegram_path = telegram_config_path()
    snapshots = {
        path: path.read_bytes() if path.is_file() else None
        for path in (settings_path, registry_path, telegram_path)
    }
    try:
        saved = save_remote(candidate, AGENT_PROJECT_ROOT)
        registry.upsert_binding(workspace, url)
        registry.configure_remote_conversation(workspace, url, enabled=True, poll_profile="normal", transport="WEBGPT")
    except Exception:
        for path, payload in snapshots.items():
            if payload is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + ".rollback")
                temporary.write_bytes(payload); temporary.replace(path)
        raise
    return {"mode": mode, "status": "APPLIED_ON_NEXT_START", "binding": saved}


def _choose(label: str, values: list[str]) -> str:
    if not values:
        raise RuntimeError(f"{label}_empty")
    print(f"\n{label}")
    for index, value in enumerate(values, 1):
        print(f"  [{index}] {value}")
    while True:
        raw = input("請選擇編號: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(values):
            return values[int(raw) - 1]
        print("  [!] 選項不存在。")


def _choose_workspace(label: str, values: list[str]) -> str:
    allow_custom = _acl_off_enabled()
    if not values and not allow_custom:
        raise RuntimeError(f"{label}_empty")
    print(f"\n{label}")
    for index, value in enumerate(values, 1):
        print(f"  [{index}] {value}")
    if allow_custom:
        print("  [0] 輸入其他 Workspace 路徑")
    while True:
        raw = input("請選擇編號: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(values):
            return values[int(raw) - 1]
        if raw == "0" and allow_custom:
            entered = input("Workspace 路徑: ").strip().strip('"')
            if not entered:
                print("  [!] 路徑不可為空。")
                continue
            try:
                return _ensure_off_workspace_registered(entered)
            except Exception as exc:
                print(f"  [!] Workspace 無法授權：{type(exc).__name__}: {exc}")
                continue
        print("  [!] 選項不存在。")


def _binding_menu() -> int:
    snapshot = _binding_snapshot()
    if not snapshot["writable_workspaces"] and snapshot["security_status"] != "SOFTWARE_ONLY":
        print("\n[!] Security is not configured. Configure Security before binding a workspace.")
        return 2
    print("\n[2-0] 選擇設定目標")
    print("  [1] Local（LocalAgent 與 WebCopilot）")
    print("  [2] RemoteAgent")
    mode_raw = input("請選擇: ").strip()
    if mode_raw not in {"1", "2"}:
        print("[!] 已取消。")
        return 1
    mode = "local" if mode_raw == "1" else "remote"
    workspace = _choose_workspace("[2-1] 選擇可讀寫 Workspace", snapshot["writable_workspaces"])
    current = snapshot[mode]
    print(f"目前 WebGPT URL：{current.get('gpt_url', '（尚未設定）')}")
    url = input("WebGPT 對話頁面 URL: ").strip()
    result = _save_binding(mode, workspace, url)
    print("\n[OK] 設定完成")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _editable_read_only(snapshot: dict) -> list[str]:
    profile = snapshot["profile"]
    protected = {
        str(Path(value).resolve()).casefold()
        for value in (*tuple(profile.get("skill_roots") or ()), profile.get("executor_code_root", ""))
        if str(value or "").strip()
    }
    return [value for value in snapshot["read_only_roots"] if value.casefold() not in protected]


def _path_key(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve()).casefold()


def _encode_access_roots(
    writable: list[str], read_only: list[str], denied: list[str]
) -> tuple[str, dict[str, list[str]]]:
    payload = {
        "additional_write_roots": list(writable[1:]),
        "read_only_roots": list(read_only),
        "denied_write_roots": list(denied),
    }
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return encoded, payload


def _verify_access_postcondition(writable: list[str], read_only: list[str]) -> None:
    applied = access_snapshot()
    applied_profile = applied["profile"]
    expected_writable = {_path_key(value) for value in writable}
    actual_writable = {_path_key(value) for value in applied["writable_workspaces"]}
    protected_read_only = [
        *list(applied_profile.get("skill_roots") or ()),
        applied_profile.get("executor_code_root", ""),
    ]
    expected_read_only = {
        _path_key(value)
        for value in [*read_only, *protected_read_only]
        if str(value or "").strip()
    }
    actual_read_only = {_path_key(value) for value in applied["read_only_roots"]}
    mismatches = []
    if actual_writable != expected_writable:
        mismatches.append(
            f"writable expected={sorted(expected_writable)} actual={sorted(actual_writable)}"
        )
    if actual_read_only != expected_read_only:
        mismatches.append(
            f"read_only expected={sorted(expected_read_only)} actual={sorted(actual_read_only)}"
        )
    if mismatches:
        raise RuntimeError("security_profile_postcondition_failed:" + "; ".join(mismatches))


def _apply_access(writable: list[str], read_only: list[str], profile: dict) -> int:
    if not writable:
        raise ValueError("at_least_one_writable_workspace_required")
    script = AGENT_PROJECT_ROOT / "install_smart_agent" / "configure_security.ps1"
    command = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-WorkspaceContainer", writable[0],
    ]
    def overlaps(left: str, right: str) -> bool:
        a = Path(left).resolve(); b = Path(right).resolve()
        try: a.relative_to(b); return True
        except ValueError: pass
        try: b.relative_to(a); return True
        except ValueError: return False
    active_access = [*writable, *read_only]
    denied = [str(Path(value).resolve()) for value in profile.get("denied_write_roots") or () if str(value or "").strip()]
    for denied_root in denied:
        for allowed in active_access:
            if overlaps(denied_root, allowed):
                raise ValueError(
                    f"access_root_overlaps_denied_root:{allowed}<->{denied_root}"
                )
    access_roots_base64, access_roots_payload = _encode_access_roots(
        writable, read_only, denied
    )
    command.extend(["-AccessRootsBase64", access_roots_base64])
    # Older security profiles could persist the executor profile path as the
    # account value. Always pass a validated short local account name.
    executor_user = str(profile.get("executor_user") or "SmartAgentExecutor").strip()
    if "\\" in executor_user or "/" in executor_user:
        executor_user = Path(executor_user).name
    if not executor_user or len(executor_user) > 20 or not executor_user.replace(".", "").replace("-", "").replace("_", "").isalnum():
        executor_user = "SmartAgentExecutor"
    command.extend([
        "-RuntimeRoot", str(profile["runtime_root"]),
        "-SkillRoot", str((profile.get("skill_roots") or [profile.get("executor_code_root")])[0]),
        "-SkillSourcePath", "",
        "-ExecutorUser", executor_user,
        "-ProfilePath", str(default_profile_path()),
        "-ExecutorPython", str(profile["executor_python"]),
        "-Apply",
    ])
    _write_apply_log(
        "APPLY_ACCESS_REQUEST "
        + json.dumps(access_roots_payload, ensure_ascii=False, sort_keys=True)
    )
    redacted_command = [
        "<access-roots-base64>" if index and command[index - 1] == "-AccessRootsBase64" else value
        for index, value in enumerate(command)
    ]
    _write_apply_log(
        "APPLY_START "
        + " ".join(str(value) for value in redacted_command if value != "-Apply")
    )
    result = subprocess.run(
        command,
        cwd=str(AGENT_PROJECT_ROOT),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    _write_apply_log(
        f"APPLY_EXIT code={result.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    )
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    if result.returncode == 0:
        try:
            save_workspace_access_policy(writable, read_only, denied)
            _verify_access_postcondition(writable, read_only)
        except Exception as exc:
            _write_apply_log(
                f"APPLY_POSTCONDITION_FAILED type={type(exc).__name__} message={exc}"
            )
            raise
        _write_apply_log("APPLY_POSTCONDITION_OK")
    return result.returncode


def _apply_software_only_access(
    writable: list[str], read_only: list[str], profile: dict
) -> int:
    """Publish software authorization only; never mutate NTFS ACLs or executor state."""
    denied = [
        str(Path(value).resolve())
        for value in profile.get("denied_write_roots") or ()
        if str(value or "").strip()
    ]
    policy = save_workspace_access_policy(writable, read_only, denied)
    _write_apply_log(
        "APPLY_SOFTWARE_ONLY_POLICY "
        + json.dumps(policy, ensure_ascii=False, sort_keys=True)
    )
    _verify_access_postcondition(writable, read_only)
    return 0


def _suggest_first_run_workspace() -> Path:
    candidates = [
        Path.home() / "Desktop" / "SmartAgentWorkspace" / "default",
        Path.home() / "OneDrive" / "Desktop" / "SmartAgentWorkspace" / "default",
        Path.home() / "SmartAgentWorkspaces" / "default",
    ]
    try:
        remote = load_remote(AGENT_PROJECT_ROOT)
        configured = str(remote.get("workspace", "") or "").strip()
        if configured:
            candidates.insert(0, Path(configured))
    except Exception:
        pass
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    return candidates[0].resolve()


def _initialize_security_profile() -> int:
    suggested = _suggest_first_run_workspace()
    print("\nSecurity is not configured on this PC.")
    print("This setup provisions the existing restricted-executor/NTFS security profile.")
    raw = input(f"Writable workspace [{suggested}]: ").strip().strip('\"')
    workspace = Path(raw or suggested).expanduser().resolve()
    if not workspace.is_dir():
        print(f"[!] Workspace does not exist: {workspace}")
        _write_apply_log(f"INITIAL_SECURITY_INVALID_WORKSPACE workspace={workspace}")
        return 2
    if (
        workspace == AGENT_PROJECT_ROOT
        or workspace in AGENT_PROJECT_ROOT.parents
        or AGENT_PROJECT_ROOT in workspace.parents
    ):
        print("[!] Workspace cannot be the SmartAgent install folder or one of its parent/child folders.")
        print(f"    SmartAgent: {AGENT_PROJECT_ROOT}")
        print(f"    Workspace : {workspace}")
        _write_apply_log(f"INITIAL_SECURITY_INSTALL_ROOT_OVERLAP workspace={workspace}")
        return 2
    script = AGENT_PROJECT_ROOT / "install_smart_agent" / "configure_security.ps1"
    command = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-WorkspaceContainer", str(workspace),
        "-ExecutorPython", str(sys.executable),
        "-Apply",
    ]
    _write_apply_log(f"INITIAL_SECURITY_START workspace={workspace}")
    result = subprocess.run(command, cwd=str(AGENT_PROJECT_ROOT), check=False)
    _write_apply_log(f"INITIAL_SECURITY_EXIT workspace={workspace} code={result.returncode}")
    if result.returncode == 0:
        applied = access_snapshot()
        save_workspace_access_policy(
            applied["writable_workspaces"],
            _editable_read_only(applied),
            applied["profile"].get("denied_write_roots") or (),
        )
        print("[OK] Security profile configured and verified.")
    return int(result.returncode)


def _security_menu() -> int:
    software_only = _acl_off_enabled()
    if not software_only and not _is_admin():
        raise RuntimeError("security_menu_requires_administrator")
    management = _management_access_snapshot()
    if management["security_status"] == "NOT_CONFIGURED":
        code = _initialize_security_profile()
        if code != 0:
            return code
    snapshot = access_snapshot()
    profile = snapshot["profile"]
    writable = list(snapshot["writable_workspaces"])
    read_only = _editable_read_only(snapshot)
    while True:
        print("\nSecurity / Workspace Access")
        print("Writable workspaces:")
        for i, value in enumerate(writable, 1):
            print(f"  W{i}. {value}")
        print("Read-only roots:")
        for i, value in enumerate(read_only, 1):
            print(f"  R{i}. {value}")
        print("\n[1] Add writable  [2] Remove writable  [3] Add read-only  [4] Remove read-only  [5] Apply  [0] Back")
        choice = input("Select: ").strip()
        if choice in {"1", "3"}:
            path = str(Path(input("Path: ").strip().strip('\"')).expanduser().resolve())
            if not Path(path).is_dir():
                print("[!] Path does not exist or is not a directory.")
                continue
            target = writable if choice == "1" else read_only
            if path not in target:
                target.append(path)
        elif choice in {"2", "4"}:
            target = writable if choice == "2" else read_only
            try:
                del target[int(input("Index: ").strip()) - 1]
            except (ValueError, IndexError):
                print("[!] Invalid index.")
        elif choice == "5":
            try:
                return_code = (
                    _apply_software_only_access(writable, read_only, profile)
                    if software_only
                    else _apply_access(writable, read_only, profile)
                )
                print(f"\nAPPLY_EXIT_CODE={return_code}")
                _write_apply_log(f"APPLY_MENU_RESULT code={return_code}")
                return return_code
            except Exception as exc:
                _write_apply_log(f"APPLY_EXCEPTION type={type(exc).__name__} message={exc}")
                print(f"\nAPPLY_EXCEPTION: {type(exc).__name__}: {exc}")
                return 1
            finally:
                input("\nApply finished. Press Enter to continue...")
        elif choice == "0":
            return 0


def _elevated_workspace_manager_command(argument: str) -> list[str]:
    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    source_root = AGENT_PROJECT_ROOT / "source"
    elevated_log = AGENT_PROJECT_ROOT / "localdata" / "logs" / "workspace_manager_elevated.txt"
    elevated_payload = (
        f"$env:PYTHONPATH={quote(str(source_root))} + [IO.Path]::PathSeparator + $env:PYTHONPATH; "
        "$env:PYTHONSAFEPATH='1'; "
        f"$transcriptPath={quote(str(elevated_log))}; $exitCode=1; "
        "try { "
        "Start-Transcript -LiteralPath $transcriptPath -Append | Out-Null; "
        f"& {quote(sys.executable)} -B -m agent_core.workspace_manager {argument}; "
        "$exitCode=$LASTEXITCODE "
        "} catch { "
        "$message=($_ | Out-String); Write-Host $message; "
        "try { Add-Content -LiteralPath $transcriptPath -Value $message -Encoding UTF8 } catch {} "
        "} finally { try { Stop-Transcript | Out-Null } catch {} }; "
        "exit $exitCode"
    )
    encoded_payload = base64.b64encode(
        elevated_payload.encode("utf-16-le")
    ).decode("ascii")
    command = (
        "$p=Start-Process -FilePath 'powershell.exe' "
        f"-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','{encoded_payload}') "
        f"-WorkingDirectory {quote(str(AGENT_PROJECT_ROOT))} -Verb RunAs -Wait -PassThru; "
        "exit $p.ExitCode"
    )
    return ["powershell", "-NoProfile", "-Command", command]


def _run_elevated_workspace_manager(argument: str) -> int:
    elevated_log = AGENT_PROJECT_ROOT / "localdata" / "logs" / "workspace_manager_elevated.txt"
    elevated_log.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        _elevated_workspace_manager_command(argument),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        outer_detail = (result.stderr or result.stdout or "").strip()
        detail = outer_detail or f"security_workflow_failed; see {elevated_log}"
        _write_apply_log(
            f"ELEVATED_LAUNCH_FAILED argument={argument} code={result.returncode} detail={detail}"
        )
        print(f"[!] Administrator workflow did not complete: {detail}")
    return int(result.returncode)


def _launch_elevated_security_menu() -> int:
    return _run_elevated_workspace_manager("--security-menu")


def _launch_elevated_security_initialize() -> int:
    """Run only first-time security initialization, then return to the wizard."""
    return _run_elevated_workspace_manager("--initialize-security")


def _telegram_safe_status() -> dict:
    store = default_telegram_store(AGENT_PROJECT_ROOT)
    if not store.path.is_file():
        return {"status": "NOT_CONFIGURED", "configured": False, "enabled": False, "workspace": ""}
    try:
        config = store.load()
    except Exception as exc:
        return {
            "status": "CREDENTIAL_UNAVAILABLE",
            "configured": False,
            "enabled": False,
            "workspace": "",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "status": "ENABLED" if config.get("enabled") else "DISABLED",
        "configured": True,
        "enabled": bool(config.get("enabled")),
        "workspace": str(config.get("workspace", "")),
        "provider": str(config.get("provider", "")),
    }


def _telegram_workspace_choices() -> list[str]:
    access = _management_access_snapshot()
    return list(access["writable_workspaces"]) if access["security_status"] in {"CONFIGURED", "SOFTWARE_ONLY"} else []


def _clear_telegram_pairing_state() -> None:
    pairing = telegram_pairing_path()
    pairing.unlink(missing_ok=True)


def _start_remote_agent_console() -> None:
    launcher = AGENT_PROJECT_ROOT / "launch_remote_agent.bat"
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(
        [comspec, "/c", str(launcher)],
        cwd=str(AGENT_PROJECT_ROOT),
        creationflags=flags,
    )


def _validate_telegram_bot_token(token: str, workspace: str) -> tuple[bool, str]:
    """Verify bot identity without exposing the token or changing local state."""
    from RemoteAgent.telegram_transport import TelegramBotClient, TelegramReceiverConfig

    config = TelegramReceiverConfig(
        enabled=True,
        bot_token=str(token or "").strip(),
        workspace=str(workspace or "").strip(),
        pairing_enabled=True,
    )
    try:
        config.validate()
        profile = TelegramBotClient(config).get_me()
    except RuntimeError as exc:
        message = str(exc)
        if message.startswith("telegram_api_http_error:401:") or message.startswith("telegram_api_http_error:404:"):
            return False, "INVALID_CREDENTIAL"
        return False, message
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    username = str(profile.get("username", "") or "").strip()
    if not username:
        return False, "telegram_bot_username_missing"
    return True, username



def _verify_remote_standalone_ready(workspace: str) -> tuple[bool, str]:
    """Verify persisted RemoteAgent state from a fresh-process point of view."""
    workspace = require_writable_workspace(workspace)
    settings_path = remote_binding_path()
    if not settings_path.is_file():
        return False, f"remote_binding_not_persisted:{settings_path}"
    try:
        binding = load_remote(AGENT_PROJECT_ROOT)
    except Exception as exc:
        return False, f"remote_binding_reload_failed:{type(exc).__name__}:{exc}"
    if str(binding.get("workspace", "")).casefold() != workspace.casefold():
        return False, f"remote_binding_workspace_mismatch:{binding.get('workspace', '')}"

    env = dict(os.environ)
    for name in (
        "SMARTAGENT_REMOTE_BINDING_SNAPSHOT",
        "SMARTAGENT_TELEGRAM_WORKSPACE",
        "SMARTAGENT_REMOTE_EXECUTION_URL",
        "SMARTAGENT_SKILL_PATH",
    ):
        env.pop(name, None)

    preflight = subprocess.run(
        [sys.executable, "-m", "agent_core.security_preflight", "--interface", "remote"],
        cwd=str(AGENT_PROJECT_ROOT), env=env, check=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if preflight.returncode != 0:
        detail = (preflight.stderr or preflight.stdout or "security_preflight_failed").strip()
        return False, f"remote_security_preflight_failed:{detail}"

    launcher = AGENT_PROJECT_ROOT / "launch_remote_agent.bat"
    if not launcher.is_file():
        return False, f"remote_launcher_missing:{launcher}"
    launch_check = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(launcher), "--launcher-self-test"],
        cwd=str(AGENT_PROJECT_ROOT), env=env, check=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    output = (launch_check.stdout or "") + (launch_check.stderr or "")
    if launch_check.returncode != 0 or "REMOTE_AGENT_LAUNCHER_PARSE_OK" not in output:
        return False, f"remote_launcher_self_test_failed:exit={launch_check.returncode}"
    return True, "READY"

def _run_telegram_pairing(config: dict) -> int:
    env = dict(os.environ)
    env["SMARTAGENT_TELEGRAM_BOT_TOKEN"] = config["bot_token"]
    env["SMARTAGENT_TELEGRAM_WORKSPACE"] = config["workspace"]
    env["SMARTAGENT_TELEGRAM_ENABLED"] = "1"
    env["SMARTAGENT_TELEGRAM_PAIRING_ENABLED"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "RemoteAgent.telegram_pairing"],
        cwd=str(AGENT_PROJECT_ROOT),
        env=env,
        check=False,
    )
    if result.returncode != 0:
        return int(result.returncode)
    if _remote_runtime_live():
        print("[OK] RemoteAgent is already running; it can consume the pairing update.")
    else:
        _start_remote_agent_console()
        print("[OK] RemoteAgent receiver started in a new console.")
    from RemoteAgent.telegram_pairing import TelegramPairingStore
    pairing_store = TelegramPairingStore(
        telegram_pairing_path()
    )
    for attempt in range(3):
        input(
            "\n在手機 Telegram 掃描 QR、開啟 Bot 並按下 Start；"
            "完成後按 Enter 檢查配對..."
        )
        paired = pairing_store.paired_chat_ids()
        if paired:
            print("[OK] Telegram 配對完成，RemoteAgent 已授權此 Telegram 對話。")
            return 0
        if attempt < 2:
            print("[!] 尚未收到配對。請確認已在 Bot 對話中按下 Start，再重新檢查。")
    print("[!] 尚未完成配對。設定已保留，可從 Telegram 管理選擇重新配對。")
    return 2


def _ensure_local_webgpt_binding(workspace: str) -> int:
    """Ensure the selected writable workspace has a Local/WebCopilot startup binding."""
    workspace = require_writable_workspace(workspace)
    current = load_startup_preferences()
    current_workspace = str(current.get("workspace", "") or "").strip()
    current_url = str(current.get("gpt_url", "") or "").strip()

    if current_url and current_workspace:
        try:
            current_workspace = require_writable_workspace(current_workspace)
        except Exception:
            current_workspace = ""
        if current_workspace and current_workspace.casefold() == workspace.casefold():
            print(f"[OK] Local/WebCopilot startup binding exists: {workspace}")
            return 0
        if current_url:
            _save_binding("local", workspace, current_url)
            print(f"[OK] Local/WebCopilot workspace updated: {workspace}")
            return 0

    print("[!] Local/WebCopilot startup binding is not configured yet.")
    print("    A writable workspace and ChatGPT conversation URL are required.")
    while True:
        url = input("Local/WebCopilot ChatGPT conversation URL: ").strip()
        try:
            _save_binding("local", workspace, url)
            print("[OK] Local/WebCopilot Workspace + WebGPT binding saved.")
            return 0
        except Exception as exc:
            print(f"[!] Invalid or unsaved Local/WebCopilot URL: {exc}")


def ensure_local_webgpt_startup() -> int:
    """Interactive first-run bootstrap used by launch_webcopilot_chatgpt.bat."""
    current = load_startup_preferences()
    current_workspace = str(current.get("workspace", "") or "").strip()
    current_url = str(current.get("gpt_url", "") or "").strip()
    if current_workspace and current_url:
        try:
            require_writable_workspace(current_workspace)
            return 0
        except Exception:
            print("[WebAgent] Saved Local/WebCopilot workspace is no longer writable on this PC; reconfiguring.")

    access = _management_access_snapshot()
    if access["security_status"] not in {"CONFIGURED", "SOFTWARE_ONLY"}:
        print("[WebAgent] First run: workspace security is not configured on this PC.")
        code = _initialize_security_profile() if _is_admin() else _launch_elevated_security_initialize()
        if code != 0:
            print(f"[WebAgent] Workspace security initialization failed (exit={code}).")
            return int(code or 1)
        access = _management_access_snapshot()

    workspaces = list(access.get("writable_workspaces") or ())
    if not workspaces:
        if access["security_status"] == "SOFTWARE_ONLY":
            workspace = _choose_workspace("Select Local/WebCopilot workspace", workspaces)
            return _ensure_local_webgpt_binding(workspace)
        print("[WebAgent] No writable workspace is configured. Run Edit_workspace.bat to configure one.")
        return 2

    if current_workspace in workspaces:
        workspace = current_workspace
    elif len(workspaces) == 1:
        workspace = workspaces[0]
        print(f"[WebAgent] Using the only writable workspace: {workspace}")
    else:
        workspace = _choose("Select Local/WebCopilot writable workspace", workspaces)

    return _ensure_local_webgpt_binding(workspace)

def _telegram_first_time_wizard() -> int:
    """Linear setup/re-pair flow that reuses a saved credential when available."""
    print("\n========================================")
    print(" SmartAgent Telegram 配對精靈")
    print("========================================")
    existing = _telegram_safe_status()
    credential_exists = bool(existing.get("configured"))
    if credential_exists:
        print("已找到這台電腦儲存的 Telegram Bot 憑證，不需要重新輸入 Bot Token。")
    print("這個精靈會依序完成：")
    print("  1. 確認可寫入的 Workspace")
    print("  2. 確認 RemoteAgent 使用的 WebGPT 對話")
    print("  3. 確認 Bot Token（已儲存時自動略過輸入）")
    print("  4. 掃描 QR 並完成配對")

    print("\n[Step 1/5] Confirm writable Workspace")
    access = _management_access_snapshot()
    if access["security_status"] == "SOFTWARE_ONLY":
        print("ACL is OFF for this installation. A writable workspace can be selected without Windows security provisioning.")
    elif access["security_status"] != "CONFIGURED":
        print("這台電腦尚未設定 Workspace 權限。接下來會開啟 Windows 管理員視窗。")
        print("請確認或輸入一個可寫入的 Workspace；安全設定完成後本精靈會繼續。")
        code = _initialize_security_profile() if _is_admin() else _launch_elevated_security_initialize()
        if code != 0:
            print(f"[!] Workspace 安全設定未完成（exit={code}）。")
            return int(code or 1)
        access = _management_access_snapshot()
    workspaces = list(access.get("writable_workspaces") or ())
    if not workspaces and access["security_status"] != "SOFTWARE_ONLY":
        print("[!] 沒有可用的 Writable Workspace。請先完成 Workspace 權限設定。")
        return 2
    store = default_telegram_store(AGENT_PROJECT_ROOT)
    saved_config = store.load() if credential_exists else {}
    saved_workspace = str(saved_config.get("workspace", ""))
    if saved_workspace in workspaces:
        workspace = saved_workspace
        print(f"[OK] 沿用已設定的 Telegram Workspace：{workspace}")
    elif len(workspaces) == 1:
        workspace = workspaces[0]
        print(f"[OK] 自動使用唯一的 Writable Workspace：{workspace}")
    else:
        workspace = _choose_workspace("請選擇 Telegram 可以操作的 Writable Workspace", workspaces)

    print("\n[Step 2/5] Confirm Local/WebCopilot WebGPT binding")
    code = _ensure_local_webgpt_binding(workspace)
    if code != 0:
        return int(code)

    print("\n[Step 3/5] Confirm RemoteAgent WebGPT binding")
    try:
        remote_binding = load_remote(AGENT_PROJECT_ROOT)
    except Exception:
        remote_binding = {}
    if remote_binding:
        if str(remote_binding.get("workspace", "")).casefold() != workspace.casefold():
            _save_binding("remote", workspace, str(remote_binding["gpt_url"]))
            print(f"[OK] RemoteAgent Workspace 已更新：{workspace}")
        else:
            print("[OK] 沿用已設定的 RemoteAgent WebGPT 對話。")
    else:
        print("RemoteAgent 尚未綁定 WebGPT。請貼上要讓 Telegram 任務使用的 ChatGPT 對話 URL。")
        while True:
            url = input("RemoteAgent WebGPT 對話 URL: ").strip()
            try:
                _save_binding("remote", workspace, url)
                print("[OK] RemoteAgent Workspace + WebGPT 已完成綁定。")
                break
            except Exception as exc:
                print(f"[!] URL 無效或無法儲存：{exc}")

    print("\n[Step 4/5] Confirm Telegram Bot Token")
    if credential_exists:
        saved_token = str(saved_config.get("bot_token", "") or "")
        token_ok, token_detail = _validate_telegram_bot_token(saved_token, workspace)
        if not token_ok:
            if token_detail == "INVALID_CREDENTIAL":
                print("[!] ???? Telegram Bot Token ?????? Telegram ???")
                print("????? Bot Token???????? Token ???????????")
                token = getpass.getpass("New Bot Token??????: ").strip()
                if not token:
                    print("[!] ????? Bot Token????????????")
                    return 2
                token_ok, token_detail = _validate_telegram_bot_token(token, workspace)
                if not token_ok:
                    print(f"[!] ? Bot Token ?????{token_detail}")
                    return 2
                store.save(bot_token=token, workspace=workspace, enabled=True)
                print(f"[OK] ? Bot Token ?? Telegram ????????@{token_detail}??")
            else:
                print(f"[!] ???????? Bot Token?{token_detail}")
                print("??????????????????????? Token?")
                return 2
        else:
            if saved_workspace != workspace:
                store.update_workspace(workspace)
            store.set_enabled(True)
            print(f"[OK] ???? Bot Token ?????@{token_detail}?????????")
    else:
        print("?? Telegram ? @BotFather ?? Bot??? Bot Token ????")
        print("Token ???????? Windows DPAPI ????? Windows ??????")
        token = getpass.getpass("Bot Token??????: ").strip()
        if not token:
            print("[!] ??? Bot Token?????????")
            return 2
        token_ok, token_detail = _validate_telegram_bot_token(token, workspace)
        if not token_ok:
            print(f"[!] Bot Token ?????{token_detail}")
            return 2
        store.save(bot_token=token, workspace=workspace, enabled=True)
        print(f"[OK] Bot Token ?? Telegram ????????@{token_detail}??")
    _clear_telegram_pairing_state()

    print("\n[Step 5/6] Complete Telegram pairing")
    print("接下來會顯示 QR Code，並啟動 RemoteAgent 接收配對訊息。")
    pairing_code = _run_telegram_pairing(store.load())
    if pairing_code != 0:
        return int(pairing_code)

    print("\n[Step 6/6] Verify standalone RemoteAgent startup")
    ready, detail = _verify_remote_standalone_ready(workspace)
    if not ready:
        print(f"[!] RemoteAgent initialization is incomplete: {detail}")
        return 3
    print("[OK] RemoteAgent initialization complete. launch_remote_agent.bat is ready for standalone startup.")
    return 0


def _telegram_set_or_replace() -> int:
    workspaces = _telegram_workspace_choices()
    if not workspaces and not _acl_off_enabled():
        print("[!] Security is not configured. Configure Security first so Telegram binds only to an authorized writable workspace.")
        return 2
    workspace = _choose_workspace("Telegram writable workspace", workspaces)
    token = getpass.getpass("Telegram bot token (hidden): ").strip()
    if not token:
        print("[!] Bot token is required.")
        return 2
    store = default_telegram_store(AGENT_PROJECT_ROOT)
    previous_token = ""
    try:
        previous_token = str(store.load().get("bot_token", ""))
    except Exception:
        pass
    store.save(bot_token=token, workspace=workspace, enabled=True)
    if previous_token and previous_token != token:
        _clear_telegram_pairing_state()
    print("[OK] Telegram credential saved with Windows DPAPI for the current user.")
    return _run_telegram_pairing(store.load())


def _telegram_menu() -> int:
    store = default_telegram_store(AGENT_PROJECT_ROOT)
    while True:
        status = _telegram_safe_status()
        print("\nManager Telegram")
        print(f"Status: {status['status']}")
        if status.get("workspace"):
            print(f"Workspace: {status['workspace']}")
        if status.get("error"):
            print(f"Credential error: {status['error']}")
        print("\n[1] Set/replace bot token and pair")
        print("[2] Pair/re-pair existing bot")
        print("[3] Enable Telegram")
        print("[4] Disable Telegram")
        print("[5] Change Telegram workspace")
        print("[6] Remove local Telegram credential")
        print("[0] Back")
        choice = input("Select: ").strip()
        try:
            if choice == "1":
                _telegram_set_or_replace()
            elif choice == "2":
                config = store.load()
                if not config:
                    print("[!] Telegram is not configured.")
                    continue
                _run_telegram_pairing(config)
            elif choice == "3":
                store.set_enabled(True)
                print("[OK] Telegram enabled. Restart RemoteAgent if it is already running.")
            elif choice == "4":
                store.set_enabled(False)
                print("[OK] Telegram disabled. Restart RemoteAgent if it is already running.")
            elif choice == "5":
                workspaces = _telegram_workspace_choices()
                if not workspaces and not _acl_off_enabled():
                    print("[!] Configure Security first.")
                    continue
                store.update_workspace(_choose_workspace("Telegram writable workspace", workspaces))
                print("[OK] Telegram workspace updated. Restart RemoteAgent if it is already running.")
            elif choice == "6":
                if input("Type REMOVE to delete this PC/user credential: ").strip() == "REMOVE":
                    store.remove()
                    _clear_telegram_pairing_state()
                    print("[OK] Local Telegram credential and pairing authorization removed.")
            elif choice == "0":
                return 0
        except Exception as exc:
            print(f"[!] Telegram operation failed: {type(exc).__name__}: {exc}")


def _interactive() -> int:
    snapshot = _binding_snapshot()
    print("\nSmartAgent Workspace Manager")
    print(f"Security: {snapshot['security_status']}")
    print("\nWritable workspaces:")
    if snapshot["writable_workspaces"]:
        for value in snapshot["writable_workspaces"]:
            print(f"  - {value}")
    else:
        print("  (none - configure Security first)")
    print("Read-only roots:")
    if snapshot["read_only_roots"]:
        for value in snapshot["read_only_roots"]:
            print(f"  - {value}")
    else:
        print("  (none)")
    print("\n[1] 設定 Writable / Read-only 路徑")
    print("[2] 設定 Local / Remote Workspace + WebGPT")
    print("[3] 設定 / 配對 Telegram（逐步引導）")
    print("[4] 管理已設定的 Telegram")
    print("[0] Exit")
    choice = input("Select: ").strip()
    if choice == "1":
        if _acl_off_enabled():
            return _security_menu()
        return _security_menu() if _is_admin() else _launch_elevated_security_menu()
    if choice == "2":
        return _binding_menu()
    if choice == "3":
        return _telegram_first_time_wizard()
    if choice == "4":
        return _telegram_menu()
    return 0


def main(argv=None) -> int:
    _install_apply_crash_logger()
    _write_apply_log("PROCESS_START argv=" + repr(list(argv or sys.argv[1:])))
    parser = argparse.ArgumentParser()
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--security-menu", action="store_true")
    parser.add_argument("--initialize-security", action="store_true")
    parser.add_argument("--set-binding", choices=("local", "remote"))
    parser.add_argument("--workspace", default="")
    parser.add_argument("--url", default="")
    args = parser.parse_args(argv)
    if args.show:
        print(json.dumps(_binding_snapshot(), ensure_ascii=False, indent=2)); return 0
    if args.security_menu: return _security_menu()
    if args.initialize_security:
        if not _is_admin():
            raise RuntimeError("security_initialize_requires_administrator")
        return _initialize_security_profile()
    if args.set_binding:
        print(json.dumps(_save_binding(args.set_binding, args.workspace, args.url), ensure_ascii=False, indent=2)); return 0
    return _interactive()


if __name__ == "__main__":
    raise SystemExit(main())
