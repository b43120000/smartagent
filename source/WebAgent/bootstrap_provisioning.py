#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Callable

from agent_core.paths import metadata_root

SCHEMA = "SMARTAGENT_PROVISIONING_STATE_V1"
RETRYABLE = {"BOOTSTRAP_READY", "PROVISIONING_RUNNING", "PROVISIONING_FAILED"}
CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


def _state_path(root: str | Path) -> Path:
    return metadata_root(root) / "provisioning_state.json"


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def bootstrap_provisioning_needed(root: str | Path) -> bool:
    path = _state_path(root)
    state = _load(path)
    if state.get("schema") != SCHEMA:
        raise RuntimeError("bootstrap_provisioning_state_missing")
    status = str(state.get("status", "") or "")
    if status == "COMPLETED":
        return False
    if status not in RETRYABLE:
        raise RuntimeError(f"bootstrap_provisioning_state_invalid:{status}")
    return True


def prepare_bootstrap_request(root: str | Path) -> str:
    root = Path(root).resolve()
    path = _state_path(root)
    state = _load(path)
    if state.get("schema") != SCHEMA:
        raise RuntimeError("bootstrap_provisioning_state_missing")
    status = str(state.get("status", "") or "")
    if status == "COMPLETED":
        return ""
    if status not in RETRYABLE:
        raise RuntimeError(f"bootstrap_provisioning_state_invalid:{status}")
    manifest = Path(str(state.get("manifest", "") or (root / "install_smart_agent" / "POST_BOOTSTRAP_SETUP.md"))).resolve()
    try:
        manifest.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("bootstrap_manifest_outside_application_root") from exc
    if not manifest.is_file():
        raise RuntimeError(f"bootstrap_manifest_missing:{manifest}")
    state.update(
        schema=SCHEMA,
        status="PROVISIONING_RUNNING",
        started_at=time.time(),
        attempt=int(state.get("attempt", 0) or 0) + 1,
        manifest=str(manifest),
        last_error="",
    )
    _save(path, state)
    return (
        "[SMARTAGENT_BOOTSTRAP_PROVISION]\n"
        "This is the one-time SmartAgent first-run provisioning request. "
        "The pre-WebGPT M0 bootstrap and browser launch verification already passed. "
        f"Read {manifest} with the available file tool and follow that contract exactly. "
        "Start with the checklist and execute the remaining M1-M5 installer milestones. "
        "Use only the deterministic installer commands specified there. "
        "Bootstrap tool policy permits only the named manifest plus the fixed checklist and milestone commands. "
        "Do not modify user projects and do not invent substitute installation commands."
    )


def _run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _json_output(value: str) -> dict:
    text = str(value or "").lstrip("\ufeff").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("install_checklist_json_missing")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise RuntimeError("install_checklist_json_invalid")
    return payload


def _engine_command(root: Path, action: str, *extra: str) -> list[str]:
    engine = root / "install_smart_agent" / "install_milestones.ps1"
    if not engine.is_file():
        raise RuntimeError(f"install_milestone_engine_missing:{engine}")
    return [
        "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(engine), "-Action", action, "-ProjectRoot", str(root), *extra,
    ]


def _save_failure(root: Path, status: str, detail: str) -> None:
    path = _state_path(root)
    state = _load(path)
    state.update(
        schema=SCHEMA,
        status=status,
        failed_at=time.time(),
        last_error=str(detail or "")[:4000],
    )
    _save(path, state)


