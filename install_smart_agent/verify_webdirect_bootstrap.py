#!/usr/bin/env python3
from __future__ import annotations
import argparse
import importlib
import json
import platform
import struct
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    source = root / "source"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    checks: dict[str, object] = {
        "windows": platform.system() == "Windows",
        "python_311_plus": sys.version_info >= (3, 11),
        "python_64bit": struct.calcsize("P") * 8 == 64,
        "protocol_manifest": (root / "config" / "protocol_manifest.json").is_file(),
        "webdirect_launcher": (root / "launch_webcopilot_chatgpt.bat").is_file(),
    }
    for module in (
        "playwright",
        "agent_core.protocol_v8",
        "agent_core.smartagent_protocol",
        "agent_core.path_cli",
        "WebAgent.browser_bridge",
        "WebAgent.browser_client",
        "WebAgent.protocol_loop",
        "WebAgent.controller",
    ):
        try:
            importlib.import_module(module)
            checks["import_" + module.replace(".", "_")] = True
        except Exception as exc:
            checks["import_" + module.replace(".", "_")] = f"{type(exc).__name__}: {exc}"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            executable = Path(pw.chromium.executable_path)
            checks["chromium_installed"] = executable.is_file()
            checks["chromium_executable"] = str(executable)
            if executable.is_file():
                browser = pw.chromium.launch(headless=True)
                browser.close()
                checks["chromium_launch"] = True
            else:
                checks["chromium_launch"] = False
    except Exception as exc:
        checks["chromium_installed"] = False
        checks["chromium_launch"] = False
        checks["chromium_error"] = f"{type(exc).__name__}: {exc}"
    passed = all(value is True or key == "chromium_executable" for key, value in checks.items())
    print(json.dumps(checks, ensure_ascii=True, indent=2))
    print("WEBDIRECT_BOOTSTRAP_PASS" if passed else "WEBDIRECT_BOOTSTRAP_FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
