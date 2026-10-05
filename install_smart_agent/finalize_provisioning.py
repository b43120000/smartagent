#!/usr/bin/env python3
from __future__ import annotations
import argparse
import importlib
import json
import sys
import time
from pathlib import Path

REQUIRED_REQUIREMENTS = {
    "playwright==1.62.0",
    "qrcode[pil]>=7.4,<9",
    "google-api-python-client>=2.0,<3",
    "google-auth-httplib2>=0.2,<1",
    "google-auth-oauthlib>=1.2,<2",
}


def write_state(root: Path, payload: dict) -> None:
    path = root / "localdata" / "metadata" / "provisioning_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    source = root / "source"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    checks: dict[str, object] = {
        "source_present": (root / "source" / "smart_agent.py").is_file(),
        "remote_launcher": (root / "launch_remote_agent.bat").is_file(),
        "webdirect_launcher": (root / "launch_webcopilot_chatgpt.bat").is_file(),
        "edit_workspace": (root / "Edit_workspace.bat").is_file(),
        "force_stop_launcher": (root / "force_stop_all_agents.bat").is_file(),
        "installer_launcher": (root / "install_smart_agent.bat").is_file(),
        "acl_status_launcher": (root / "ACLstatus.bat").is_file(),
        "reinstall_launcher": (root / "reinstall_smart_agent.bat").is_file(),
        "checklist_launcher": (root / "InstallCheckList.bat").is_file(),
    }
    requirements = root / "install_smart_agent" / "requirements.txt"
    try:
        raw = requirements.read_text(encoding="utf-8-sig")
        lines = {line.strip() for line in raw.splitlines() if line.strip()}
        checks["requirements_real_lines"] = "\\n" not in raw
        checks["requirements_complete"] = REQUIRED_REQUIREMENTS <= lines
    except Exception:
        checks["requirements_real_lines"] = False
        checks["requirements_complete"] = False
    for module in (
        "playwright", "qrcode", "googleapiclient",
        "google_auth_httplib2", "google_auth_oauthlib",
    ):
        try:
            importlib.import_module(module)
            checks["import_" + module] = True
        except Exception as exc:
            checks["import_" + module] = f"{type(exc).__name__}: {exc}"
    install_state_path = root / "localdata" / "metadata" / "install-state.json"
    try:
        install_state = json.loads(install_state_path.read_text(encoding="utf-8-sig"))
    except Exception:
        install_state = {}
    checks["full_installer_validation_passed"] = install_state.get("validation_passed") is True
    security_root = root / "localdata" / "secure" / "windows_security"
    try:
        acl_mode = json.loads((security_root / "acl_mode.json").read_text(encoding="utf-8-sig"))
        checks["acl_mode_off"] = (
            acl_mode.get("schema") == "SMARTAGENT_ACL_MODE_V1"
            and acl_mode.get("mode") == "off"
            and Path(str(acl_mode.get("install_root", ""))).resolve() == root
        )
    except Exception:
        checks["acl_mode_off"] = False
    try:
        policy = json.loads(
            (security_root / "workspace_access_policy.json").read_text(encoding="utf-8-sig")
        )
        writable = [
            Path(str(value)).expanduser().resolve()
            for value in policy.get("writable_workspaces", [])
            if str(value or "").strip()
        ]
        checks["workspace_access_policy"] = (
            policy.get("schema") == "SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1"
            and bool(writable)
            and all(path.is_dir() for path in writable)
        )
    except Exception:
        checks["workspace_access_policy"] = False
    try:
        instance = json.loads(
            (root / "localdata" / "metadata" / "install_instance.json").read_text(
                encoding="utf-8-sig"
            )
        )
        checks["install_instance_bound"] = (
            instance.get("schema") == "SMARTAGENT_INSTALL_INSTANCE_V1"
            and Path(str(instance.get("install_root", ""))).resolve() == root
        )
    except Exception:
        checks["install_instance_bound"] = False
    passed = all(value is True for value in checks.values())
    print(json.dumps(checks, ensure_ascii=True, indent=2))
    print("SMARTAGENT_FULL_PROVISIONING_PASS" if passed else "SMARTAGENT_FULL_PROVISIONING_FAIL")
    if passed and not args.check_only:
        instance_path = root / "localdata" / "metadata" / "install_instance.json"
        instance = json.loads(instance_path.read_text(encoding="utf-8-sig"))
        instance.update(status="COMPLETED", updated_at=time.time())
        temporary_instance = instance_path.with_suffix(".json.tmp")
        temporary_instance.write_text(
            json.dumps(instance, indent=2, sort_keys=True), encoding="utf-8"
        )
        temporary_instance.replace(instance_path)
        write_state(root, {
            "schema": "SMARTAGENT_PROVISIONING_STATE_V1",
            "status": "COMPLETED",
            "completed_at": time.time(),
            "next_action": "Run Edit_workspace.bat to choose the normal workspace and ChatGPT conversation.",
        })
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
