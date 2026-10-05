#!/usr/bin/env python3
"""Request-scoped authorization context owned by the local runtime."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .path_security import PathSecurityError, authorize_path, is_within
from .security_config import SecurityConfig


def _configured_profile_read_roots() -> tuple[Path, ...]:
    """Return persistent OS-provisioned read-only roots when configured.

    security_profile.json remains runtime-owned.  WebGPT/model text cannot add
    persistent write authority.  The active workspace is the only task-owned
    writable tree, even when the profile authorizes other workspace containers.
    """
    try:
        from .windows_security import (
            default_profile_path,
            load_security_profile,
        )

        profile_path = default_profile_path()
        if not profile_path.is_file():
            return ()
        profile = load_security_profile(profile_path)
        reads = tuple(
            Path(value).expanduser().resolve()
            for value in (profile.get("read_only_roots") or ())
            if str(value or "").strip()
        )
        return tuple(dict.fromkeys(reads))
    except Exception:
        # Software-only mode and partially installed development trees must
        # remain usable.  Security preflight separately fails closed when a
        # restricted executor is required but its profile is invalid.
        return ()


@dataclass(frozen=True)
class SecurityContext:
    config: SecurityConfig
    request_id: str = ""
    task_id: str = ""
    interface_name: str = "local"

    @property
    def workspace_root(self) -> Path:
        return self.config.workspace_root

    @property
    def write_roots(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((self.config.workspace_root, self.config.runtime_write_root)))

    @property
    def read_roots(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((*self.write_roots, *self.config.read_roots)))

    def require_write(
        self,
        path: str | Path,
        *,
        require_absolute: bool = False,
        allow_root: bool = True,
        forbid_glob: bool = False,
    ) -> Path:
        resolved = authorize_path(
            path,
            self.write_roots,
            base=self.workspace_root,
            require_absolute=require_absolute,
            allow_root=allow_root,
            forbid_glob=forbid_glob,
        )
        protected = self.workspace_root / ".agents" / "security"
        if is_within(resolved, protected):
            raise PathSecurityError(f"runtime_owned_security_state:{resolved}")
        return resolved

    def require_read(
        self,
        path: str | Path,
        *,
        require_absolute: bool = False,
        allow_root: bool = True,
        forbid_glob: bool = False,
    ) -> Path:
        return authorize_path(
            path,
            self.read_roots,
            base=self.workspace_root,
            require_absolute=require_absolute,
            allow_root=allow_root,
            forbid_glob=forbid_glob,
        )

    @classmethod
    def for_workspace(
        cls,
        workspace: str | Path,
        *,
        read_roots: tuple[str | Path, ...] = (),
        request_id: str = "",
        task_id: str = "",
        interface_name: str = "local",
    ) -> "SecurityContext":
        return cls(
            SecurityConfig.build(
                workspace,
                read_roots=read_roots,
            ),
            request_id=str(request_id or ""),
            task_id=str(task_id or ""),
            interface_name=str(interface_name or "local"),
        )

    @classmethod
    def from_agent(
        cls, agent, *, fallback_workspace: str | Path | None = None
    ) -> "SecurityContext":
        workspace = getattr(agent, "workspace_root", None) if agent is not None else None
        workspace = workspace or fallback_workspace
        if workspace is None:
            raise PathSecurityError("security_workspace_missing")

        profile_reads = _configured_profile_read_roots()
        request_reads = tuple(getattr(agent, "_authorized_local_paths", ()) or ())
        return cls.for_workspace(
            workspace,
            read_roots=tuple(dict.fromkeys((*profile_reads, *request_reads))),
            request_id=str(getattr(agent, "current_request_id", "") or ""),
            task_id=str(getattr(agent, "current_task_id", "") or ""),
            interface_name=str(getattr(agent, "interface_name", "local") or "local"),
        )


__all__ = ["SecurityContext"]
