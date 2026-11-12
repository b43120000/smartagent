"""Read-only diagnosis: Telegram task workspace vs Turbo OS Broker enrollment.

Prints only paths, revision and booleans. Never reads/decrypts Bot tokens,
never edits ACL/Policy, and never grants Runtime authority.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .paths import telegram_config_path, remote_binding_path
from .turbo_os_broker_client import active_broker_available
from .turbo_sandbox import resolve_turbo_state
from .workspace_access import access_snapshot, resolve_turbo_registered_workspace


def _configured_workspace(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return str(data.get("workspace") or "").strip() if isinstance(data, dict) else ""
    except (OSError, ValueError, TypeError):
        return ""


def snapshot(workspace: str | None = None) -> dict:
    """Operator diagnostics, NOT an OS attestation or permission result."""
    telegram_persisted = _configured_workspace(telegram_config_path())
    remote_persisted = _configured_workspace(remote_binding_path())
    listener_workspace = os.environ.get("SMARTAGENT_TELEGRAM_WORKSPACE", "").strip()
    candidate = str(workspace or listener_workspace or telegram_persisted
                    or remote_persisted).strip()
    access = access_snapshot()
    policy = access["policy"]
    binding = resolve_turbo_registered_workspace(candidate, access)
    state = resolve_turbo_state(candidate or None)
    reason = str(state.get("reason_code") or "")
    return {
        "schema": "SMARTAGENT_TURBO_TASK_BINDING_DIAGNOSTIC_V1",
        "task_workspace_candidate": candidate,
        "actual_running_task_workspace": "NOT_OBSERVED_BY_THIS_CLI",
        "listener_environment_workspace": listener_workspace,
        "telegram_saved_workspace": telegram_persisted,
        "remote_saved_workspace": remote_persisted,
        "policy_revision": int(policy.get("revision") or 0),
        "policy_writable_roots": list(access.get("writable_workspaces") or ()),
        "policy_turbo_roots": list(access.get("turbo_roots") or ()),
        "turbo_requested": bool(policy.get("turbo_requested")),
        "registered_broker_workspace": binding or "",
        "binding_status": "SCOPE_MATCHED" if binding else "BINDING_MISMATCH",
        "binding_reason": reason,
        "broker_enrolled_for_candidate": bool(
            candidate and active_broker_available(candidate)
        ),
        "untrusted_command_authorized": False,
        "policy_or_acl_changed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Turbo Telegram workspace binding")
    parser.add_argument("--workspace", default="", help="exact task workspace to inspect")
    args = parser.parse_args(argv)
    try:
        result = snapshot(args.workspace or None)
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        result = {
            "schema": "SMARTAGENT_TURBO_TASK_BINDING_DIAGNOSTIC_V1",
            "binding_status": "STATE_UNAVAILABLE",
            "reason": type(exc).__name__,
            "policy_or_acl_changed": False,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    # Diagnostic is read-only and deliberately returns 0 so a Telegram CMD
    # batch can finish, even when the *authorization state* is blocked.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
