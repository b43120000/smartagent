"""Local-only source execution attestation; diagnostic, not an authorization grant.

A disk Git SHA is not proof of the historical loaded Python bytecode.
Record both disk module hashes and live code-object fingerprints, with their
distinct provenance, at startup. Never store secrets or request bodies.
"""
from __future__ import annotations

import hashlib
import json
import marshal
import os
import re
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
    # Tri-One 2A: record actual process-loaded functions. An unloaded module
    # stays unloaded; never import it merely to satisfy an attestation.
    "agent_core.tri_one_task_result": (
        "make_task_result", "validate_task_result",
        "make_task_result_from_remote_event", "make_task_result_from_webagent_loop",
    ),
    "agent_core.source_attestation": (
        "capture", "require_task_result_source_attestation", "build_task_result_source_metadata",
    ),
    "agent_core.remote_events": ("RemoteEventStore.emit", "DeliveryManager.deliver"),
    "agent_core.task_continuation": ("TaskContinuationStore.finish", "render_task_reply"),
    "RemoteAgent.telegram_delivery": (
        "TelegramDeliveryAdapter.render", "TelegramDeliveryAdapter.deliver_event",
    ),
    "RemoteAgent.webgpt_delivery": (
        "WebGPTDeliveryAdapter.render", "WebGPTDeliveryAdapter.deliver_event",
    ),
    "WebAgent.controller": ("build_final_render_prompt", "render_final_ack"),
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


def require_writer_loaded_source_attestation(attestation: dict) -> dict:
    """Fail closed unless in-process Writer/dispatch code matches verified disk.

    The caller must invoke this on its own worker process capture immediately
    before Planner starts. A detached-stage import is not a Worker proof.
    """
    module = (attestation.get("modules") or {}).get("agent_core.tools", {})
    comparisons = module.get("live_matches_recompiled_disk") or {}
    required = ("_task_scoped_chunked_write_manager", "_execute_tool_impl")
    if (
        attestation.get("manifest_verified_before_capture") is not True
        or module.get("loaded") is not True
        or module.get("source_path") != "source/agent_core/tools.py"
        or not module.get("disk_sha256_at_capture")
        or not all(comparisons.get(name) is True for name in required)
    ):
        raise RuntimeError("writer_loaded_source_attestation_failed")
    return {"status": "PASS", "module": "agent_core.tools",
            "functions": {name: True for name in required},
            "comparison_method": "cpython_code_object_eq_and_verified_source_filename"}



# 2A coverage is an explicit, fail-closed contract, not a claim about Git HEAD.
# A caller may only rely on this within its *own* executing process after
# all named modules have been loaded through the actual request path.
TRI_ONE_REQUIRED_FUNCTIONS = {
    name: FUNCTIONS[name]
    for name in (
        "agent_core.tri_one_task_result",
        "agent_core.remote_events",
        "agent_core.task_continuation",
        "RemoteAgent.telegram_delivery",
        "RemoteAgent.webgpt_delivery",
        "WebAgent.controller",
    )
}


def require_tri_one_loaded_source_attestation(attestation: dict) -> dict:
    """Validate in-process 2A code identity without manufacturing a commit SHA.

    Missing/lazy modules remain NOT_ATTESTED, including a genuine process that
    did not use a given adapter. This proves neither full Git-tree identity
    nor an accepted terminal receipt; downstream code must preserve those
    independent gates.
    """
    if not isinstance(attestation, dict) or attestation.get(
        "manifest_verified_before_capture"
    ) is not True:
        raise RuntimeError("tri_one_loaded_source_manifest_not_verified")
    observed = attestation.get("modules") or {}
    for name, required in TRI_ONE_REQUIRED_FUNCTIONS.items():
        record = observed.get(name) or {}
        expected_path = "source/" + name.replace(".", "/") + ".py"
        matches = record.get("live_matches_recompiled_disk") or {}
        if (
            record.get("loaded") is not True
            or record.get("source_path") != expected_path
            or not record.get("disk_sha256_at_capture")
            or not all(matches.get(function) is True for function in required)
        ):
            raise RuntimeError("tri_one_loaded_source_not_attested:" + name)
    return {
        "status": "PASS",
        "coverage": list(TRI_ONE_REQUIRED_FUNCTIONS),
        "comparison_method": "cpython_code_object_eq_and_verified_source_filename",
        "source_commit": "NOT_ATTESTED",
    }


