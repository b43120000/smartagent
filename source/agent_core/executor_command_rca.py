"""Bounded, redacted Executor run_command trace for local RCA export.

This is diagnostic data, never execution authority. Exact commands remain
inside the trusted Runtime during dispatch; the persisted RCA intentionally
masks common secrets and limits output size. Review ZIP before sharing.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_EXECUTOR_COMMAND_RCA_V1"
MAX_COMMAND = 8192
MAX_STREAM = 4096

_SECRET_RULES = (
    (r"(?is)-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----.*?-----END (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----",
     "[REDACTED_PRIVATE_KEY]"),
    (r"(?i)\b(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|sk-)[A-Za-z0-9_\-]{14,}",
     "[REDACTED_CREDENTIAL]"),
    (r"(?i)\b(?:[0-9]{6,12}):[A-Za-z0-9_-]{20,}\b",
     "[REDACTED_BOT_TOKEN]"),
    (r"(?i)(https?://)[^\s/'\"@]+:[^\s/'\"@]+@", r"\1[REDACTED_USERINFO]@"),
    (r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|password|secret|client[_-]?secret|signature|sig|auth|code)=)[^&\s'\"<>]+",
     r"\1[REDACTED]"),
    (r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=\-]{8,}",
     r"\1[REDACTED]"),
    (r"(?i)\b((?:api[_-]?key|access[_-]?token|auth(?:orization)?|client[_-]?secret|password|passwd|secret|session[_-]?key)\s*[:=]\s*)"
     r"(?:'[^']*'|\"[^\"]*\"|[^\s;,&}]+)", r"\1[REDACTED]"),
    (r"(?i)(-(?:Password|Credential|ApiKey|AccessToken|Token|Authorization|ClientSecret|EncodedCommand|AsPlainText)\s+)"
     r"(?:'[^']*'|\"[^\"]*\"|[^\s;]+)", r"\1[REDACTED]"),
)


def redact_for_rca(value: object, *, limit: int = MAX_COMMAND) -> dict:
    """Keep executable grammar and failure text, not common credentials."""
    raw = str(value or "")
    safe = raw
    removed = 0
    for expression, replacement in _SECRET_RULES:
        safe, count = re.subn(expression, replacement, safe)
        removed += count
    # Truncate after redaction, preventing partial disclosure at the boundary.
    clipped = len(safe) > limit
    if clipped:
        safe = safe[:limit] + "\n[TRUNCATED_RCA_TEXT]"
    return {
        "text": safe,
        "original_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "truncated": clipped,
        "redaction_count": removed,
    }


def capture_command_rca(original: object, dispatched: object = None) -> dict:
    original = original if isinstance(original, Mapping) else {}
    command = str(original.get("command") or "")
    submitted = redact_for_rca(command)
    dispatch_text = (
        str(dispatched.get("command") or "")
        if isinstance(dispatched, Mapping) else None
    )
    result = {
        "schema": SCHEMA,
        "command_capture_status": "CAPTURED" if command else "EMPTY_COMMAND",
        "original_command_redacted": submitted["text"],
        "original_command_sha256": submitted["original_sha256"],
        "command_truncated": submitted["truncated"],
        "command_redaction_count": submitted["redaction_count"],
        "operation": str(original.get("operation") or "")[:60],
        "shell_mode": str(original.get("shell_mode") or "POWERSHELL")[:20],
        "argv_count": (len(original.get("argv")) if isinstance(original.get("argv"),list) else 0),
        "argv_integrity_matched": (
            original.get("argv") == dispatched.get("argv")
            if isinstance(dispatched, Mapping) else None),
        "argv_redacted": [redact_for_rca(item,limit=512)["text"]
                          for item in (original.get("argv") or [])[:64]
                          if isinstance(item,str)] if isinstance(original.get("argv"),list) else [],
        "raw_command_included": False,
        "sanitization_best_effort": True,
        "dispatch_known": dispatch_text is not None,
        "dispatch_matches_original": (
            dispatch_text == command
            and original.get("argv") == dispatched.get("argv")
            and (original.get("shell_mode") or "POWERSHELL") == (dispatched.get("shell_mode") or "POWERSHELL")
            if dispatch_text is not None else None
        ),
    }
    if dispatch_text is not None:
        dispatch = redact_for_rca(dispatch_text)
        result["dispatched_command_redacted"] = dispatch["text"]
        result["dispatched_command_sha256"] = dispatch["original_sha256"]
        result["dispatch_truncated"] = dispatch["truncated"]
        result["dispatch_redaction_count"] = dispatch["redaction_count"]
    return result


def _section(source: str, name: str) -> str:
    # The command result is a trusted Runtime wrapper, but command stdout is
    # untrusted. These are diagnostic excerpts, not a parser of OS authority.
    start = re.search(r"(?m)^" + re.escape(name) + r":\n", source)
    if not start:
        return ""
    rest = source[start.end():]
    next_header = re.search(
        r"(?m)^(?:stdout:|stderr:|error:|VERIFICATION_STATUS:|"
        r"EXPECTATION_STATUS:|TERMINAL_ELIGIBLE:|"
        r"\[POST_RUN_VERIFICATION\]|\[OPERATION_RESULT\]|"
        r"\[RUNTIME_TOOL_RESULT\]|\[TURBO_OS_SANDBOX_EVIDENCE\])",
        rest,
    )
    return (rest[:next_header.start()] if next_header else rest).rstrip("\r\n")


def _inline_error(source: str) -> str:
    match = re.search(r"(?m)^error:\s*(.+)$", source)
    return match.group(1).strip() if match else ""


def _failure_category(text: str) -> str:
    rules = (
        ("DNS", r"(?i)NameResolutionFailure|Name or service not known|"
                r"No such host|Could not resolve host|DNS_ERROR|"
                r"getaddrinfo|host not found"),
        ("PROXY", r"(?i)proxy authentication|proxy error|"
                  r"proxyconnect|407 Proxy|proxy server"),
        ("TLS", r"(?i)SSL|TLS|certificate verify|trust relationship|"
                r"SecureChannelFailure|CERTIFICATE_VERIFY_FAILED"),
        ("HTTP", r"(?i)HTTP(?:/[\d.]+)?\s*[45]\d\d|"
                 r"HTTPError|remote server returned an error"),
        ("NETWORK_PERMISSION", r"(?i)network access denied|"
                               r"PermissionError|UnauthorizedAccessException|"
                               r"Access is denied|WinError 10013"),
        ("CONNECTION", r"(?i)connection refused|connection reset|"
                       r"connect timed out|timed out|Unable to connect|"
                       r"network is unreachable"),
    )
    for category, pattern in rules:
        if re.search(pattern, text):
            return category
    return "UNCLASSIFIED"


def capture_command_result_rca(raw_result: object, receipt: object) -> dict:
    """Extract Runtime tool output before it disappears from the Blackboard.

    No host command is run, no attempt is replayed, and output is never proof
    of AppContainer identity. OS attestation is copied from trusted data only.
    """
    receipt = receipt if isinstance(receipt, Mapping) else {}
    data = receipt.get("data")
    data = data if isinstance(data, Mapping) else {}
    raw = str(raw_result or "")
    format_ok = raw.startswith("[COMMAND_RESULT]")
    block = raw.split("\n[RUNTIME_TOOL_RESULT]", 1)[0] if format_ok else ""
    stdout = redact_for_rca(_section(block, "stdout"), limit=MAX_STREAM)
    stderr = redact_for_rca(_section(block, "stderr"), limit=MAX_STREAM)
    error = redact_for_rca(_inline_error(block), limit=1024)
    evidence = data.get("sandbox_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    os_fields = (
        "sandbox_backend", "sandbox_identity_verified",
        "kernel_outside_read_denied", "kernel_outside_write_denied",
        "child_process_policy",
        "os_execution_result", "policy_revision",
        "turbo_effective_for_action", "executed",
        "internet_client_token_verified", "internet_client_capability_sid",
        "native_toolchain_status", "native_trusted_tools",
        "native_error_class", "native_failure_stage",
        "native_failure_substage", "native_exception_type",
        "native_exception_hresult", "native_failure_stage_source",
        "native_process_pid", "native_running_sample_count",
        "native_command_trace", "native_cwd_probes",
    )
    return {
        "schema": SCHEMA,
        "result_capture_status": (
            "COMMAND_RESULT_EXCERPT" if format_ok
            else "STRUCTURED_RECEIPT_ONLY"
        ),
        "stdout_excerpt_redacted": stdout["text"],
        "stderr_excerpt_redacted": stderr["text"],
        "error_excerpt_redacted": error["text"],
        "stdout_excerpt_truncated": stdout["truncated"],
        "stderr_excerpt_truncated": stderr["truncated"],
        "error_excerpt_truncated": error["truncated"],
        "output_redaction_count": (
            stdout["redaction_count"] + stderr["redaction_count"]
            + error["redaction_count"]
        ),
        "network_failure_hint": _failure_category(
            stderr["text"] + "\n" + stdout["text"] + "\n" + error["text"]
        ),
        "runtime_status": str(receipt.get("status") or "")[:48],
        "runtime_reason_code": str(receipt.get("reason") or "")[:160],
        "executed": receipt.get("executed") is True,
        "command_exit_code": (
            data.get("exit_code")
            if isinstance(data.get("exit_code"), (int, type(None)))
            and not isinstance(data.get("exit_code"), bool) else None
        ),
        "verification_status": str(data.get("verification_status") or "")[:48],
        "execution_status": str(data.get("execution_status") or "")[:48],
        "failure_origin": str(data.get("failure_origin") or "")[:48],
        "shell_mode": str(data.get("shell_mode") or "")[:20],
        "launch_form": str(data.get("launch_form") or "")[:50],
        "process_started": data.get("process_started") is True,
        "process_state": str(data.get("process_state") or "")[:30],
        "timed_out": data.get("timed_out") is True,
        "stdout_bytes": data.get("stdout_bytes") if type(data.get("stdout_bytes")) is int else None,
        "stderr_bytes": data.get("stderr_bytes") if type(data.get("stderr_bytes")) is int else None,
        "stdout_sha256": str(data.get("stdout_sha256") or "")[:64],
        "stderr_sha256": str(data.get("stderr_sha256") or "")[:64],
        "stdout_raw_sha256": str(data.get("stdout_raw_sha256") or "")[:64],
        "stderr_raw_sha256": str(data.get("stderr_raw_sha256") or "")[:64],
        "capture_encoding": str(data.get("capture_encoding") or "")[:20],
        "decode_error_policy": str(data.get("decode_error_policy") or "")[:20],
        "output_complete": data.get("output_complete") is True,
        "observed_post_timeout_exit_code": data.get("observed_post_timeout_exit_code"),
        "command_rejection_stage": str(data.get("command_rejection_stage") or "")[:80],
        "sandbox_evidence": {
            key: evidence[key] for key in os_fields if key in evidence
        },
        "raw_result_included": False,
    }
