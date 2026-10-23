"""Opt-in hook injected into an isolated SmartAgent NL-IT worker.

The helper never patches a running process. Activates only when this NEW worker
is launched with explicit test env and a task-specific trace nonce. Lazy import
hook avoids importing web_runtime before run_task establishes CDP settings.
"""
import builtins
import os

if (os.environ.get("SMARTAGENT_REMOTE_WORKER") == "1"
    and os.environ.get("SMARTAGENT_NLIT_CAPTURE_ENABLED") == "1"
    and os.environ.get("SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED") == "YES"):
    try:
        from private_capture import patch_available_modules
        _original_import = builtins.__import__
        _installing = False

        def _nlit_import(name, globals=None, locals=None, fromlist=(), level=0):
            global _installing
            result = _original_import(name, globals, locals, fromlist, level)
            if not _installing:
                _installing = True
                try:
                    if patch_available_modules():
                        # Stop intercepting imports once all three existing
                        # classes have been wrapped. Do not alter module code.
                        builtins.__import__ = _original_import
                finally:
                    _installing = False
            return result

        builtins.__import__ = _nlit_import
    except Exception:
        # Never turn capture installation failure into a fake tool failure.
        # The verifier will see no capture files and report NOT_OBSERVED.
        pass
