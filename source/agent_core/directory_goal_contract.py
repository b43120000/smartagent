"""Bind a directory Goal target through the existing SCR translation + SPR path preflight.

This is a read-only input contract, not a new intent translator or an ACL.
Unlike Deployment Target Contract, it covers a simple original request such
as: "創立這個資料夾\nE:\SmartAgent_GoalDriven_IT_GH_v1".
"""
from __future__ import annotations

import hashlib
import ntpath
import re
from collections.abc import Mapping

from .semantic_capability_resolution import (
    TRANSLATION_SCHEMA, translate_request_action,
)

SCHEMA = "SMARTAGENT_DIRECTORY_TARGET_BINDING_V1"


def bind_directory_goal_target(
    original_request: str, selected_paths: object,
    *, path_resolution: Mapping | None = None,
    translation: Mapping | None = None,
) -> dict:
    """Select only a unique exact original-request path, no invented parents."""
    text = str(original_request or "")
    semantic = (
        dict(translation) if isinstance(translation, Mapping)
        else translate_request_action(text)
    )
    source = path_resolution if isinstance(path_resolution, Mapping) else {}
    paths = source.get("paths") if isinstance(source.get("paths"), list) else selected_paths
    candidates = [
        str(p) for p in list(paths or [])[:24] if isinstance(p, str) and p.strip()
    ]
    capability = str(semantic.get("selected_capability") or "")
    reason = "NOT_A_DIRECTORY_CREATION_GOAL"
    target = ""
    if semantic.get("schema") != TRANSLATION_SCHEMA or capability != "directory.create":
        reason = "NOT_A_DIRECTORY_CREATION_GOAL"
    elif source.get("status") == "AMBIGUOUS":
        reason = "ORIGINAL_REQUEST_PATH_AMBIGUOUS"
    elif len(candidates) != 1:
        reason = "EXACTLY_ONE_ORIGINAL_REQUEST_PATH_REQUIRED"
    else:
        proposed = candidates[0]
        # Exact literal user input; cannot bind a parent, guessed suffix,
        # different workspace or CWD from model-generated text.
        is_absolute = bool(
            re.fullmatch(r"[A-Za-z]:[\\/][^\r\n<>|?*]+", proposed)
            or proposed.startswith("/") and not proposed.startswith("//")
        )
        is_safe = (
            is_absolute and proposed in text
            and not re.fullmatch(r"[A-Za-z]:[\\/]", proposed)
            and not re.search(r'["<>|?*]', proposed)
            and ".." not in proposed.replace("\\", "/").split("/")
            and not proposed.rstrip("\\/").endswith(":")
            and not proposed.startswith("\\\\")
        )
        if is_safe:
            target = proposed
            reason = "EXACT_USER_DIRECTORY_CREATION_TARGET"
        else:
            reason = "DIRECTORY_TARGET_NOT_EXACT_ORIGINAL_ABSOLUTE_PATH"
    return {
        "schema": SCHEMA,
        "binding_status": "BOUND" if target else "UNBOUND",
        "requested_target": target,
        "capability": capability,
        "canonical_tool": semantic.get("canonical_tool") or "",
        "path_source": "SPR_ORIGINAL_REQUEST_SPAN" if source else "ORIGINAL_REQUEST_EXPLICIT_PATH",
        "request_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "reason_code": reason,
        "authorization_granted": False,
        "operation_executed": False,
    }


__all__ = ["SCHEMA", "bind_directory_goal_target"]
