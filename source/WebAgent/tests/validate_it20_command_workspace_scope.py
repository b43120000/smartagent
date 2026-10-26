"""IT20 bounded short wait within binding, unrelated fixture not granted.

Offline-only: PYTHONPATH=source python source/WebAgent/tests/validate_it20_command_workspace_scope.py
"""
from pathlib import Path
from types import SimpleNamespace
import tempfile

from agent_core.path_security import PathSecurityError
from agent_core.security_context import SecurityContext
from agent_core.tools import _bounded_path_independent_wait, resolve_command_working_directory


def must_reject(f, reason):
    try:
        f()
    except PathSecurityError as exc:
        assert reason in str(exc), exc
    else:
        raise AssertionError("expected rejection: " + reason)


def main():
    with tempfile.TemporaryDirectory(prefix="it20-wait-") as temp:
        root = Path(temp).resolve()
        workspace, fixture = root / "authorized_workspace", root / "unrelated_fixture"
        subdir = workspace / "unrelated_subdir"
        for path in (workspace, fixture, subdir):
            path.mkdir(parents=True)
        context = SecurityContext.for_workspace(workspace)
        agent = SimpleNamespace(_authorized_local_paths=[str(fixture)])
        waits = [
            "python -c \"__import__('time').sleep(3)\"",
            "Start-Sleep -Seconds 1",
            "Start-Sleep -Milliseconds 100",
        ]
        for cmd in waits:
            assert _bounded_path_independent_wait(cmd)
            assert resolve_command_working_directory(None, context, agent, command=cmd) == workspace
            assert resolve_command_working_directory(str(workspace), context, agent, command=cmd) == workspace
        for cmd in (
            "python -c \"__import__('os').system('dir')\"",
            "python -c \"__import__('time').sleep(30)\"",
            "python -c \"__import__('time').sleep(3);print(7)\"",
            "Start-Sleep -Seconds 11",
            "Get-ChildItem",
        ):
            assert not _bounded_path_independent_wait(cmd), cmd
            must_reject(
                lambda cmd=cmd: resolve_command_working_directory(None, context, agent, command=cmd),
                "command_working_directory_outside_request_scope",
            )
        must_reject(
            lambda: resolve_command_working_directory(str(subdir), context, agent, command=waits[0]),
            "command_working_directory_outside_request_scope",
        )
        must_reject(
            lambda: resolve_command_working_directory(str(fixture), context, agent, command=waits[0]),
            "path_outside_authorized_roots",
        )


if __name__ == "__main__":
    main()
    print("IT20_BOUNDED_WAIT_SCOPE_PASS")