def run_deterministic_provisioning(
    root: str | Path,
    *,
    runner: CommandRunner = _run_command,
    max_steps: int = 12,
) -> dict:
    """Run the fixed installer locally; WebGPT is used only after a real failure."""
    root = Path(root).resolve()
    attempts: dict[str, int] = {}
    transcript: list[str] = []
    for _ in range(max_steps):
        try:
            check = runner(_engine_command(root, "Check", "-Json", "-Quiet"), root)
        except Exception as exc:
            detail = f"checklist_start_failed:{type(exc).__name__}: {exc}"
            _save_failure(root, "PROVISIONING_FAILED", detail)
            repair = (
                "[SMARTAGENT_BOOTSTRAP_REPAIR]\n"
                "The deterministic checklist could not start. Do not inspect or scan the project and "
                "do not call inspect_project_scope. Read only the named installer files and restore "
                "InstallCheckList.bat plus install_smart_agent\\install_milestones.ps1. "
                "Do not upload project snapshots or attachments.\n"
                f"Failure detail:\n{detail}"
            )
            return {"status": "FAILED", "detail": detail, "webgpt_request": repair}
        try:
            checklist = _json_output(check.stdout)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}; stderr={check.stderr[-2000:]}"
            _save_failure(root, "PROVISIONING_FAILED", detail)
            return {"status": "FAILED", "detail": detail, "webgpt_request": ""}
        if checklist.get("overall") == "PASS":
            return {"status": "COMPLETED", "detail": "all_milestones_pass", "checklist": checklist}
        milestone = str(checklist.get("next_milestone") or "").strip().upper()
        if milestone not in {f"M{index}" for index in range(6)}:
            detail = f"invalid_next_milestone:{milestone or '<empty>'}"
            _save_failure(root, "PROVISIONING_FAILED", detail)
            return {"status": "FAILED", "detail": detail, "webgpt_request": ""}
        attempts[milestone] = attempts.get(milestone, 0) + 1
        if attempts[milestone] > 3:
            detail = f"milestone_retry_limit:{milestone}"
            _save_failure(root, "PROVISIONING_FAILED", detail)
            return {"status": "FAILED", "detail": detail, "webgpt_request": ""}
        try:
            install = runner(
                _engine_command(root, "Install", "-Milestone", milestone, "-NonInteractive"),
                root,
            )
        except Exception as exc:
            detail = f"{milestone}_start_failed:{type(exc).__name__}: {exc}"
            _save_failure(root, "PROVISIONING_FAILED", detail)
            repair = (
                "[SMARTAGENT_BOOTSTRAP_REPAIR]\n"
                f"The deterministic installer could not start {milestone}. "
                "Do not inspect or scan the project and do not call inspect_project_scope. "
                f"Repair only {milestone} and its named installer files. "
                "Do not upload project snapshots or attachments.\n"
                f"Failure detail:\n{detail}"
            )
            return {
                "status": "FAILED", "milestone": milestone,
                "detail": detail, "webgpt_request": repair,
            }
        combined = "\n".join(part for part in (install.stdout, install.stderr) if part).strip()
        transcript.append(f"{milestone} exit={install.returncode}\n{combined[-3000:]}")
        if install.returncode == 0:
            continue
        if install.returncode == 3:
            detail = combined or f"{milestone} requires user approval"
            _save_failure(root, "PROVISIONING_FAILED", detail)
            return {
                "status": "NEEDS_USER_ACTION",
                "milestone": milestone,
                "detail": detail,
            }
        detail = combined or f"{milestone} installer failed with exit {install.returncode}"
        _save_failure(root, "PROVISIONING_FAILED", detail)
        repair = (
            "[SMARTAGENT_BOOTSTRAP_REPAIR]\n"
            f"The deterministic installer failed at {milestone}. "
            "Do not inspect or scan the project and do not call inspect_project_scope. "
            f"Read only install_smart_agent\\install_report.txt, repair only {milestone}, then run "
            f"powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File install_smart_agent\\install_milestones.ps1 -Action Install -ProjectRoot . -Milestone {milestone} -NonInteractive followed by "
            "InstallCheckList.bat --json. Do not upload project snapshots or attachments.\n"
            f"Failure detail:\n{detail[-2500:]}"
        )
        return {
            "status": "FAILED",
            "milestone": milestone,
            "detail": detail,
            "webgpt_request": repair,
            "transcript": transcript,
        }
    detail = "provisioning_step_limit"
    _save_failure(root, "PROVISIONING_FAILED", detail)
    return {"status": "FAILED", "detail": detail, "webgpt_request": ""}


def finish_bootstrap_provisioning(root: str | Path, final_content: str) -> dict:
    path = _state_path(root)
    state = _load(path)
    if state.get("status") == "COMPLETED":
        return state
    state.update(
        schema=SCHEMA,
        status="PROVISIONING_FAILED",
        failed_at=time.time(),
        last_error=(str(final_content or "provisioning finished without COMPLETED state")[:4000]),
    )
    _save(path, state)
    return state


def fail_bootstrap_provisioning(root: str | Path, error: BaseException) -> dict:
    path = _state_path(root)
    state = _load(path)
    state.update(
        schema=SCHEMA,
        status="PROVISIONING_FAILED",
        failed_at=time.time(),
        last_error=f"{type(error).__name__}: {error}"[:4000],
    )
    _save(path, state)
    return state
