#!/usr/bin/env python3
"""Read-only standalone-boundary verification for the SmartAgent release tree."""
from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
GENERATED = {
    "install_smart_agent/environment_report.txt",
    "install_smart_agent/install_report.txt",
    "install_smart_agent/install-state.json",
}
REQUIRED = (
    "smart_agent.py",
    "menu_ui.py",
    "agent_core/host_supervisor.py",
    "agent_core/tools.py",
    "agent_core/web_runtime.py",
    "agent_core/webgpt_rate_governor.py",
    "RemoteAgent/remote_runtime.py",
    "RemoteAgent/telegram_webagent_worker.py",
    "RemoteAgent/local_telegram_sender.py",
    "RemoteAgent/local_test_delivery.py",
    "agent_core/json_state_io.py",
    "WebAgent/controller.py",
    "WebAgent/browser_client.py",
    "WebAgent/protocol_loop.py",
    "WebAgent/runtime_log.py",
    "WebAgent/tests/validate_startup_input_queue.py",
    "install_smart_agent.bat",
    "install_smart_agent/install.ps1",
    "install_smart_agent/check_environment.ps1",
    "launch_smart_agent.bat",
    "launch_remote_agent.bat",
    "send_remoteagent_test.bat",
    "launch_webcopilot_chatgpt.bat",
)


def inside_release(path: str | Path) -> bool:
    candidate = Path(path).resolve()
    try:
        candidate.relative_to(ROOT)
        return True
    except ValueError:
        return False


def main() -> int:
    checks: dict[str, object] = {}
    missing = [item for item in REQUIRED if not (ROOT / item).is_file()]
    checks["required_files"] = not missing
    checks["missing_files"] = missing
    checks["personal_startup_config_absent"] = not (
        ROOT / "config" / "startup_preferences.json"
    ).exists()

    syntax_errors = []
    for source in ROOT.rglob("*.py"):
        if any(part in {".venv", "__pycache__"} for part in source.parts):
            continue
        try:
            ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
        except Exception as exc:
            syntax_errors.append(f"{source.relative_to(ROOT)}: {type(exc).__name__}: {exc}")
    checks["python_syntax"] = not syntax_errors
    checks["syntax_errors"] = syntax_errors

    parent_literal = str(ROOT.parent).lower()
    hardcoded = []
    for pattern in ("*.py", "*.ps1", "*.bat", "*.json", "*.md", "*.txt"):
        for source in ROOT.rglob(pattern):
            if any(part in {".venv", ".agents", "__pycache__"} for part in source.parts):
                continue
            if source.relative_to(ROOT).as_posix() in GENERATED:
                continue
            try:
                text = source.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                continue
            if parent_literal and parent_literal in text:
                hardcoded.append(str(source.relative_to(ROOT)))
    checks["no_outer_checkout_literal"] = not hardcoded
    checks["outer_checkout_references"] = sorted(set(hardcoded))

    sys.path.insert(0, str(ROOT))
    origins = {}
    for name in (
        "agent_core.workspace",
        "agent_core.webgpt_rate_governor",
        "RemoteAgent.remote_protocol",
        "WebAgent.controller",
    ):
        module = importlib.import_module(name)
        origin = Path(module.__file__).resolve()
        origins[name] = str(origin)
        checks[f"import_{name}_inside_release"] = inside_release(origin)
    checks["module_origins"] = origins

    workspace = importlib.import_module("agent_core.workspace")
    checks["agent_project_root_is_release"] = Path(workspace.AGENT_PROJECT_ROOT).resolve() == ROOT
    checks["passed"] = all(
        value is True
        for key, value in checks.items()
        if key != "passed" and isinstance(value, bool)
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if checks["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
