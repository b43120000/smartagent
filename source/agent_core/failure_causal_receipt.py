"""Fail-closed, request/epoch-bound causal failure projection.

This adapter never grants execution/Scope, interprets Planner prose as Tool
Evidence, or exports raw command outputs. An actual typed Tool Result and the
matching action/evidence ledgers are required. Tool stderr is classified only
by conservative known process error patterns; ambiguous failures stay UNKNOWN.
"""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import re

SCHEMA = "SMARTAGENT_FAILURE_CAUSAL_RECEIPT_V1"

# These signatures recognize real subprocess stderr, NOT planned commands,
# instructions, or unverified user-provided values. No raw stderr is exported.
_SIGNATURES = (
    ("DNS_RESOLUTION_FAILED", re.compile(
        r"(?i)(could not resolve host|name or service not known|temporary failure in name resolution|"
        r"could not resolve proxy|no such host is known|"
        r"getaddrinfo failed|unable to resolve host)")),
    ("TLS_CERTIFICATE_ERROR", re.compile(
        r"(?i)(SSL certificate problem|certificate verify failed|"
        r"schannel:.*certificate|unable to get local issuer certificate|"
        r"certificate has expired)")),
    ("NETWORK_CONNECTION_FAILED", re.compile(
        r"(?i)(failed to connect to|connection timed out|connection refused|"
        r"network is unreachable|connection was reset|operation timed out)")),
    ("AUTHENTICATION_REJECTED", re.compile(
        r"(?i)(authentication failed|could not read username|"
        r"terminal prompts disabled|permission denied \(publickey\)|"
        r"invalid username or password|authentication required)")),
    ("REPOSITORY_NOT_FOUND_OR_INACCESSIBLE", re.compile(
        r"(?i)(repository not found|remote: not found|repository .* does not exist)")),
    ("GIT_COMMAND_UNAVAILABLE", re.compile(
        r"(?i)(git is not recognized as (?:an internal|the name)|"
        r"git: command not found|cannot find git executable)")),
)
_CLASS_TO_NEXT = {
    "DNS_RESOLUTION_FAILED": "CHECK_DNS_WITH_SCOPED_OBSERVATION",
    "TLS_CERTIFICATE_ERROR": "CHECK_TRUST_STORE_WITH_SCOPED_OBSERVATION",
    "NETWORK_CONNECTION_FAILED": "CHECK_NETWORK_WITH_SCOPED_OBSERVATION",
    "AUTHENTICATION_REJECTED": "CHECK_REPOSITORY_AUTHENTICATION_WITHOUT_EXPOSING_CREDENTIALS",
    "REPOSITORY_NOT_FOUND_OR_INACCESSIBLE": "VERIFY_REGISTERED_REPOSITORY_VISIBILITY",
    "GIT_COMMAND_UNAVAILABLE": "VERIFY_RUNTIME_GIT_CAPABILITY",
    "UNCLASSIFIED_FAILURE": "INSPECT_ACTUAL_GIT_FAILURE_EVIDENCE",
}
_LINE_STOP = re.compile(
    r"(?m)^((?:\[OPERATION_RESULT\]|\[RUNTIME_TOOL_RESULT\]|"
    r"\[TOOL_RESULT\]|VERIFICATION_STATUS:|EXPECTATION_STATUS:|"
    r"TERMINAL_ELIGIBLE:|error:|\[STDERR_LOCAL_REF\]))"
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


def _code(value: object, default: str = "UNKNOWN") -> str:
    s = str(value or "")
    return s if _SAFE_ID.fullmatch(s) else default


def _captured_stderr(raw: str) -> str:
    # Older command-result text is newline-based, and stdout precedes stderr.
    # Extract the section rather than matching diagnostics in Planner text,
    # echoed command strings, or unrelated source files.
    marker = "\nstderr:\n"
    start = raw.find(marker)
    if start < 0:
        return ""
    tail = raw[start + len(marker):]
    stop = _LINE_STOP.search(tail)
    if stop:
        tail = tail[:stop.start()]
    return tail[:8192].strip()


def _classify(stderr: str, *, timed_out: bool) -> tuple[str, bool]:
    if timed_out:
        return "NETWORK_TIMEOUT_OR_PROCESS_TIMEOUT", False
    matches = [name for name, regex in _SIGNATURES if regex.search(stderr)]
    if len(matches) == 1:
        # Repository-not-found can mask an authorization problem.
        return matches[0], matches[0] != "REPOSITORY_NOT_FOUND_OR_INACCESSIBLE"
    return "UNCLASSIFIED_FAILURE", False


def build_failure_receipt(action_id: str, *,
                          action_ledger: Mapping | None,
                          result_ledger: Mapping | None,
                          request_id: str, task_epoch: str) -> dict | None:
    from .tool_result_contract import decode_result
    evidence = (result_ledger or {}).get(action_id) or {}
    recorded = (action_ledger or {}).get(action_id) or {}
    if (not isinstance(evidence, Mapping) or not isinstance(recorded, Mapping)
            or evidence.get("request_id") != request_id
            or evidence.get("task_epoch") != task_epoch
            or evidence.get("evidence_status", "ACTIVE") != "ACTIVE"):
        return None
    action = recorded.get("action") or {}
    if not isinstance(action, Mapping):
        return None
    raw = str(recorded.get("result") or "")
    decoded = decode_result(raw)
    if not isinstance(decoded, Mapping) or decoded.get("schema") != "SMARTAGENT_TOOL_RESULT_V1":
        return None
    tool = _code(decoded.get("tool"), "")
    if tool != _code(action.get("tool"), ""):
        return None
    status = _code(decoded.get("status"))
    if status not in {"FAILED", "REJECTED", "PARTIAL", "UNKNOWN"}:
        return None
    data = decoded.get("data") if isinstance(decoded.get("data"), Mapping) else {}
    exit_code = data.get("exit_code")
    if type(exit_code) is not int or not -32768 <= exit_code <= 32767:
        exit_code = None
    timed_out = data.get("timed_out") is True
    stderr = _captured_stderr(raw) if tool == "run_command" else ""
    category, conclusive = _classify(stderr, timed_out=timed_out)
    if tool != "run_command":
        category, conclusive = "UNCLASSIFIED_FAILURE", False
    if status == "REJECTED" and decoded.get("executed") is False:
        category, conclusive = "ADMISSION_REJECTED", True
    next_evaluation = _CLASS_TO_NEXT.get(category, "INSPECT_RUNTIME_BLOCKER")
    return {
        "schema": SCHEMA,
        "request_id": request_id,
        "task_epoch": task_epoch,
        "action_id": _code(action_id),
        "tool": tool,
        "operation": _code(action.get("operation"), "") if tool == "run_command" else "",
        "execution": {
            "status": status,
            "reason_code": _code(decoded.get("reason")),
            "executed": decoded.get("executed") is True,
            "exit_code": exit_code,
            "timed_out": timed_out,
        },
        "failure": {
            "category": category,
            "classification_basis": ("CAPTURED_TOOL_STDERR_PATTERN"
                                     if stderr and category not in {"UNCLASSIFIED_FAILURE", "ADMISSION_REJECTED"}
                                     else "TYPED_TOOL_RESULT_OR_UNCLASSIFIED"),
            "classification_verified": conclusive,
            "stderr_present": bool(stderr),
            "stderr_sha256": sha256(stderr.encode("utf-8")).hexdigest() if stderr else "",
            "raw_stderr_exported": False,
        },
        "prerequisite": {
            "state": "UNRESOLVED",
            "blocking_evidence_ref": _code(action_id),
        },
        "replan": {
            "next_evaluation": next_evaluation,
            "requires_planner_choice": True,
            "candidate_tool_admission_required": True,
            "failed_action_must_not_be_replayed_unchanged": True,
        },
        "authorization_granted": False,
        "action_dispatched_by_receipt": False,
        "result_evidence_created_by_receipt": False,
    }


def recent_failure_receipts(*, action_ledger: Mapping | None,
                            result_ledger: Mapping | None,
                            request_id: str, task_epoch: str,
                            limit: int = 12) -> list[dict]:
    """Retain a bounded causal index across later Progress schema repairs."""
    ids = list((result_ledger or {}).keys())[-128:]
    rows = [build_failure_receipt(
        action_id, action_ledger=action_ledger, result_ledger=result_ledger,
        request_id=request_id, task_epoch=task_epoch
    ) for action_id in ids]
    return [item for item in rows if item is not None][-max(1, min(limit, 24)):]


__all__ = ["SCHEMA", "build_failure_receipt", "recent_failure_receipts"]