TASK_RESULT_SOURCE_FUNCTIONS = {
    "agent_core.source_attestation": (
        "capture", "require_task_result_source_attestation", "build_task_result_source_metadata",
    ),
    "agent_core.remote_events": ("RemoteEventStore.emit",),
    "agent_core.tri_one_task_result": (
        "make_task_result", "validate_task_result", "make_task_result_from_remote_event",
    ),
    "RemoteAgent.telegram_webagent_worker": ("run_task",),
}


def require_task_result_source_attestation(attestation: dict, *, root=None) -> dict:
    """Require live producer functions to match their strict-manifested disk source."""
    if not isinstance(attestation, dict) or attestation.get("manifest_verified_before_capture") is not True:
        raise RuntimeError("task_result_source_manifest_not_verified")
    base = Path(root or Path(__file__).resolve().parents[2]).expanduser().resolve()
    manifest_path = base / "config" / "protocol_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        core_files = manifest.get("core_files", {})
    except Exception as exc:
        raise RuntimeError("task_result_source_protocol_manifest_unreadable") from exc
    if not isinstance(core_files, dict):
        raise RuntimeError("task_result_source_core_files_missing")
    observed = attestation.get("modules") or {}
    summaries = {}
    for module_name, required in TASK_RESULT_SOURCE_FUNCTIONS.items():
        record = observed.get(module_name) or {}
        relative = "source/" + module_name.replace(".", "/") + ".py"
        matches = record.get("live_matches_recompiled_disk") or {}
        live_hashes = record.get("live_code_sha256") or {}
        disk_code_hashes = record.get("recompiled_disk_code_sha256") or {}
        disk_sha = str(record.get("disk_sha256_at_capture") or "").lower()
        if (
            record.get("loaded") is not True
            or record.get("source_path") != relative
            or not re.fullmatch(r"[0-9a-f]{64}", disk_sha)
            or str(core_files.get(relative) or "").lower() != disk_sha
            or not all(matches.get(name) is True for name in required)
            or not all(re.fullmatch(r"[0-9a-f]{64}", str(live_hashes.get(name) or "")) for name in required)
            or not all(re.fullmatch(r"[0-9a-f]{64}", str(disk_code_hashes.get(name) or "")) for name in required)
        ):
            raise RuntimeError("task_result_source_not_attested:" + module_name)
        summaries[module_name] = {
            "source_path": relative,
            "disk_sha256": disk_sha,
            "live_matches_recompiled_disk": {name: True for name in required},
            "functions": {name: True for name in required},
            "live_code_sha256": {name: live_hashes[name] for name in required},
            "recompiled_disk_code_sha256": {name: disk_code_hashes[name] for name in required},
            "comparison_method": str(record.get("comparison_method") or ""),
        }
    return {"status": "PASS", "modules": summaries}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_task_result_source_metadata(attestation: dict, *, root=None) -> dict:
    """Build additive code-identity metadata; never infer source_commit from Git HEAD.

    An unsigned package may carry a build-commit claim, but the TaskResult
    source_commit remains NOT_ATTESTED until a trusted signature or protected
    machine-authorization binding is available.
    """
    from .tri_one_task_result import _empty_source_metadata

    base = Path(root or Path(__file__).resolve().parents[2]).expanduser().resolve()
    metadata = _empty_source_metadata("task_result_source_not_attested")
    try:
        required = require_task_result_source_attestation(attestation, root=base)
        from .protocol_manifest import require_protocol_manifest
        verified = require_protocol_manifest(base)
        if verified.get("hash_verification") != "PASS" or verified.get("hash_mismatches"):
            raise RuntimeError("task_result_source_manifest_not_strict_pass")
        protocol_path = base / "config" / "protocol_manifest.json"
        protocol_sha = _sha256_file(protocol_path)
        protocol_document = json.loads(protocol_path.read_text(encoding="utf-8"))
        attestation_bytes = json.dumps(
            attestation, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")
        metadata.update(
            loaded_code_status="PASS",
            attestation_sha256=hashlib.sha256(attestation_bytes).hexdigest(),
            protocol_manifest_sha256=protocol_sha,
            modules=required["modules"],
            reason=None,
        )
        provenance_path = base / "config" / "source_build_provenance.json"
        update_path = base / "config" / "update_manifest.json"
        if not provenance_path.is_file() or not update_path.is_file():
            metadata["reason"] = "source_build_provenance_unavailable"
            return metadata
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            update_manifest = json.loads(update_path.read_text(encoding="utf-8"))
        except Exception:
            metadata["reason"] = "source_build_provenance_unreadable"
            return metadata
        provenance_sha = _sha256_file(provenance_path)
        update_sha = _sha256_file(update_path)
        files = update_manifest.get("files", {})
        source_commit_claim = str(provenance.get("source_commit") or "").lower()
        source_tree_sha = str(provenance.get("source_tree_sha") or "").lower()
        sequence = update_manifest.get("release_sequence")
        if (
            provenance.get("schema") != "SMARTAGENT_SOURCE_BUILD_PROVENANCE_V1"
            or provenance.get("source_tree_clean_at_build") is not True
            or not re.fullmatch(r"[0-9a-f]{40}", source_commit_claim)
            or not re.fullmatch(r"[0-9a-f]{40}", source_tree_sha)
            or provenance.get("protocol_manifest_sha256") != protocol_sha
            or provenance.get("release_sequence") != sequence
            or provenance.get("release_id") != update_manifest.get("release_id")
            or provenance.get("protocol_family") != protocol_document.get("protocol_family")
            or provenance.get("protocol_version") != protocol_document.get("protocol_version")
            or provenance.get("publisher_signature_status") != "NOT_PROVIDED"
            or update_manifest.get("schema") != "SMARTAGENT_UPDATE_MANIFEST_V1"
            or not isinstance(files, dict)
            or str(files.get("config/source_build_provenance.json") or "").lower() != provenance_sha
            or str(files.get("config/protocol_manifest.json") or "").lower() != protocol_sha
        ):
            metadata["reason"] = "source_build_provenance_not_consistent"
            return metadata
        metadata.update(
            source_commit_claim=source_commit_claim,
            source_commit_claim_status="UNSIGNED_BUILD_PROVENANCE",
            source_provenance_sha256=provenance_sha,
            update_manifest_sha256=update_sha,
            source_tree_sha=source_tree_sha,
            publisher_signature_status="NOT_PROVIDED",
            reason="source_commit_not_attested_unsigned_provenance",
        )
        return metadata
    except Exception as exc:
        metadata["reason"] = "task_result_source_attestation_failed:" + type(exc).__name__
        return metadata


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
        if name in {
            "WebAgent.protocol_loop", "agent_core.strict_observation_guard", "agent_core.tools",
            "agent_core.tri_one_task_result", "agent_core.remote_events",
            "agent_core.source_attestation", "RemoteAgent.telegram_webagent_worker",
            "agent_core.task_continuation", "RemoteAgent.telegram_delivery",
            "RemoteAgent.webgpt_delivery", "WebAgent.controller",
        }:
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


# GAR A0: an explicitly opted-in diagnostic of THIS Python process only.
# No process injection, remote command, evidence generation or terminal authority.
GAR_A0_FUNCTIONS = {
    "WebAgent.controller": ("main", "render_final_ack"),
    "WebAgent.protocol_loop": (
        "WebAgentProtocolLoop.run", "WebAgentProtocolLoop._run_impl",
        "WebAgentProtocolLoop._accept_v8_ack",
        "WebAgentProtocolLoop._gar_rejection",
        "WebAgentProtocolLoop._gar_l4_activate",
        "WebAgentProtocolLoop._gar_snapshot",
    ),
    "agent_core.goal_anchored_recovery": (
        "enabled", "RecoveryEngine.snapshot",
        "RecoveryEngine.planner_rejection",
        "RecoveryEngine.activate_fresh_context",
        "RecoveryEngine.fence_old_planner",
    ),
    "agent_core.task_progress": (
        "_validate_evidence_refs", "validate_model_progress",
        "record_model_progress",
    ),
    "agent_core.tri_one_answer_only": (
        "enabled", "classify_original_request", "accept_terminal_candidate",
    ),
    "agent_core.tri_one_task_result": ("enabled",),
    "agent_core.source_attestation": ("capture_gar_a0",),
}


def gar_a0_enabled(env=None) -> bool:
    """Process-start diagnostic opt-in; default OFF."""
    flags = os.environ if env is None else env
    return str(flags.get("SMARTAGENT_GAR_A0_DIAGNOSTIC", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def capture_gar_a0(*, controller_main, require_fresh_context: bool = False) -> dict:
    """Attest already-loaded functions against strict-Manifest disk bytes.

    A matching disk Git HEAD is not trusted loaded-code provenance. For a
    python -m Controller, __main__ is used ONLY if its file is the actual
    Controller file and its main callable is the caller-bound main object.
    All paths are kept relative; errors become NOT_ATTESTED.
    """
    root = Path(__file__).resolve().parents[2]
    expected_files = {}
    manifest_strict = False
    try:
        from .protocol_manifest import require_protocol_manifest
        result = require_protocol_manifest(root)
        manifest_strict = (
            result.get("hash_verification") == "PASS"
            and not result.get("hash_mismatches")
        )
        if manifest_strict:
            data = json.loads((root / "config" / "protocol_manifest.json").read_text(
                encoding="utf-8"
            ))
            expected_files = data.get("core_files", {})
            manifest_strict = isinstance(expected_files, dict)
    except (OSError, ValueError, RuntimeError, KeyError):
        manifest_strict = False

    required = dict(GAR_A0_FUNCTIONS)
    if require_fresh_context:
        required["WebAgent.fresh_context"] = (
            "ExistingConversationFreshAdapter.prepare",
        )

    entries = {}
    all_matched = manifest_strict and controller_main is not None
    for name, functions in required.items():
        relative = "source/" + name.replace(".", "/") + ".py"
        expected_source = (root / relative).resolve()
        module = sys.modules.get(name)
        if name == "WebAgent.controller":
            executing = sys.modules.get("__main__")
            try:
                if (executing is not None and
                        Path(getattr(executing, "__file__", "")).resolve()
                        == expected_source):
                    module = executing
            except (OSError, TypeError, ValueError):
                pass
        loaded = module is not None
        correct_file = False
        digest = None
        function_checks = {}
        if loaded:
            try:
                current_file = Path(getattr(module, "__file__", "")).resolve()
                correct_file = current_file == expected_source
                if correct_file:
                    digest = hashlib.sha256(current_file.read_bytes()).hexdigest()
            except (OSError, TypeError, ValueError):
                correct_file = False
        disk_verified = bool(
            manifest_strict and correct_file and digest
            and expected_files.get(relative) == digest
        )
        callable_bound = (
            name != "WebAgent.controller"
            or (module is not None and
                getattr(module, "main", None) is controller_main)
        )
        for function in functions:
            comparison = {}
            if disk_verified and callable_bound:
                try:
                    comparison = _compared_code_identity(
                        module, expected_source, function
                    )
                except (OSError, SyntaxError, TypeError, ValueError):
                    pass
            matched = bool(
                comparison.get("code_objects_equal") is True
                and comparison.get("filename_verified") is True
                and comparison.get("first_line_equal") is True
                and comparison.get("qualname_equal") is True
            )
            function_checks[function] = {
                "match": matched,
                "live_code_sha256": comparison.get("live_code_sha256"),
                "disk_code_sha256": comparison.get("disk_code_sha256"),
            }
        module_ok = bool(
            loaded and disk_verified and callable_bound
            and all(f["match"] for f in function_checks.values())
        )
        all_matched = all_matched and module_ok
        entries[name] = {
            "loaded": loaded,
            "source_path": relative,
            "strict_manifest_disk_match": disk_verified,
            "controller_main_bound": bool(callable_bound),
            "disk_sha256": digest,
            "functions": function_checks,
            "match": module_ok,
        }
    return {
        "schema": "SMARTAGENT_GAR_A0_LOADED_CODE_V1",
        "captured_at_epoch": time.time(),
        "manifest_strict": manifest_strict,
        "loaded_code_status": (
            "MATCHES_VERIFIED_DISK" if all_matched else "NOT_ATTESTED"
        ),
        "source_commit_status": "NOT_ATTESTED",
        "modules": entries,
    }
