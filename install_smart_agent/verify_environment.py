#!/usr/bin/env python3
"""Read-only SmartAgent installation verification."""
from __future__ import annotations

import argparse
import importlib
import json
import platform
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--check-browser", action="store_true")
    parser.add_argument("--check-ollama", action="store_true")
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    checks: dict[str, object] = {
        "project_root": str(root),
        "windows": platform.system() == "Windows",
        "python_version": platform.python_version(),
        "python_64bit": struct.calcsize("P") * 8 == 64,
        "source_present": (root / "smart_agent.py").is_file(),
        "launcher_present": (root / "launch_smart_agent.bat").is_file(),
        "remote_launcher_present": (root / "launch_remote_agent.bat").is_file(),
        "webagent_launcher_present": (root / "launch_webcopilot_chatgpt.bat").is_file(),
        "webagent_present": (root / "WebAgent" / "controller.py").is_file(),
        "agent_core_present": (root / "agent_core" / "webgpt_rate_governor.py").is_file(),
    }
    for module in ("ollama", "playwright", "qrcode", "PIL"):
        try:
            importlib.import_module(module)
            checks[f"import_{module}"] = True
        except Exception as exc:
            checks[f"import_{module}"] = f"{type(exc).__name__}: {exc}"
    if args.check_browser:
        try:
            code = (
                "from playwright.sync_api import sync_playwright; "
                "p=sync_playwright().start(); b=p.chromium.launch(headless=True); "
                "b.close(); p.stop()"
            )
            subprocess.run([sys.executable, "-c", code], check=True, timeout=45)
            checks["chromium_launch"] = True
        except Exception as exc:
            checks["chromium_launch"] = f"{type(exc).__name__}: {exc}"
    if args.check_ollama:
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as response:
                checks["ollama_api"] = response.status == 200
        except Exception as exc:
            checks["ollama_api"] = f"{type(exc).__name__}: {exc}"
    required = (
        "windows", "python_64bit", "source_present", "launcher_present",
        "remote_launcher_present", "webagent_launcher_present",
        "webagent_present", "agent_core_present",
        "import_ollama", "import_playwright", "import_qrcode", "import_PIL",
    )
    passed = all(checks.get(key) is True for key in required)
    if args.check_browser:
        passed = passed and checks.get("chromium_launch") is True
    if args.check_ollama:
        passed = passed and checks.get("ollama_api") is True
    checks["passed"] = passed
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
