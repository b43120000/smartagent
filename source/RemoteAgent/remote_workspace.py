#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_core.conversation_registry import ConversationRegistry
from agent_core.workspace import normalize_chatgpt_url, normalize_workspace_path


def _registry() -> ConversationRegistry:
    reg = ConversationRegistry()
    reg.load()
    return reg


def cmd_list(_args) -> int:
    from agent_core.remote_binding import load
    try:
        print(json.dumps({'primary_binding': load(ROOT)}, ensure_ascii=False, indent=2))
    except FileNotFoundError:
        pass
    rows = _registry().list_remote_conversations(enabled_only=False)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_set(args) -> int:
    workspace = normalize_workspace_path(args.workspace)
    url = normalize_chatgpt_url(args.url)
    reg = _registry()
    from agent_core.remote_binding import DEFAULT_SKILL_PATH, load, save
    try:
        current_skill_path = load(ROOT).get("skill_path", str(DEFAULT_SKILL_PATH))
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        current_skill_path = str(DEFAULT_SKILL_PATH)
    save({
        "workspace": str(workspace), "gpt_url": url,
        "skill_path": args.skill_path or current_skill_path,
    }, ROOT)
    reg.upsert_binding(workspace, url)
    record = reg.configure_remote_conversation(
        workspace,
        url,
        enabled=bool(args.enabled),
        poll_profile=args.poll_profile,
        transport="WEBGPT",
    )
    # This file is the explicit execution authority selected by the operator;
    # task routing must prefer it over historical registry entries.
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def cmd_disable(args) -> int:
    reg = _registry()
    record = reg.find_by_url(normalize_chatgpt_url(args.url))
    if not record:
        raise SystemExit("conversation not registered")
    updated = reg.configure_remote_conversation(
        record["workspace"], record["gpt_url"], enabled=False,
        poll_profile=str(record.get("poll_profile") or "normal"), transport="WEBGPT",
    )
    print(json.dumps(updated, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RemoteAgent v1 conversation registry editor")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("set")
    p.add_argument("--workspace", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--poll-profile", default="normal", choices=("active", "normal", "inactive"))
    p.add_argument("--skill-path", default="")
    p.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    p.set_defaults(func=cmd_set)
    p = sub.add_parser("disable")
    p.add_argument("--url", required=True)
    p.set_defaults(func=cmd_disable)
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
