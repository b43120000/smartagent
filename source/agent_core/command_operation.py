"""Typed semantic contract for the shared command executor.

``run_command`` remains the escape hatch that can invoke any approved command,
but the model must state why it is invoking it. Runtime validates only clear
contradictions; it never guesses task success from a command string.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping


COMMAND_OPERATIONS = {
    "INSPECT", "MUTATE", "BUILD", "TEST", "VERIFY",
    "GIT_INSPECT", "GIT_MUTATE", "PROCESS", "TRANSFER", "GENERAL",
}
COMMAND_RESULT_SCHEMA = "SMARTAGENT_COMMAND_RESULT_V1"
OPERATION_RESULT_CONTRACTS = {
    "INSPECT": ("execution_status", "stdout_or_local_ref"),
    "MUTATE": ("execution_status", "verification_status", "postcondition_evidence"),
    "BUILD": ("execution_status", "verification_status", "build_artifact_evidence"),
    "TEST": ("execution_status", "verification_status", "test_evidence"),
    "VERIFY": ("execution_status", "verification_status", "verifies_action_id"),
    "GIT_INSPECT": ("execution_status", "stdout_or_local_ref", "git_identity"),
    "GIT_MUTATE": ("execution_status", "verification_status", "git_identity"),
    "PROCESS": ("execution_status", "verification_status", "process_evidence"),
    "TRANSFER": ("execution_status", "verification_status", "artifact_evidence"),
    "GENERAL": ("execution_status", "stdout_or_local_ref"),
}


def normalize_command_operation(value: object) -> str:
    operation = str(value or "").strip().upper()
    if operation not in COMMAND_OPERATIONS:
        raise ValueError(
            "run_command_operation_invalid:"
            + (operation or "missing")
            + ":allowed=" + ",".join(sorted(COMMAND_OPERATIONS))
        )
    return operation


def _looks_like_git_mutation(command: str) -> bool:
    return bool(re.search(
        r"(?i)(?:^|[;&|]\s*)git(?:\.exe)?(?:\s+-C\s+[^;&|]+)?\s+"
        r"(?:add|am|apply|checkout|cherry-pick|clean|commit|merge|mv|pull|push|"
        r"rebase|reset|restore|revert|rm|stash|switch)\b",
        command,
    ))


def _looks_like_git(command: str) -> bool:
    return bool(re.search(r"(?i)(?:^|[;&|]\s*)git(?:\.exe)?(?:\s|$)", command))


def _looks_like_build(command: str) -> bool:
    return bool(re.search(
        r"(?i)(?:gradlew(?:\.bat)?\b.*\b(?:assemble|build|bundle|compile|externalNativeBuild)"
        r"|cmake(?:\.exe)?\s+--build\b|ninja(?:\.exe)?\b|msbuild(?:\.exe)?\b"
        r"|dotnet\s+build\b|npm\s+run\s+build\b|cargo\s+build\b)",
        command,
    ))


def _looks_like_test(command: str) -> bool:
    return bool(re.search(
        r"(?i)(?:\bpytest\b|\bctest\b|\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b"
        r"|\bcargo\s+test\b|\bdotnet\s+test\b|gradlew(?:\.bat)?\b.*\btest\w*\b)",
        command,
    ))


def operation_mismatch_detail(action: Mapping[str, Any]) -> str:
    """Return a diagnostic only for an unambiguous contradiction."""
    operation = normalize_command_operation(action.get("operation"))
    command = str(action.get("command", "") or "").strip()
    verifies = str(action.get("verifies_action_id", "") or "").strip()
    condition = str(action.get("condition_id", "") or "").strip()
    mismatch = ""
    if verifies and operation != "VERIFY":
        mismatch = "verifies_action_id_requires_VERIFY"
    elif operation == "VERIFY" and not verifies and not condition:
        mismatch = "VERIFY_requires_verifies_action_id_or_condition_id"
    elif operation == "GIT_INSPECT" and _looks_like_git_mutation(command):
        mismatch = "GIT_INSPECT_contains_git_mutation"
    elif operation == "GIT_MUTATE" and not _looks_like_git(command):
        mismatch = "GIT_MUTATE_requires_git_command"
    elif operation == "BUILD" and _looks_like_test(command) and not _looks_like_build(command):
        mismatch = "BUILD_contains_test_only_command"
    elif operation == "TEST" and _looks_like_build(command) and not _looks_like_test(command):
        mismatch = "TEST_contains_build_only_command"
    return f"operation={operation};reason={mismatch};command={command}" if mismatch else ""


def operation_phase(operation: object) -> str:
    value = normalize_command_operation(operation)
    if value in {"INSPECT", "GIT_INSPECT"}:
        return "DISCOVERY"
    if value in {"VERIFY", "TEST"}:
        return "VERIFY"
    return "EXECUTE"


def operation_requires_postcondition(operation: object) -> bool:
    return normalize_command_operation(operation) in {
        "MUTATE", "BUILD", "TEST", "VERIFY", "GIT_MUTATE", "PROCESS", "TRANSFER",
    }


def operation_terminal_eligible(operation: object, verification_status: object) -> bool:
    value = normalize_command_operation(operation)
    verification = str(verification_status or "UNKNOWN").strip().upper()
    if value == "GENERAL":
        return False
    if operation_requires_postcondition(value):
        return verification in {"PASS", "FAIL"}
    return True


def result_schema_for_operation(operation: object) -> str:
    value = normalize_command_operation(operation)
    return f"SMARTAGENT_COMMAND_RESULT_{value}_V1"


def operation_result_contract(operation: object) -> tuple[str, ...]:
    return OPERATION_RESULT_CONTRACTS[normalize_command_operation(operation)]


def build_operation_result(
    operation: object,
    *,
    command: str,
    execution_status: str,
    verification_status: str,
    stdout: str = "",
    stdout_ref: str = "",
    verification_evidence: object = None,
    verifies_action_id: str = "",
    condition_id: str = "",
    expectation_status: str = "",
    expected_failure: object = None,
    expectation_evidence: object = None,
) -> dict[str, Any]:
    """Build the machine-readable result required by the typed operation.

    The command transcript remains available for a human/model, while this
    compact object is the Runtime-owned state-machine input.  Evidence fields
    describe what was observed; their presence never implies PASS by itself.
    """
    value = normalize_command_operation(operation)
    output = str(stdout or "")
    evidence = list(verification_evidence or [])
    execution = str(execution_status or "UNKNOWN").upper()
    verification = str(verification_status or "UNKNOWN").upper()
    reference = {
        "local_ref": str(stdout_ref or ""),
        "inline_bytes": len(output.encode("utf-8", errors="replace")),
        "sha256": hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()
        if output else "",
    }
    result: dict[str, Any] = {
        "schema": result_schema_for_operation(value),
        "operation": value,
        "execution_status": execution,
        "verification_status": verification,
        "terminal_eligible": operation_terminal_eligible(value, verification),
    }
    expectation = str(expectation_status or "").strip().upper()
    if expectation:
        result["expectation_status"] = expectation
        result["expected_failure"] = dict(expected_failure or {})
        result["expectation_evidence"] = dict(expectation_evidence or {})
        result["terminal_eligible"] = expectation == "PASS"
    if value in {"INSPECT", "GENERAL"}:
        result["stdout_or_local_ref"] = reference
    if value in {"MUTATE", "BUILD", "TEST", "GIT_MUTATE", "PROCESS", "TRANSFER"}:
        result["postcondition_evidence"] = evidence
    if value == "BUILD":
        result["build_artifact_evidence"] = evidence
    elif value == "TEST":
        result["test_evidence"] = evidence
    elif value == "VERIFY":
        result["verifies_action_id"] = str(verifies_action_id or "")
        result["condition_id"] = str(condition_id or "")
        result["postcondition_evidence"] = evidence
    elif value in {"GIT_INSPECT", "GIT_MUTATE"}:
        result["git_identity"] = reference
        if value == "GIT_INSPECT":
            result["stdout_or_local_ref"] = reference
    elif value == "PROCESS":
        result["process_evidence"] = evidence
    elif value == "TRANSFER":
        result["artifact_evidence"] = evidence
    return result


__all__ = [
    "COMMAND_OPERATIONS", "COMMAND_RESULT_SCHEMA", "OPERATION_RESULT_CONTRACTS",
    "normalize_command_operation",
    "operation_mismatch_detail", "operation_phase", "operation_requires_postcondition",
    "operation_terminal_eligible", "result_schema_for_operation", "operation_result_contract",
    "build_operation_result",
]
