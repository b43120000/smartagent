#!/usr/bin/env python3
"""Ordered software restart transaction for the remote runtime."""
from __future__ import annotations

from typing import Any

from .remote_clean_start import remote_clean_start


class RemoteRestartCoordinator:
    """Converge all request and runtime state before returning to listener mode."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def clean_requests(self) -> dict[str, int]:
        result = remote_clean_start(
            self.host.root, reason="remote_restart_discarded"
        )
        self.host.remote_runtime_log.write(
            "RECONNECT",
            component="remote_restart_coordinator",
            stage="REMOTE_REQUEST_RESET",
            **result,
        )
        return result

    def perform(self, execution: Any) -> dict[str, int]:
        """Run the restart barrier in a fixed, observable order."""
        result = self.clean_requests()
        self.host._close_remote_session(reason="SOFTWARE_RUNTIME_RESTART")
        execution.reset()
        # Reconnect means rebuilding the WebGPT page now, not merely waiting
        # for a later task to discover that the old session was closed.
        state = self.host.remote_browser_session.restart()
        lifecycle = self.host.runtime_lifecycle
        if lifecycle is not None:
            lifecycle.emit("WAITING_SIGNAL")
        self.host.remote_runtime_log.write(
            "RECONNECT",
            component="remote_restart_coordinator",
            stage="REMOTE_RESTART_COMPLETED",
            conversation_url=str(state.get("conversation_url", "") or ""),
            cdp_endpoint=str(state.get("cdp_endpoint", "") or ""),
            **result,
        )
        return result
