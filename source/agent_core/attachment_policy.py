"""Shared attachment and project-bundle limits.

This module deliberately has no browser, prompt, ACK, or agent dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping


_ENV_KEYS = {
    "max_files_per_bundle": "SMARTAGENT_MAX_FILES_PER_BUNDLE",
    "max_bytes_per_bundle": "SMARTAGENT_MAX_BUNDLE_BYTES",
    "max_attachments_per_message": "SMARTAGENT_MAX_ATTACHMENTS_PER_MESSAGE",
    "max_batches": "SMARTAGENT_MAX_PROJECT_SYNC_BATCHES",
}


@dataclass(frozen=True)
class AttachmentPolicy:
    max_files_per_bundle: int = 50
    max_bytes_per_bundle: int = 500_000
    max_attachments_per_message: int = 7
    max_batches: int = 128

    def __post_init__(self) -> None:
        for name in _ENV_KEYS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"invalid_attachment_policy:{name}={value!r}")

    @property
    def max_total_bundles(self) -> int:
        """Independent whole-transaction safety cap, never a UI message cap."""
        return self.max_attachments_per_message * self.max_batches

    @classmethod
    def from_sources(
        cls,
        config: Mapping[str, object] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "AttachmentPolicy":
        values = dict(config or {})
        source = os.environ if environ is None else environ
        for field_name, env_name in _ENV_KEYS.items():
            raw = source.get(env_name)
            if raw is not None and str(raw).strip():
                try:
                    values[field_name] = int(str(raw).strip())
                except ValueError as exc:
                    raise ValueError(
                        f"invalid_attachment_policy_env:{env_name}={raw!r}"
                    ) from exc
        known = set(_ENV_KEYS)
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"unknown_attachment_policy_keys:{','.join(unknown)}")
        return cls(**values)


def resolve_attachment_policy(
    policy: AttachmentPolicy | None = None,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> AttachmentPolicy:
    """Resolve shared policy while preserving legacy explicit size overrides."""
    base = policy or AttachmentPolicy.from_sources()
    if max_files is None and max_bytes is None:
        return base
    return AttachmentPolicy(
        max_files_per_bundle=(
            base.max_files_per_bundle if max_files is None else int(max_files)
        ),
        max_bytes_per_bundle=(
            base.max_bytes_per_bundle if max_bytes is None else int(max_bytes)
        ),
        max_attachments_per_message=base.max_attachments_per_message,
        max_batches=base.max_batches,
    )


__all__ = ["AttachmentPolicy", "resolve_attachment_policy"]
