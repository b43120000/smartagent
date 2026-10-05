"""Validated, versioned selector profiles used by provider adapters.

This module is deliberately data-only.  A planner may propose selector values,
but only the local calibration controller can validate and publish them.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid
from typing import Mapping

from ..paths import web_ui_profile_root
from .provider_registry import profile_store_providers


PROFILE_SCHEMA = "SMARTAGENT_WEB_UI_PROFILE_V1"
SUPPORTED_PROVIDERS = frozenset(profile_store_providers())
SCALAR_FIELDS = (
    "composer_selector", "send_selector", "user_turn_selector",
    "user_content_selector", "assistant_turn_selector", "final_content_selector",
)
LIST_FIELDS = ("stop_selectors", "busy_selectors")
ALL_FIELDS = SCALAR_FIELDS + LIST_FIELDS
_FORBIDDEN_SELECTOR_TOKENS = (
    "javascript:", "xpath=", "text=", ":has-text(", ":text(", "document.",
    "window.", "=>", "<script", "\x00",
)


class ProfileValidationError(ValueError):
    pass


def _root(root: str | Path | None = None) -> Path:
    return Path(root).expanduser().resolve() if root else web_ui_profile_root()


def _validate_selector(value: object, *, field: str) -> str:
    selector = str(value or "").strip()
    if not selector:
        raise ProfileValidationError(f"empty_selector:{field}")
    if len(selector) > 2000:
        raise ProfileValidationError(f"selector_too_long:{field}")
    lowered = selector.casefold()
    if any(token in lowered for token in _FORBIDDEN_SELECTOR_TOKENS):
        raise ProfileValidationError(f"unsafe_selector:{field}")
    return selector


def validate_selector_payload(provider: str, selectors: Mapping[str, object]) -> dict:
    provider_name = str(provider or "").strip().lower()
    if provider_name not in SUPPORTED_PROVIDERS:
        raise ProfileValidationError(f"unsupported_provider:{provider_name or '(empty)'}")
    if not isinstance(selectors, Mapping):
        raise ProfileValidationError("selectors_not_object")
    unknown = sorted(set(selectors) - set(ALL_FIELDS))
    missing = sorted(set(ALL_FIELDS) - set(selectors))
    if unknown:
        raise ProfileValidationError("unknown_selector_fields:" + ",".join(unknown))
    if missing:
        raise ProfileValidationError("missing_selector_fields:" + ",".join(missing))
    clean: dict[str, object] = {}
    for field in SCALAR_FIELDS:
        clean[field] = _validate_selector(selectors[field], field=field)
    for field in LIST_FIELDS:
        raw = selectors[field]
        if not isinstance(raw, (list, tuple)) or not raw or len(raw) > 24:
            raise ProfileValidationError(f"invalid_selector_list:{field}")
        clean[field] = tuple(
            _validate_selector(value, field=f"{field}[{index}]")
            for index, value in enumerate(raw)
        )
    return clean


def build_profile_document(
    provider: str,
    selectors: Mapping[str, object],
    *,
    source_url: str,
    planner_url: str,
    validation: Mapping[str, object],
) -> dict:
    clean = validate_selector_payload(provider, selectors)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return {
        "schema": PROFILE_SCHEMA,
        "provider": provider,
        "name": f"{provider}_calibrated_{stamp}_{uuid.uuid4().hex[:8]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_url": str(source_url or "")[:2000],
        "planner_url": str(planner_url or "")[:2000],
        "selectors": {
            **{field: clean[field] for field in SCALAR_FIELDS},
            **{field: list(clean[field]) for field in LIST_FIELDS},
        },
        "validation": dict(validation),
    }


def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def publish_profile(document: Mapping[str, object], *, root: str | Path | None = None) -> Path:
    if document.get("schema") != PROFILE_SCHEMA:
        raise ProfileValidationError("profile_schema_mismatch")
    provider = str(document.get("provider") or "").strip().lower()
    validate_selector_payload(provider, document.get("selectors", {}))
    validation = document.get("validation")
    if not isinstance(validation, Mapping) or validation.get("passed") is not True:
        raise ProfileValidationError("profile_not_validated")
    base = _root(root) / provider
    history = base / "history" / f"{document.get('name')}.json"
    active = base / "active.json"
    _atomic_write(history, document)
    _atomic_write(active, document)
    return active


def rollback_profile(provider: str, *, root: str | Path | None = None) -> Path:
    provider_name = str(provider or "").strip().lower()
    if provider_name not in SUPPORTED_PROVIDERS:
        raise ProfileValidationError(f"unsupported_provider:{provider_name or '(empty)'}")
    base = _root(root) / provider_name
    active_path = base / "active.json"
    current = load_active_document(provider_name, root=root)
    current_name = str((current or {}).get("name") or "")
    candidates = sorted((base / "history").glob("*.json"), reverse=True)
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            if str(payload.get("name") or "") == current_name:
                continue
            if payload.get("schema") != PROFILE_SCHEMA or payload.get("provider") != provider_name:
                continue
            validate_selector_payload(provider_name, payload.get("selectors", {}))
            if not isinstance(payload.get("validation"), Mapping) or payload["validation"].get("passed") is not True:
                continue
            _atomic_write(active_path, payload)
            return active_path
        except (OSError, ValueError, TypeError, ProfileValidationError):
            continue
    raise ProfileValidationError(f"no_previous_valid_profile:{provider_name}")


def load_active_document(provider: str, *, root: str | Path | None = None) -> dict | None:
    provider_name = str(provider or "").strip().lower()
    if provider_name not in SUPPORTED_PROVIDERS:
        return None
    path = _root(root) / provider_name / "active.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != PROFILE_SCHEMA or payload.get("provider") != provider_name:
            return None
        payload["selectors"] = validate_selector_payload(provider_name, payload.get("selectors", {}))
        validation = payload.get("validation")
        if not isinstance(validation, Mapping) or validation.get("passed") is not True:
            return None
        return payload
    except (OSError, ValueError, TypeError, ProfileValidationError):
        return None


def profile_values(provider: str, *, name: str = "", root: str | Path | None = None) -> dict | None:
    payload = load_active_document(provider, root=root)
    if payload is None:
        return None
    if name and str(payload.get("name") or "") != str(name):
        return None
    selectors = dict(payload["selectors"])
    selectors["name"] = str(payload["name"])
    return selectors


__all__ = [
    "ALL_FIELDS", "LIST_FIELDS", "PROFILE_SCHEMA", "ProfileValidationError",
    "SCALAR_FIELDS", "SUPPORTED_PROVIDERS", "build_profile_document",
    "load_active_document", "profile_values", "publish_profile",
    "rollback_profile", "validate_selector_payload",
]
