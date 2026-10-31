"""Independent GAR WebGPT2 target shared across SmartAgent interfaces.

A stored target does not enable GAR. The explicit process-level env override
takes precedence, including an explicit empty string to disable the target.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import MutableMapping

from .conversation_identity import conversation_id, same_conversation
from .paths import metadata_root
from .workspace import normalize_chatgpt_url

SCHEMA = "SMARTAGENT_SHARED_GAR_WEBGPT2_V1"
ENV_KEY = "SMARTAGENT_GAR_FRESH_CONTEXT_URL"


def settings_path() -> Path:
    return metadata_root() / "gar_webgpt2.json"


def validate_target(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    target = normalize_chatgpt_url(raw)
    if not conversation_id(target):
        raise ValueError("gar_webgpt2_conversation_id_required")
    return target


def load_target(*, path: Path | None = None, legacy_path: Path | None = None) -> str:
    target_path = Path(path) if path is not None else settings_path()
    if target_path.is_file():
        try:
            saved = json.loads(target_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("gar_webgpt2_setting_unreadable") from exc
        if not isinstance(saved, dict) or saved.get("schema") != SCHEMA:
            raise ValueError("gar_webgpt2_setting_schema_invalid")
        if not isinstance(saved.get("url"), str):
            raise ValueError("gar_webgpt2_setting_url_invalid")
        return validate_target(saved["url"])

    # Backward compatibility for the prior Main's Local-only saved field.
    from .startup_preferences import load_startup_preferences
    legacy = (load_startup_preferences(legacy_path) if legacy_path is not None
              else load_startup_preferences())
    return validate_target(legacy.get("gar_webgpt2_url", ""))


def save_target(url: str, *, path: Path | None = None) -> str:
    target = validate_target(url)
    dest = Path(path) if path is not None else settings_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(dest.name + ".tmp")
    temp.write_text(json.dumps({"schema": SCHEMA, "url": target},
                               ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    temp.replace(dest)
    return target


def apply_target(primary_url: str, *, environ: MutableMapping[str, str] | None = None,
                 path: Path | None = None, legacy_path: Path | None = None) -> str:
    values = os.environ if environ is None else environ
    target = (validate_target(values[ENV_KEY]) if ENV_KEY in values else
              load_target(path=path, legacy_path=legacy_path))
    if target and same_conversation(primary_url, target):
        raise ValueError("gar_webgpt2_must_differ_from_active_webgpt1")
    if target and ENV_KEY not in values:
        values[ENV_KEY] = target
    return target


__all__ = ["SCHEMA", "ENV_KEY", "settings_path", "validate_target",
           "load_target", "save_target", "apply_target"]
