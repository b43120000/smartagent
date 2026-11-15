"""Local-only Python entry to the *existing* SmartAgent Runtime dispatch.

This is a test harness, not an HTTP service or alternate executor. It cannot
change ACL, enroll a Turbo Root, supply AppContainer attestation, or fall back
to host execution. Run explicitly with --live on a provisioned Windows host.

UT/CI may inject a fake policy/admission/dispatch to check the adapter's
contract; only a real Windows Broker live test proves OS isolation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

SCHEMA = "SMARTAGENT_RUNTIME_TEST_GATEWAY_V1"
TOOLS = frozenset({
    "run_command", "create_directory", "write_file", "delete_path",
    "observe_fact", "read_file", "list_directory", "inspect_directory",
})
MUTATORS = frozenset({"run_command", "create_directory", "write_file", "delete_path"})
FIELD_PATTERN = re.compile(r"^(?:status|reason|executed|data(?:\.[A-Za-z_][A-Za-z0-9_]*){1,5})$")
MAX_ACTIONS = 12
MAX_CASE_BYTES = 65536


class GatewayError(ValueError):
    """Invalid test fixture or non-native environment; no execution."""


def _path_key(value: object) -> str:
    # Real filesystem/policy binding is resolved by the current host OS.
    return str(Path(str(value)).expanduser().resolve()).casefold()


def _validate_case(spec: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(spec, dict) or set(spec) != {"schema", "workspace", "actions"}:
        raise GatewayError("GATEWAY_CASE_SCHEMA_INVALID")
    if spec["schema"] != SCHEMA:
        raise GatewayError("GATEWAY_CASE_VERSION_INVALID")
    workspace = spec["workspace"]
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise GatewayError("GATEWAY_WORKSPACE_MUST_BE_ABSOLUTE")
    actions = spec["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
        raise GatewayError("GATEWAY_ACTION_COUNT_INVALID")
    validated = []
    for row in actions:
        if not isinstance(row, dict) or set(row) != {"action", "expect"}:
            raise GatewayError("GATEWAY_ACTION_RECORD_INVALID")
        action = row["action"]
        expect = row["expect"]
        if not isinstance(action, dict) or action.get("tool") not in TOOLS:
            raise GatewayError("GATEWAY_TOOL_INVALID")
        if any(str(key).startswith("_") for key in action):
            raise GatewayError("GATEWAY_PRIVATE_MARKER_FORBIDDEN")
        if "action_id" in action or "task_id" in action or "request_id" in action:
            raise GatewayError("GATEWAY_IDENTITY_INJECTION_FORBIDDEN")
        if action["tool"] == "run_command":
            if (not isinstance(action.get("command"), str)
                    or not 0 < len(action["command"]) <= 8192
                    or action.get("operation") not in {
                        "INSPECT", "TEST", "VERIFY", "BUILD", "MUTATE",
                        "GIT_INSPECT", "GIT_MUTATE", "GENERAL"}
                    or type(action.get("timeout", 30)) is not int
                    or not 1 <= action.get("timeout", 30) <= 300
                    or not isinstance(action.get("working_directory"), str)
                    or not Path(action["working_directory"]).is_absolute()):
                raise GatewayError("GATEWAY_RUN_COMMAND_INVALID")
        elif action["tool"] in MUTATORS:
            if (not isinstance(action.get("path"), str)
                    or not Path(action["path"]).is_absolute()):
                raise GatewayError("GATEWAY_MUTATION_PATH_INVALID")
        if not isinstance(expect, dict) or not expect:
            raise GatewayError("GATEWAY_EXPECTATIONS_REQUIRED")
        if len(expect) > 32 or any(
            not isinstance(k, str) or not FIELD_PATTERN.fullmatch(k)
            or not isinstance(v, (str, int, float, bool, type(None)))
            for k, v in expect.items()
        ):
            raise GatewayError("GATEWAY_EXPECTATION_INVALID")
        validated.append({"action": dict(action), "expect": dict(expect)})
    return workspace, validated


def _field(receipt: Mapping[str, Any], field: str) -> Any:
    current: Any = receipt
    for key in field.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _safe_runtime_summary(receipt: Mapping[str, Any]) -> dict[str, Any]:
    data = receipt.get("data")
    data = data if isinstance(data, dict) else {}
    sandbox = data.get("sandbox_evidence")
    sandbox = sandbox if isinstance(sandbox, dict) else {}
    probes = sandbox.get("native_cwd_probes")
    probes = probes if isinstance(probes, dict) else {}
    accepted = (
        "sandbox_backend", "sandbox_identity_verified", "native_error_class",
        "native_failure_stage", "native_failure_substage",
        "native_exception_type", "native_exception_hresult",
        "native_process_pid", "native_running_sample_count",
    )
    probe_names = (
        "native_cwd_status", "native_cwd_hresult", "psdrive_status",
        "psdrive_hresult", "provider_path_status", "provider_path_hresult",
    )
    running = data.get("last_running_evidence") or {}
    running = running if isinstance(running, dict) else {}
    return {
        "tool_status": receipt.get("status"),
        "tool_reason": receipt.get("reason"),
        "executed": receipt.get("executed") is True,
        "exit_code": data.get("exit_code"),
        "process_state": data.get("process_state"),
        "execution_id": data.get("execution_id"),
        "sandbox": {k: sandbox[k] for k in accepted if k in sandbox},
        "cwd_probes": {k: probes[k] for k in probe_names if k in probes},
        "last_running_evidence": {
            k: running[k] for k in (
                "evidence_source", "pid", "execution_id",
                "liveness_verified", "observed_at") if k in running
        } if running else None,
    }


def run_case(
    spec: Mapping[str, Any],
    *,
    live_authorized: bool = False,
    policy_loader: Callable[[], Mapping[str, Any]] | None = None,
    admission: Callable[..., Mapping[str, Any]] | None = None,
    dispatch: Callable[..., str] | None = None,
    result_decoder: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Execute bounded test actions via normal Runtime, never host shell.

    Injected functions are for UT only; production CLI uses the actual imports.
    A test's asserted success is distinct from Runtime's observed outcome.
    """
    workspace, rows = _validate_case(spec)
    if not live_authorized:
        raise GatewayError("GATEWAY_EXPLICIT_LIVE_OPT_IN_REQUIRED")

    if policy_loader is None:
        from agent_core.workspace_access import access_snapshot
        policy_loader = access_snapshot
    if admission is None:
        from agent_core.turbo_broker_admission import admit_action
        admission = admit_action
    if dispatch is None:
        from agent_core.tools import execute_tool
        dispatch = execute_tool
    if result_decoder is None:
        from agent_core.tool_result_contract import decode_result
        result_decoder = decode_result

    # A fixture cannot create or widen an authorized Workspace. This is a
    # comparison against the *installed* Read/Write policy, not a mock ACL ON.
    policy = policy_loader()
    allowed_roots = policy.get("writable_workspaces") or ()
    if _path_key(workspace) not in {_path_key(p) for p in allowed_roots}:
        raise GatewayError("GATEWAY_WORKSPACE_NOT_CONFIGURED")

    agent = SimpleNamespace(
        workspace_root=Path(workspace),
        workspace=Path(workspace),
        interface_name="runtime_test_gateway",
        current_task_id="TASK-RTG-" + uuid.uuid4().hex.upper(),
        current_request_id="RR-RTG-" + uuid.uuid4().hex.upper(),
        _models_registry={},
    )
    cases = []
    for index, row in enumerate(rows):
        action = dict(row["action"])
        action["action_id"] = "ACT-RTG-" + uuid.uuid4().hex.upper()
        tool = action["tool"]
        gate = admission(workspace, action)
        decision = str(gate.get("decision") or "")
        # No mutator may drop to ACL OFF / ordinary executor during a Live
        # native test. Also reject a changed policy at final dispatch (the
        # normal Runtime re-attests native status a second time).
        if tool in MUTATORS and decision != "BROKER_REQUIRED":
            cases.append({
                "index": index, "tool": tool, "status": "BLOCKED",
                "reason": "GATEWAY_NATIVE_BROKER_REQUIRED",
                "admission_decision": decision,
                "admission_reason": str(gate.get("reason_code") or "")[:128],
                "executed": False,
            })
            break
        if tool not in MUTATORS and decision != "NORMAL":
            cases.append({
                "index": index, "tool": tool, "status": "BLOCKED",
                "reason": "GATEWAY_READ_ADMISSION_NOT_NORMAL",
                "executed": False,
            })
            break

        raw = dispatch(action, agent=agent)
        receipt = result_decoder(raw)
        if not isinstance(receipt, dict):
            cases.append({
                "index": index, "tool": tool, "status": "FAIL",
                "reason": "GATEWAY_RUNTIME_RECEIPT_UNPARSEABLE",
                "executed": False,
            })
            break
        data = receipt.get("data") or {}
        sandbox = data.get("sandbox_evidence") if isinstance(data, dict) else {}
        sandbox = sandbox if isinstance(sandbox, dict) else {}
        attested = (
            sandbox.get("sandbox_backend") == "WIN32_APPCONTAINER"
            and sandbox.get("sandbox_identity_verified") is True
            and sandbox.get("turbo_effective_for_action") == "ON"
        )
        if tool in MUTATORS and receipt.get("executed") is True and not attested:
            cases.append({
                "index": index, "tool": tool, "status": "FAIL",
                "reason": "GATEWAY_EXECUTED_WITHOUT_NATIVE_ATTESTATION",
                "executed": True, "admission_decision": decision,
                "runtime": _safe_runtime_summary(receipt),
                "runtime_receipt_sha256": hashlib.sha256(
                    str(raw).encode("utf-8")).hexdigest(),
            })
            break
        comparisons = []
        for field, expected in row["expect"].items():
            actual = _field(receipt, field)
            comparisons.append({
                "field": field, "expected": expected, "actual": actual,
                "pass": type(actual) is type(expected) and actual == expected,
            })
        verdict = "PASS" if all(c["pass"] for c in comparisons) else "FAIL"
        cases.append({
            "index": index, "tool": tool, "status": verdict,
            "admission_decision": decision,
            "runtime": _safe_runtime_summary(receipt),
            "assertions": comparisons,
            "runtime_receipt_sha256": hashlib.sha256(
                str(raw).encode("utf-8")).hexdigest(),
        })
        if verdict != "PASS":
            break

    overall = "PASS" if len(cases) == len(rows) and all(
        item["status"] == "PASS" for item in cases) else "FAIL"
    return {
        "schema": "SMARTAGENT_RUNTIME_TEST_REPORT_V1",
        "status": overall,
        "test_actions_requested": len(rows),
        "test_actions_observed": len(cases),
        "native_required": True,
        "planner_invoked": False,
        "runtime_dispatch_shared": True,
        "task_id": agent.current_task_id,
        "request_id": agent.current_request_id,
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local-only SmartAgent Runtime Native UT/IT gateway.")
    parser.add_argument("--case", required=True, help="Bounded JSON test fixture path")
    parser.add_argument(
        "--live", action="store_true",
        help="Explicitly authorize test actions through existing Runtime policy")
    args = parser.parse_args(argv)
    try:
        case_path = Path(args.case)
        if not case_path.is_file() or case_path.stat().st_size > MAX_CASE_BYTES:
            raise GatewayError("GATEWAY_CASE_FILE_UNAVAILABLE_OR_OVERSIZED")
        spec = json.loads(case_path.read_text(encoding="utf-8-sig"))
        result = run_case(spec, live_authorized=args.live)
    except (GatewayError, OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "schema": "SMARTAGENT_RUNTIME_TEST_REPORT_V1",
            "status": "BLOCKED",
            "reason": str(exc) if isinstance(exc, GatewayError) else type(exc).__name__,
            "executed": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
