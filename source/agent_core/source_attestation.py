"""Local-only source execution attestation; diagnostic, not an authorization grant.

A disk Git SHA is not proof of the historical loaded Python bytecode.
Record both disk module hashes and live code-object fingerprints, with their
distinct provenance, at startup. Never store secrets or request bodies.
"""
from __future__ import annotations

import hashlib
import marshal
import os
from pathlib import Path
import sys
import time


FUNCTIONS = {
    # IT19: Writer is safety-critical. Capture the real Worker-imported code,
    # not just an installed source hash or detached worktree import.
    "agent_core.tools": ("_task_scoped_chunked_write_manager", "_execute_tool_impl"),
    "RemoteAgent.remote_worker_launcher": ("RemoteWorkerLauncher._spawn",),
    "RemoteAgent.telegram_webagent_worker": ("run_task",),
    "WebAgent.protocol_loop": ("WebAgentProtocolLoop.run", "WebAgentProtocolLoop._accept_v8_ack"),
    "agent_core.strict_observation_guard": (
        "strict_observation_only_request", "forbidden_strict_actions",
        "strict_observation_action_diagnostic",
    ),
    "agent_core.path_integrity_trace": ("_settings", "begin", "record", "worker_env_from_handoff"),
    "agent_core.web_runtime": (
        "WebLLMScraper._composer_matches_prompt",
        "WebLLMScraper._composer_verification_passes",
        "WebLLMScraper._wait_for_send_ready",
    ),
    "agent_core.composer_operation": ("_page_snapshot", "_write_staged_prompt", "helper_main"),
}


def _loaded_fingerprint(module, name):
    target = module
    for part in name.split("."):
        target = getattr(target, part, None)
        if target is None:
            return None
    code = getattr(target, "__code__", None)
    if code is None:
        return None
    return hashlib.sha256(marshal.dumps(code)).hexdigest()


def _recompiled_disk_code(source: Path, function: str):
    """Compile verified disk source and locate the exact named code object.

    The compiler may embed a different filename than the module import path.
    Raw marshal fingerprints include that metadata: they are diagnostic only.
    """
    code = compile(source.read_bytes(), str(source), "exec", dont_inherit=True)
    for part in function.split("."):
        code = next((
            child for child in code.co_consts
            if hasattr(child, "co_code") and child.co_name == part
        ), None)
        if code is None:
            return None
    return code


def _loaded_code(module, function: str):
    target = module
    for part in function.split("."):
        target = getattr(target, part, None)
        if target is None:
            return None
    return getattr(target, "__code__", None)


def _compared_code_identity(module, source: Path, function: str) -> dict:
    """Require native code-object equality AND an independently verified path.

    CPython code equality compares bytecode and constants (including nested
    code objects) without confusing compiler filename metadata with semantics.
    A disk Git HEAD is never substituted for the actual loaded code object.
    """
    live_code = _loaded_code(module, function)
    disk_code = _recompiled_disk_code(source, function)
    if live_code is None or disk_code is None:
        return {"code_objects_equal": False, "filename_verified": False,
                "first_line_equal": False, "qualname_equal": False,
                "live_code_sha256": None, "disk_code_sha256": None}
    filename_verified = (
        Path(live_code.co_filename).resolve() == source
        and Path(disk_code.co_filename).resolve() == source
    )
    return {
        "code_objects_equal": live_code == disk_code,
        "filename_verified": filename_verified,
        "first_line_equal": live_code.co_firstlineno == disk_code.co_firstlineno,
        "qualname_equal": live_code.co_qualname == disk_code.co_qualname,
        "live_code_sha256": hashlib.sha256(marshal.dumps(live_code)).hexdigest(),
        "disk_code_sha256": hashlib.sha256(marshal.dumps(disk_code)).hexdigest(),
    }


def _disk_git_head(root):
    """An explicitly labelled disk-only claim; never a loaded-code assertion."""
    try:
        git = Path(root) / ".git"
        if git.is_file():
            line = git.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir: "):
                return ""
            git = (git.parent / line[8:]).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head[5:]
            ref_file = git / ref
            if ref_file.is_file():
                head = ref_file.read_text(encoding="utf-8").strip()
            else:
                for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref):
                        head = line.split(" ", 1)[0]
                        break
        return head.lower() if len(head) == 40 and all(c in "0123456789abcdefABCDEF" for c in head) else ""
    except (OSError, ValueError, UnicodeError):
        return ""


def capture(*, role, manifest_verified):
    """Snapshot the modules actually loaded in this process at capture time."""
    root = Path(__file__).resolve().parents[2]
    modules = {}
    for name, functions in FUNCTIONS.items():
        module = sys.modules.get(name)
        if module is None and name == "RemoteAgent.telegram_webagent_worker":
            # python -m runs the Worker as __main__, not under its import name.
            candidate = sys.modules.get("__main__")
            candidate_path = str(getattr(candidate, "__file__", "") or "").replace("\\", "/")
            if candidate_path.endswith("/RemoteAgent/telegram_webagent_worker.py"):
                module = candidate
        if module is None:
            modules[name] = {"loaded": False}
            continue
        entry = {"loaded": True}
        source = None
        path = getattr(module, "__file__", None)
        if path:
            try:
                source = Path(path).resolve()
                source.relative_to(root)
                entry["source_path"] = str(source.relative_to(root)).replace(os.sep, "/")
                entry["disk_sha256_at_capture"] = hashlib.sha256(source.read_bytes()).hexdigest()
            except (OSError, ValueError):
                entry["disk_sha256_at_capture"] = None
        entry["live_code_sha256"] = {
            function: digest for function in functions
            if (digest := _loaded_fingerprint(module, function)) is not None
        }
        if name in {"WebAgent.protocol_loop", "agent_core.strict_observation_guard", "agent_core.tools"}:
            # R012 raw marshal hashes are diagnostics, not semantic equivalence
            # tests. A real code-object or independently verified path mismatch
            # remains fail-closed before Planner or Tool.
            comparisons = {}
            for function in functions:
                try:
                    comparisons[function] = (
                        _compared_code_identity(module, source, function) if source else {}
                    )
                except (OSError, SyntaxError, ValueError, TypeError):
                    comparisons[function] = {}
            entry["code_identity_comparisons"] = comparisons
            entry["comparison_method"] = "cpython_code_object_eq_and_verified_source_filename"
            entry["raw_marshal_hash_is_diagnostic_only"] = True
            entry["recompiled_disk_code_sha256"] = {
                function: comparisons[function].get("disk_code_sha256")
                for function in functions
            }
            entry["live_matches_recompiled_disk"] = {
                function: bool(
                    comparisons[function].get("code_objects_equal") is True
                    and comparisons[function].get("filename_verified") is True
                    and comparisons[function].get("first_line_equal") is True
                    and comparisons[function].get("qualname_equal") is True
                )
                for function in functions
            }
        modules[name] = entry
    return {
        "schema": "SMARTAGENT_SOURCE_ATTESTATION_V1",
        "role": str(role),
        "captured_at_epoch": time.time(),
        "pid": os.getpid(),
        "manifest_verified_before_capture": bool(manifest_verified),
        "disk_git_head_only": _disk_git_head(root),
        "disk_head_is_not_loaded_source_proof": True,
        "modules": modules,
    }
