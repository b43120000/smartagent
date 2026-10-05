#!/usr/bin/env python3
"""Immutable path-access configuration for one request/security context."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .path_security import canonicalize_path, is_within
from .paths import workspace_runtime_root


@dataclass(frozen=True)
class SecurityConfig:
    workspace_root: Path
    runtime_write_root: Path
    read_roots: tuple[Path, ...] = ()
    mode: str = "enforce"
    restricted_executor_required: bool = False

    @classmethod
    def build(
        cls,
        workspace_root: str | os.PathLike[str],
        *,
        runtime_write_root: str | os.PathLike[str] | None = None,
        read_roots: Iterable[str | os.PathLike[str]] = (),
        mode: str = "enforce",
        restricted_executor_required: bool = False,
    ) -> "SecurityConfig":
        workspace = canonicalize_path(workspace_root, require_absolute=True)
        runtime = canonicalize_path(
            runtime_write_root or workspace_runtime_root(workspace),
            require_absolute=True,
        )
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode not in {"enforce", "audit"}:
            raise ValueError(f"invalid_security_mode:{mode}")
        normalized_reads = tuple(
            dict.fromkeys(
                canonicalize_path(root, require_absolute=True) for root in read_roots
            )
        )
        if not is_within(runtime, workspace):
            raise ValueError(f"runtime_write_root_outside_workspace:{runtime}")
        return cls(
            workspace_root=workspace,
            runtime_write_root=runtime,
            read_roots=normalized_reads,
            mode=normalized_mode,
            restricted_executor_required=bool(restricted_executor_required),
        )


__all__ = ["SecurityConfig"]
