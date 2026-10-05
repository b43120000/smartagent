#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared workspace and Web conversation binding primitives.

Interactive menus stay in LocalAgent; validation/persistence lives here.
"""
import json
import os
from pathlib import Path

from .paths import install_root, workspace_registry_path
from .web_ui.factory import (
    normalize_web_conversation_url as _normalize_web_conversation_url,
    provider_from_url,
)

AGENT_PROJECT_ROOT = install_root()
DEFAULT_PROFILE_STORE = workspace_registry_path(AGENT_PROJECT_ROOT)


def normalize_web_conversation_url(raw: str) -> str:
    """Validate one supported Web conversation URL without provider-specific rewriting."""
    return _normalize_web_conversation_url(raw)


def normalize_chatgpt_url(raw: str) -> str:
    """Backward-compatible ChatGPT-only validator for legacy callers."""
    value = normalize_web_conversation_url(raw)
    if provider_from_url(value) != "chatgpt":
        raise ValueError("GPT URL must use chatgpt.com")
    return value


def normalize_workspace_path(raw: str) -> str:
    value = str(raw or "").strip().strip('"')
    if not value:
        raise ValueError("Workspace 路徑不可為空")
    path = Path(value).expanduser()
    try:
        path = path.resolve()
    except Exception as exc:
        raise ValueError(f"Workspace 路徑解析失敗: {exc}") from exc
    if not path.exists():
        raise ValueError(f"Workspace 不存在: {path}")
    if not path.is_dir():
        raise ValueError(f"Workspace 不是目錄: {path}")
    return str(path)


def normalize_profiles(raw_profiles: list[dict]) -> list[dict]:
    profiles=[]; seen=set()
    for item in raw_profiles or []:
        if not isinstance(item, dict):
            continue
        try:
            workspace=normalize_workspace_path(item.get("workspace", ""))
            gpt_url=normalize_web_conversation_url(item.get("gpt_url", ""))
        except ValueError:
            continue
        key=(os.path.normcase(workspace), gpt_url)
        if key in seen:
            continue
        seen.add(key)
        profiles.append({"workspace": workspace, "gpt_url": gpt_url})
    return profiles


def load_workspace_links(store: str | Path = DEFAULT_PROFILE_STORE) -> list[dict]:
    store=Path(store)
    if not store.exists():
        return []
    try:
        payload=json.loads(store.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw=payload.get("profiles", []) if isinstance(payload, dict) else []
    return normalize_profiles(raw)


def save_workspace_links(profiles: list[dict], store: str | Path = DEFAULT_PROFILE_STORE) -> None:
    store=Path(store)
    store.parent.mkdir(parents=True, exist_ok=True)
    payload={"version": 1, "profiles": normalize_profiles(profiles)}
    tmp=store.with_name(store.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(store)


def upsert_workspace_link(profiles: list[dict], workspace: str, gpt_url: str) -> tuple[list[dict], dict, bool]:
    workspace=normalize_workspace_path(workspace)
    gpt_url=normalize_web_conversation_url(gpt_url)
    normalized=normalize_profiles(profiles)
    key=(os.path.normcase(workspace), gpt_url)
    for profile in normalized:
        if (os.path.normcase(profile["workspace"]), profile["gpt_url"]) == key:
            return normalized, profile, False
    profile={"workspace": workspace, "gpt_url": gpt_url}
    normalized.append(profile)
    return normalized, profile, True
