"""B-P1: attest completed, current Runtime observations without inventing conditions.

A successful filesystem probe proves *its own execution and observed payload*.
It does not imply the user's full task/postcondition is verified. The PR-A
ACTION may become SATISFIED, while independent terminal verification stays
UNKNOWN unless a separately linked verifier proves the business condition.
"""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Mapping

from .action_dependencies import command_is_proven_observation
from .tool_result_contract import decode_result

_NATIVE_REASONS = {
    "list_directory": "DIRECTORY_LISTED",
    "inspect_directory": "DIRECTORY_INSPECTED",
}


def observation_receipt(action: Mapping, result: str, ledger_entry: Mapping) -> dict:
    """Fail-closed proof of an actually executed, native read-only observation.

    Returns only a bounded status/code. Does not mutate the action or graph.
    The surrounding Protocol Loop is responsible for request/epoch identity,
    scope admission and durable action/result association.
    """
    tool = str(action.get("tool", ""))
    result_id = str(action.get("action_id", "") or "")
    envelope = decode_result(result) or {}
    if not result_id or envelope.get("tool") != tool:
        return {"admitted": False, "reason": "OBSERVATION_TOOL_IDENTITY_UNAVAILABLE"}
    if (envelope.get("status") != "SUCCEEDED"
            or envelope.get("executed") is not True
            or ledger_entry.get("execution_status") != "SUCCEEDED"
            or ledger_entry.get("result_status") != "SUCCEEDED"
            or ledger_entry.get("executed") is not True
            or ledger_entry.get("evidence_status") not in {"ACTIVE", None}):
        return {"admitted": False, "reason": "OBSERVATION_NOT_COMPLETED"}
    envelope_id = str(envelope.get("action_id", "") or "")
    if envelope_id and envelope_id != result_id:
        return {"admitted": False, "reason": "OBSERVATION_ACTION_MISMATCH"}
    data = envelope.get("data")
    if not isinstance(data, dict):
        return {"admitted": False, "reason": "OBSERVATION_DATA_INVALID"}
    # Observation admission does NOT erase an independently observed negative
    # predicate, mismatched expectation or rejected verifier.
    if (str(ledger_entry.get("verification_status", "UNKNOWN")).upper()
            in {"FAIL", "SPEC_INVALID"}
            or str(ledger_entry.get("expectation_status", "")).upper()
            in {"FAIL", "SPEC_INVALID"}):
        return {"admitted": False, "reason": "OBSERVATION_PREDICATE_FAILED"}

    if tool == "list_directory":
        if envelope.get("reason") != _NATIVE_REASONS[tool]:
            return {"admitted": False, "reason": "OBSERVATION_PRODUCER_UNRECOGNIZED"}
        if not isinstance(data.get("entries"), list) or not isinstance(data.get("text"), str):
            return {"admitted": False, "reason": "OBSERVATION_PAYLOAD_INCOMPLETE"}
        requested = str(action.get("path", "") or "").strip()
        resolved = str(envelope.get("resolved_path", "") or "")
        if not requested or not resolved or not Path(resolved).is_absolute():
            return {"admitted": False, "reason": "OBSERVATION_PATH_UNBOUND"}
        if Path(requested).expanduser().resolve() != Path(resolved).expanduser().resolve():
            return {"admitted": False, "reason": "OBSERVATION_PATH_MISMATCH"}
        return {"admitted": True, "reason": "NATIVE_DIRECTORY_LIST_CURRENT"}

    if tool == "inspect_directory":
        if envelope.get("reason") == "DIRECTORY_ABSENCE_OBSERVED":
            # A negative observation is not a positive B-P1 proof. The typed
            # Runtime Fact Ledger may nevertheless expose this known absence.
            return {"admitted": False, "reason": "OBSERVED_DIRECTORY_ABSENT"}
        if envelope.get("reason") != _NATIVE_REASONS[tool]:
            return {"admitted": False, "reason": "OBSERVATION_PRODUCER_UNRECOGNIZED"}
        try:
            payload = json.loads(str(result))
        except (ValueError, TypeError):
            return {"admitted": False, "reason": "OBSERVATION_PAYLOAD_NOT_INLINE"}
        paths = payload.get("paths") if isinstance(payload, dict) else None
        requested = action.get("paths")
        if (not isinstance(paths, list) or not paths
                or not isinstance(requested, list)
                or not requested or len(paths) != len(requested)):
            return {"admitted": False, "reason": "OBSERVATION_PAYLOAD_INCOMPLETE"}
        for want, row in zip(requested, paths):
            if not isinstance(want, str) or not isinstance(row, dict):
                return {"admitted": False, "reason": "OBSERVATION_PAYLOAD_INVALID"}
            if (row.get("authorized") is not True
                    or row.get("exists") is not True
                    or row.get("inaccessible_count") != 0
                    or row.get("errors") != []):
                return {"admitted": False, "reason": "OBSERVATION_PARTIAL_OR_INACCESSIBLE"}
            try:
                if Path(want).expanduser().resolve() != Path(str(row.get("path", ""))).expanduser().resolve():
                    return {"admitted": False, "reason": "OBSERVATION_PATH_MISMATCH"}
            except (ValueError, RuntimeError, OSError):
                return {"admitted": False, "reason": "OBSERVATION_PATH_INVALID"}
        return {"admitted": True, "reason": "NATIVE_DIRECTORY_INSPECT_CURRENT"}

    if tool == "run_command":
        if not command_is_proven_observation(action):
            return {"admitted": False, "reason": "INSPECT_COMMAND_NOT_PROVEN_READ_ONLY"}
        if (data.get("observation_confirmed") is not True
                or data.get("process_state") != "EXITED"
                or data.get("execution_status") != "SUCCEEDED"
                or data.get("exit_code") != 0
                or data.get("timed_out") is not False):
            return {"admitted": False, "reason": "INSPECT_EXECUTION_NOT_CONFIRMED"}
        if str(data.get("working_directory", "") or "") == "":
            return {"admitted": False, "reason": "INSPECT_CWD_UNBOUND"}
        return {"admitted": True, "reason": "PROVEN_INSPECT_EXIT_ZERO"}

    return {"admitted": False, "reason": "OBSERVATION_TOOL_NOT_SUPPORTED"}
