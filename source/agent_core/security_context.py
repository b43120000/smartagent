#!/usr/bin/env python3
"""Request-scoped authorization context owned by the local runtime."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .path_security import PathSecurityError, authorize_path, is_within, workspace_path_mode
from .security_config import SecurityConfig


def _configured_access_roots() -> tuple[tuple[Path, ...], tuple[Path, ...], tuple[Path, ...]]:
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
        policy = snapshot.get("policy") or {}
        profile = snapshot.get("profile") or {}
        protected = tuple(
            Path(value).expanduser().resolve()
            for value in (
                *(policy.get("denied_write_roots") or ()),
                *(profile.get("denied_write_roots") or ()),
                *(profile.get("skill_roots") or ()),
                profile.get("executor_code_root"),
                profile.get("controller_state_root"),
                *(snapshot.get("builtin_toolchain_read_roots") or ()),
            )
            if str(value or "").strip()
        )
        return tuple(dict.fromkeys(writes)), tuple(dict.fromkeys(reads)), protected
    except Exception:
        # Software-only mode and partially installed development trees must
        # remain usable.  Security preflight separately fails closed when a
        # restricted executor is required but its profile is invalid.
        return (), (), ()


@dataclass(frozen=True)
class SecurityContext:
    config: SecurityConfig
    request_id: str = ""
    task_id: str = ""
    interface_name: str = "local"
    policy_write_roots: tuple[Path, ...] = ()
    protected_write_denials: tuple[Path, ...] = ()

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
        if any(is_within(resolved, root) for root in self.protected_write_denials):
            raise PathSecurityError("protected_workspace_path_read_only", str(resolved))
        if workspace_path_mode(
            resolved, self.policy_write_roots, self.config.read_roots
        ) == "read_only":
            raise PathSecurityError("workspace_path_read_only", str(resolved))
        workspace_roots = tuple(dict.fromkeys((
            self.config.workspace_root,
            *self.config.write_roots,
        )))
        for root in workspace_roots:
            protected = root / ".agents" / "security"
            if is_within(resolved, protected):
                raise PathSecurityError(f"runtime_owned_security_state:{resolved}")
        # Even a broad editable workspace cannot make SmartAgent's bundled
        # interpreter/toolchain files mutable through the Agent API.
        from .workspace_access import builtin_toolchain_read_roots
        if any(is_within(resolved, Path(root))
               for root in builtin_toolchain_read_roots()):
            raise PathSecurityError("builtin_toolchain_read_only")
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
        protected_write_denials: tuple[Path, ...] = (),
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
            policy_write_roots=tuple(Path(root).resolve() for root in write_roots),
            protected_write_denials=protected_write_denials,
        )

    @classmethod
    def from_agent(
        cls, agent, *, fallback_workspace: str | Path | None = None
    ) -> "SecurityContext":
        workspace = getattr(agent, "workspace_root", None) if agent is not None else None
        workspace = workspace or fallback_workspace
        if workspace is None:
            raise PathSecurityError("security_workspace_missing")

        configured_writes, configured_reads, *denials = _configured_access_roots()
        protected = denials[0] if denials else ()
        # An untrusted task prompt or Planner path hint NEVER grants read
        # permission. The only data-read authority comes from Edit Workspace.
        # Runtime-owned .venv joins Read Scope, never generic Write Scope.
        return cls.for_workspace(
            workspace,
            write_roots=configured_writes,
            read_roots=configured_reads,
            protected_write_denials=protected,
            request_id=str(getattr(agent, "current_request_id", "") or ""),
            task_id=str(getattr(agent, "current_task_id", "") or ""),
            interface_name=str(getattr(agent, "interface_name", "local") or "local"),
        )


__all__ = ["SecurityContext"]
