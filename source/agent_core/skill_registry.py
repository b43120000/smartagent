#!/usr/bin/env python3
"""Registry for controlled SmartAgent skills backed by fixed Python scripts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .python_executor import PythonExecutionRequest


_SKILL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_GLOB_CHARS = frozenset("*?[]")


def normalize_skill_name(name: str) -> str:
    return str(name or "").strip().lower().replace("-", "_")


def _validate_script_path(script: str | Path) -> str:
    value = str(script or "").strip()
    if not value:
        raise ValueError("skill_script_required")
    if any(char in value for char in _GLOB_CHARS):
        raise ValueError("skill_script_glob_forbidden")
    path = Path(value)
    if path.is_absolute():
        raise ValueError("skill_script_must_be_relative")
    if ".." in path.parts:
        raise ValueError("skill_script_parent_traversal_forbidden")
    if path.suffix.casefold() != ".py":
        raise ValueError("skill_script_extension_required")
    return path.as_posix()


@dataclass(frozen=True)
class SkillDescriptor:
    name: str
    script: str | Path
    description: str = ""
    kind: str = "python"

    def __post_init__(self) -> None:
        canonical = normalize_skill_name(self.name)
        if not _SKILL_NAME.fullmatch(canonical):
            raise ValueError("skill_name_invalid")
        kind = str(self.kind or "").strip().lower()
        if kind != "python":
            raise ValueError("skill_kind_unsupported")
        script = _validate_script_path(self.script)
        object.__setattr__(self, "name", canonical)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "script", script)
        object.__setattr__(self, "description", str(self.description or "").strip())


class SkillRegistry:
    """Deterministic registry of approved skill descriptors."""

    def __init__(self, descriptors=()):
        self._by_name: dict[str, SkillDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: SkillDescriptor) -> None:
        if not isinstance(descriptor, SkillDescriptor):
            raise TypeError("descriptor must be SkillDescriptor")
        if descriptor.name in self._by_name:
            raise ValueError(f"skill_already_registered:{descriptor.name}")
        self._by_name[descriptor.name] = descriptor

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def resolve(self, name: str) -> SkillDescriptor:
        canonical = normalize_skill_name(name)
        try:
            return self._by_name[canonical]
        except KeyError as exc:
            raise KeyError(f"unknown_skill:{canonical}") from exc

    def execution_request(
        self,
        name: str,
        *,
        args: tuple[str, ...] = (),
        timeout: int = 30,
        capture_root: str | Path | None = None,
    ) -> PythonExecutionRequest:
        descriptor = self.resolve(name)
        return PythonExecutionRequest(
            script=descriptor.script,
            args=args,
            timeout=timeout,
            capture_root=capture_root,
        )


DEFAULT_SKILL_DESCRIPTORS = (
    SkillDescriptor(
        name="image_compare",
        script="source/agent_core/skills/image_compare.py",
        description="Compare two workspace images and emit deterministic pixel-difference metrics as JSON.",
    ),
)


def default_skill_registry() -> SkillRegistry:
    """Return a fresh registry containing only built-in approved skills."""
    return SkillRegistry(DEFAULT_SKILL_DESCRIPTORS)


__all__ = [
    "DEFAULT_SKILL_DESCRIPTORS",
    "SkillDescriptor",
    "SkillRegistry",
    "default_skill_registry",
    "normalize_skill_name",
]
