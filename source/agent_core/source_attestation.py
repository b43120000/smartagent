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


def _recompiled_disk_fingerprint(source: Path, function: str):
    """Fingerprint a named function compiled fresh from verified disk source.

    This is a bounded comparison, not proof that every function in a module
    matches the on-disk source or that the Git HEAD was loaded.
    """
    code = compile(source.read_bytes(), str(source), "exec", dont_inherit=True)
    for part in function.split("."):
        code = next((
            child for child in code.co_consts
            if hasattr(child, "co_code") and child.co_name == part
        ), None)
        if code is None:
            return None
    return hashlib.sha256(marshal.dumps(code)).hexdigest()


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
        if name in {"WebAgent.protocol_loop", "agent_core.strict_observation_guard"}:
            live = entry["live_code_sha256"]
            recompiled = {}
            for function in functions:
                try:
                    recompiled[function] = (
                        _recompiled_disk_fingerprint(source, function) if source else None
                    )
                except (OSError, SyntaxError, ValueError):
                    recompiled[function] = None
            entry["recompiled_disk_code_sha256"] = recompiled
            entry["live_matches_recompiled_disk"] = {
                function: bool(live.get(function) and live[function] == recompiled.get(function))
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
