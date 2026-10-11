#!/usr/bin/env python3
"""Request-scoped authorization context owned by the local runtime."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .path_security import PathSecurityError, authorize_path, is_within
from .security_config import SecurityConfig


def _configured_access_roots() -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Return runtime-owned writable/read-only roots from the shared policy.

    The model cannot add roots.  ``Edit_workspace.bat`` owns the persistent
    policy; the active Local/Remote binding only selects the default workspace
    used for relative paths and command working directories.
    """
    try:
        from .workspace_access import access_snapshot

        snapshot = access_snapshot()
        writes = tuple(
            Path(value).expanduser().resolve()
            for value in (snapshot.get("writable_workspaces") or ())
            if str(value or "").strip()
        )
        reads = tuple(
            Path(value).expanduser().resolve()
            for value in (snapshot.get("read_only_roots") or ())
            if str(value or "").strip()
        )
        return tuple(dict.fromkeys(writes)), tuple(dict.fromkeys(reads))
    except Exception:
        # Software-only mode and partially installed development trees must
        # remain usable.  Security preflight separately fails closed when a
        # restricted executor is required but its profile is invalid.
        return (), ()


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
        return tuple(dict.fromkeys((
            self.config.workspace_root,
            *self.config.write_roots,
            self.config.runtime_write_root,
        )))

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
        workspace_roots = tuple(dict.fromkeys((
            self.config.workspace_root,
            *self.config.write_roots,
        )))
        for root in workspace_roots:
            protected = root / ".agents" / "security"
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
        write_roots: tuple[str | Path, ...] = (),
        read_roots: tuple[str | Path, ...] = (),
        request_id: str = "",
        task_id: str = "",
        interface_name: str = "local",
    ) -> "SecurityContext":
        return cls(
            SecurityConfig.build(
                workspace,
                write_roots=write_roots,
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

        configured_writes, configured_reads = _configured_access_roots()
        request_reads = tuple(getattr(agent, "_authorized_local_paths", ()) or ())
        return cls.for_workspace(
            workspace,
            write_roots=configured_writes,
            read_roots=tuple(dict.fromkeys((*configured_reads, *request_reads))),
            request_id=str(getattr(agent, "current_request_id", "") or ""),
            task_id=str(getattr(agent, "current_task_id", "") or ""),
            interface_name=str(getattr(agent, "interface_name", "local") or "local"),
        )


__all__ = ["SecurityContext"]
