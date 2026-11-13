"""Git55: immutable Requirement-to-Goal State Contract.

The compiler is conservative: it recognizes only requirements for which a
deterministic, source-grounded acceptance contract can be constructed. Unknown
natural language is marked UNCOMPILED and keeps the existing comparator; it
is never silently converted into an easy-to-pass generic observation.
No Goal Contract is an OS permission or a license to edit Runtime evidence.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import re
from collections.abc import Mapping

SCHEMA = "SMARTAGENT_REQUIREMENT_GOAL_STATE_CONTRACT_V1"
COMPARATOR_SCHEMA = "SMARTAGENT_GOAL_STATE_COMPARISON_V1"
_WIN_PATH = re.compile(r"[A-Za-z]:\\[^\s，,。；;？?\r\n]+")
_EXE = {
    name: re.compile(
        r"([A-Za-z]:[\\/][^\s'\"\r\n,，；;]+?\.venv[\\/]"
        + pattern + r")", re.I
    )
    for name, pattern in (
        ("git", r"git[\\/]cmd[\\/]git\.exe"),
        ("python", r"Scripts[\\/]python\.exe"),
    )
}
_READ_ONLY_TOOLS = frozenset({
    "read_runtime_evidence", "read_runtime_blackboard",
    "inspect_directory", "observe_fact", "list_directory",
    "read_file", "inspect_project_scope", "inspect_project_working_set",
})


def _digest(data: object) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _path(value: object) -> str:
    return ntpath.normcase(ntpath.normpath(str(value or "").strip().strip("'\"")))


def _condition(identifier: str, kind: str, subject: str,
               comparator: str, expected: object) -> dict:
    return {
        "condition_id": identifier, "condition_type": kind,
        "subject": subject, "comparator": comparator,
        "expected": expected, "required": True,
    }


def compile_requirement_goal_state(request: str) -> dict:
    """Compile before the first ToolCall; do not ask the Planner for checks."""
    request = str(request or "")
    lower = request.casefold()
    digest = hashlib.sha256(request.encode("utf-8")).hexdigest()
    result = {
        "schema": SCHEMA, "request_sha256": digest,
        "compiler": "DETERMINISTIC_CONSERVATIVE_V1",
        "status": "UNCOMPILED", "goal_kind": "UNRECOGNIZED",
        "conditions": [], "authorizes_operation": False,
        "uncompiled_reason": "NO_TRUSTWORTHY_SEMANTIC_COMPILATION",
    }
    # Six independent native diagnostics: the goal is a truthful diagnosis,
    # not the success of the diagnosed external executables.
    git_match = _EXE["git"].search(request)
    python_match = _EXE["python"].search(request)
    diagnostic = (
        "win32_appcontainer" in lower
        and "get-command" in lower
        and "test-path" in lower
        and "--version" in lower
        and "path" in lower
        and "git" in lower and "python" in lower
        and "acl on" in lower and "turbo on" in lower
        and git_match is not None and python_match is not None
        and bool(re.search(r"unverified|未驗證|無法判定|缺乏證據", lower))
        and not bool(re.search(
            r"必須.{0,18}(?:成功執行|正常啟動|載入成功)|"
            r"must.{0,18}(?:execute successfully|run successfully)",
            lower, re.I))
    )
    if diagnostic:
        paths = {
            "git": _path(git_match.group(1)),
            "python": _path(python_match.group(1)),
        }
        result.update(
            status="COMPILED",
            goal_kind="DIAGNOSTIC_EXECUTABLE_RESOLUTION",
            uncompiled_reason="",
            executable_targets=paths,
            conditions=[
                _condition("D01", "PROCESS_CONSTRAINT",
                           "all command sandbox attestations",
                           "NATIVE_APPCONTAINER_REQUIRED", True),
                _condition("D02", "PROCESS_CONSTRAINT",
                           "independent read-only steps",
                           "SIX_READ_ONLY_DIAGNOSTIC_ATTEMPTS", True),
                _condition("D03", "OBSERVATION", "AppContainer PATH",
                           "CAPTURED_VALUE", "REPORT_ACTUAL_MEMBERSHIP"),
                _condition("D04", "OBSERVATION", paths["git"],
                           "TEST_PATH_BOOLEAN_OR_UNVERIFIED", True),
                _condition("D05", "OBSERVATION", paths["python"],
                           "TEST_PATH_BOOLEAN_OR_UNVERIFIED", True),
                _condition("D06", "DIAGNOSTIC", "Get-Command git, python",
                           "CAPTURE_AND_CLASSIFY", "RESOLVED_OR_UNVERIFIED"),
                _condition("D07", "DIAGNOSTIC", paths["git"] + " --version",
                           "CAPTURE_AND_CLASSIFY", "VERSION_OR_UNVERIFIED"),
                _condition("D08", "DIAGNOSTIC", paths["python"] + " --version",
                           "CAPTURE_AND_CLASSIFY", "VERSION_OR_UNVERIFIED"),
                _condition("D09", "REPORT", "diagnostic findings",
                           "TRUTHFUL_UNCERTAINTY_REPORT", True),
            ],
        )
    else:
        # Small, unambiguous single-directory tasks, not multi-step plans.
        paths = _WIN_PATH.findall(request)
        if len(paths) == 1 and len(request) <= 700 and (
            re.search(r"(?:資料夾|目錄|directory|folder)", lower)
        ):
            # Single-goal only. A mixed request such as "create folder and
            # email a report" must not be accepted by checking just one
            # filesystem state. The anchored grammar intentionally fails
            # closed on extra clauses and numbered instructions.
            residue = re.sub(
                r"\s+", " ", request.replace(paths[0], " ")
            ).strip().casefold()
            single_goal = bool(
                re.fullmatch(
                    r"(?:請你?|請幫我|幫我)?\s*(?:確認|確保|驗證|保證)?\s*"
                    r"(?:這個|這|該|目標)?\s*(?:資料夾|目錄|directory|folder)\s*"
                    r"(?:是否存在|有沒有|存在嗎|存在|不存在)[？?。.!]*",
                    residue, re.I
                )
                or re.fullmatch(
                    r"(?:請你?|請幫我|幫我)?\s*(?:在\s*)?"
                    r"(?:建立|創建|新增|刪除|移除|create|mkdir|delete|remove)\s*"
                    r"(?:這個|這|該|目標)?\s*(?:資料夾|目錄|directory|folder)[。.!]*",
                    residue, re.I
                )
            )
            if not single_goal:
                result["uncompiled_reason"] = "MULTI_CLAUSE_OR_UNSUPPORTED_DIRECTORY_GOAL"
                result["contract_sha256"] = _digest(result)
                return result
            target = _path(paths[0].rstrip(".!"))
            question = bool(re.search(
                r"是否存在|有沒有|存在嗎|存在？|存在\?|是否有|does.*exist|is.*there",
                lower, re.I))
            exact_false = bool(re.search(r"不存在|刪除|移除|delete|remove", lower))
            exact_true = bool(re.search(
                r"建立|創建|新增|確保.*(?<!不)存在|確認.*(?<!不)存在|create|mkdir|ensure.*exists",
                lower, re.I))
            if question and not exact_false and not re.search(
                r"建立|創建|新增|create|mkdir", lower, re.I
            ):
                result.update(
                    status="COMPILED", goal_kind="DIRECTORY_OBSERVATION",
                    uncompiled_reason="", target=target,
                    conditions=[_condition(
                        "S01", "OBSERVATION", target,
                        "CONFIRMED_BOOLEAN", "TRUE_OR_FALSE")],
                )
            elif exact_false and not exact_true and not question:
                result.update(
                    status="COMPILED", goal_kind="DIRECTORY_EXACT_STATE",
                    uncompiled_reason="", target=target,
                    conditions=[_condition(
                        "S01", "EXACT_STATE", target, "EQ", False)],
                )
            elif exact_true and not exact_false and not question:
                result.update(
                    status="COMPILED", goal_kind="DIRECTORY_EXACT_STATE",
                    uncompiled_reason="", target=target,
                    conditions=[_condition(
                        "S01", "EXACT_STATE", target, "EQ", True)],
                )
    result["contract_sha256"] = _digest(result)
    return result


def validate_frozen_goal_contract(contract: object, request: str) -> bool:
    if not isinstance(contract, Mapping) or contract.get("schema") != SCHEMA:
        return False
    if contract.get("authorizes_operation") is not False:
        return False
    body = {k: v for k, v in contract.items() if k != "contract_sha256"}
    if _digest(body) != contract.get("contract_sha256"):
        return False
    return dict(contract) == compile_requirement_goal_state(request)


def _row(condition: Mapping, outcome: str, actual: object,
         evidence_refs: list[str], reason: str) -> dict:
    return {
        "condition_id": condition["condition_id"],
        "condition_type": condition["condition_type"],
        "subject": condition["subject"],
        "expected": condition["expected"],
        "actual": actual, "verdict": outcome, "reason_code": reason,
        "evidence_refs": evidence_refs,
    }


def _observed_directory(item: Mapping, target: str):
    action, receipt = item["action"], item["receipt"]
    if receipt.get("status") != "SUCCEEDED" or receipt.get("executed") is not True:
        return None
    path = action.get("path") or (
        (action.get("paths") or [""])[0]
        if isinstance(action.get("paths"), list) else ""
    )
    if _path(path) != target:
        return None
    data = receipt.get("data") or {}
    if (action.get("tool") == "inspect_directory"
            and data.get("observation_status") == "CONFIRMED"
            and type(data.get("observed_exists")) is bool):
        return bool(data["observed_exists"]), item["evidence_ref"], bool(
            data.get("observed_is_directory") is True
        )
    if action.get("tool") == "observe_fact" and isinstance(data.get("observations"), list):
        for fact in data["observations"]:
            if (isinstance(fact, Mapping)
                    and fact.get("predicate") in {"file.exists", "directory.exists"}
                    and type(fact.get("value")) is bool):
                return fact["value"], item["evidence_ref"], fact["value"]
    return None


def _step(command: str, paths: Mapping) -> str:
    command = command.strip()
    if re.fullmatch(r"(?is)(?:Write-Output\s+)?\$env:PATH", command):
        return "path"
    if re.fullmatch(
        r"(?is)Get-Command\s+git\s*,\s*python\s+-ErrorAction\s+SilentlyContinue",
        command,
    ):
        return "resolve"
    test_path = re.fullmatch(
        r"""(?is)Test-Path\s+(?:-LiteralPath\s+|-Path\s+)?(['"]?)([A-Za-z]:[\\/][^'"]+?)\1""",
        command,
    )
    if test_path:
        candidate = _path(test_path.group(2).strip())
        for name in ("git", "python"):
            if candidate == paths[name]:
                return name + "_exists"
    version = re.fullmatch(
        r"""(?is)&?\s*(['"]?)([A-Za-z]:[\\/][^'"]+?)\1\s+--version""",
        command,
    )
    if version:
        candidate = _path(version.group(2).strip())
        for name in ("git", "python"):
            if candidate == paths[name]:
                return name + "_version"
    return ""


def _has_stream_evidence(receipt: Mapping) -> bool:
    data = receipt.get("data") or {}
    sandbox = data.get("sandbox_evidence") or {}
    return (
        isinstance(sandbox, Mapping)
        and isinstance(sandbox.get("native_error_class"), str)
        and bool(sandbox.get("native_error_class"))
        and receipt.get("executed") is True
        and receipt.get("status") in {"SUCCEEDED", "FAILED"}
        and isinstance(data, Mapping)
        and type(data.get("exit_code")) is int
        and data.get("process_state") == "EXITED"
        and type(data.get("stdout")) is str
        and type(data.get("stderr")) is str
        and data.get("output_complete") is True
        and data.get("timed_out") is False
    )


def _diagnostic_actual(item: Mapping, name: str, paths: Mapping):
    receipt = item["receipt"]
    data = receipt.get("data") or {}
    output = str(data.get("stdout") or "")
    os_proof = data.get("sandbox_evidence") or {}
    if not _has_stream_evidence(receipt):
        return "UNVERIFIED", "COMMAND_STREAM_OR_EXIT_EVIDENCE_MISSING", {
            "exit_code": data.get("exit_code"),
            "stream_available": False,
        }
    actual = {
        "exit_code": data["exit_code"],
        "stdout": output[:256],
        "stderr": str(data["stderr"])[:256],
        "native_error_class": os_proof.get("native_error_class"),
        "native_executable_identity_verified": os_proof.get(
            "native_executable_identity_verified"),
        "native_executable_identity_reason": os_proof.get(
            "native_executable_identity_reason"),
        "native_executable_probe_name": os_proof.get(
            "native_executable_probe_name"),
        "native_executable_probe_exit_code": os_proof.get(
            "native_executable_probe_exit_code"),
        "native_executable_probe_pid": os_proof.get(
            "native_executable_probe_pid"),
        "native_executable_direct_launch_count": os_proof.get(
            "native_executable_direct_launch_count"),
        "native_executable_probe_is_second_process": os_proof.get(
            "native_executable_probe_is_second_process"),
        "native_executable_probe_stdout": str(
            os_proof.get("native_executable_probe_stdout") or "")[:256],
        "native_executable_probe_stderr": str(
            os_proof.get("native_executable_probe_stderr") or "")[:256],
        "native_executable_probe_capture_complete": os_proof.get(
            "native_executable_probe_capture_complete"),
        "native_executable_probe_output_source": os_proof.get(
            "native_executable_probe_output_source"),
    }
    if name.endswith("_exists"):
        if output.strip() in {"True", "False"}:
            actual["observed_boolean"] = output.strip() == "True"
            return "PASS", "TEST_PATH_BOOLEAN_OBSERVED", actual
        actual["classification"] = "UNVERIFIED"
        return "PASS", "TEST_PATH_UNVERIFIED_EXPLICIT", actual
    if name == "path":
        actual["membership"] = {
            tool: _path(exe.rsplit("\\", 1)[0]) in {
                _path(segment) for segment in output.split(";")
            } for tool, exe in paths.items()
        }
        return "PASS", "PATH_CAPTURED", actual
    if name in {"git_version", "python_version"}:
        expected_tool = name.split("_", 1)[0]
        proof = (
            os_proof.get("native_executable_identity_verified") is True
            and os_proof.get("native_executable_probe_name") == expected_tool
            and os_proof.get("native_executable_direct_launch_count") == 1
            and os_proof.get("native_executable_probe_is_second_process") is True
            and type(os_proof.get("native_executable_probe_exit_code")) is int
            and type(os_proof.get("native_executable_probe_pid")) is int
            and os_proof["native_executable_probe_pid"] > 0
        )
        if not proof:
            actual["classification"] = "UNVERIFIED"
            return "PASS", "NATIVE_CHILD_TOKEN_UNVERIFIED_EXPLICIT", actual
        if (data["exit_code"] != 0
                or os_proof["native_executable_probe_exit_code"] != 0):
            actual["classification"] = "EXIT_NONZERO"
            return "PASS", "NATIVE_CHILD_EXECUTION_NONZERO_OBSERVED", actual
        version_prefix = (
            "git version" if expected_tool == "git" else "python "
        )
        native_stdout = str(
            os_proof.get("native_executable_probe_stdout") or "")
        native_stderr = str(
            os_proof.get("native_executable_probe_stderr") or "")
        if (os_proof.get("native_executable_probe_capture_complete") is not True
                or os_proof.get("native_executable_probe_output_source")
                    != "OS_DIRECT_CHILD_PINNED_FILE_HANDLE"
                or version_prefix not in (native_stdout + "\n" + native_stderr
                    ).strip().casefold()):
            # The PowerShell parent stdout is NOT proof that this separately
            # OS-attested executable actually emitted its version.
            actual["classification"] = "UNVERIFIED"
            return "PASS", "NATIVE_CHILD_VERSION_OUTPUT_UNVERIFIED", actual
        actual["classification"] = "OS_DIRECT_CHILD_TOKEN_AND_VERSION_VERIFIED"
        return "PASS", "NATIVE_CHILD_EXECUTION_VERIFIED", actual
    if output.strip():
        actual["classification"] = "OBSERVED_OUTPUT"
        return "PASS", "COMMAND_OUTPUT_OBSERVED", actual
    actual["classification"] = "UNVERIFIED"
    return "PASS", "EMPTY_OUTPUT_UNVERIFIED", actual


def evaluate_frozen_goal_contract(contract: Mapping, history: list[dict],
                                  summary: str, *,
                                  admission_denials: list[dict] | None = None) -> dict:
    """Evaluate only actual decoded receipts, never Planner-authored checks."""
    if contract.get("status") != "COMPILED":
        return {
            "schema": COMPARATOR_SCHEMA, "supported": False,
            "verdict": "UNCOMPILED", "reason_code": "GOAL_CONTRACT_UNCOMPILED",
            "state_table": [],
        }
    conditions = contract["conditions"]
    kind = contract["goal_kind"]
    table = []
    if kind in {"DIRECTORY_OBSERVATION", "DIRECTORY_EXACT_STATE"}:
        target = contract["target"]
        observations = [
            row for item in history
            if (row := _observed_directory(item, target)) is not None
        ]
        condition = conditions[0]
        if not observations:
            table.append(_row(condition, "UNVERIFIED", None, [],
                              "REQUIRED_DIRECTORY_OBSERVATION_MISSING"))
        else:
            exists, evidence_ref, directory = observations[-1]
            expected = condition["expected"]
            # Existence=true must denote a directory, not an unrelated file.
            passed = (
                (expected == "TRUE_OR_FALSE" or exists is expected)
                and (not exists or directory)
            )
            table.append(_row(
                condition, "PASS" if passed else "FAIL",
                {"exists": exists, "is_directory": directory},
                [evidence_ref], "DIRECTORY_STATE_MATCH" if passed
                else "DIRECTORY_STATE_MISMATCH",
            ))
    elif kind == "DIAGNOSTIC_EXECUTABLE_RESOLUTION":
        paths = contract["executable_targets"]
        required = ("path", "git_exists", "python_exists",
                    "resolve", "git_version", "python_version")
        by_step = {step: [] for step in required}
        denied_steps = {"git_version": [], "python_version": []}
        # Rejected attempts are NOT ToolResults. They count only for the two
        # explicitly optional executable launches and only with a verified
        # Runtime Blackboard event binding to the frozen step.
        for denial in admission_denials or []:
            if not isinstance(denial, Mapping):
                continue
            step = str(denial.get("goal_diagnostic_step") or "")
            native_refused = (
                denial.get("kind") == "TURBO_NATIVE_CONFIRMATION_BLOCKED"
                and denial.get("executed") is False
                and denial.get("authorization_granted") is False
                and re.fullmatch(r"[0-9a-f]{64}",
                                 str(denial.get("command_sha256") or ""))
                and str(denial.get("reason_code") or "")
            )
            admission = denial.get("admission_diagnostic") or {}
            rca = denial.get("command_rca") or {}
            scoped_refused = (
                denial.get("kind") == "ADMISSION_REJECTED"
                and (denial.get("runtimeEvidence") or {}).get("status") == "REJECTED"
                and (denial.get("runtimeEvidence") or {}).get("executed") is False
                and admission.get("stage") == "SCOPE_PREFLIGHT"
                and str(admission.get("reason_code") or "").startswith("scope_rejected:")
                and rca.get("command_capture_status") == "CAPTURED"
                and not rca.get("command_truncated")
                and not rca.get("command_redaction_count")
                and re.fullmatch(r"[0-9a-f]{64}",
                                 str(rca.get("original_command_sha256") or ""))
                and str(admission.get("reason_code") or "")
            )
            if (step in denied_steps and denial.get("toolName") == "run_command"
                    and (native_refused or scoped_refused)):
                denied_steps[step].append(denial)
        unexpected = []
        for item in history:
            action = item["action"]
            tool = action.get("tool")
            if tool == "run_command":
                step = _step(str(action.get("command") or ""), paths)
                # The command itself is read-only, but a run_command envelope
                # may carry nested verify commands. These must not become
                # invisible side effects in an accepted diagnostic.
                safe_envelope = (
                    str(action.get("operation") or "INSPECT").upper()
                    in {"INSPECT", "GIT_INSPECT", "GENERAL"}
                    and not action.get("verify")
                    and not action.get("expected_verification")
                    and not action.get("expected_failure")
                )
                if step in by_step and safe_envelope:
                    by_step[step].append(item)
                else:
                    unexpected.append(item)
            elif tool not in _READ_ONLY_TOOLS:
                unexpected.append(item)
        all_run = [item for group in by_step.values() for item in group]
        sandbox_ok = bool(all_run) and all(
            (item["receipt"].get("data") or {}).get("sandbox_evidence", {}).get(
                "sandbox_backend") == "WIN32_APPCONTAINER"
            and (item["receipt"].get("data") or {}).get("sandbox_evidence", {}).get(
                "sandbox_identity_verified") is True
            and (item["receipt"].get("data") or {}).get("sandbox_evidence", {}).get(
                "turbo_effective_for_action") == "ON"
            and (item["receipt"].get("data") or {}).get("sandbox_evidence", {}).get(
                "kernel_outside_read_denied") is True
            and (item["receipt"].get("data") or {}).get("sandbox_evidence", {}).get(
                "kernel_outside_write_denied") is True
            for item in all_run
        )
        table.append(_row(
            conditions[0], "PASS" if sandbox_ok else "FAIL",
            {"attested_native_actions": sum(
                1 for item in all_run if (item["receipt"].get("data") or {}).get(
                    "sandbox_evidence", {}).get("sandbox_identity_verified") is True),
             "recognized_actions": len(all_run)},
            [item["evidence_ref"] for item in all_run],
            "NATIVE_SANDBOX_ATTESTED" if sandbox_ok else "NATIVE_SANDBOX_NOT_ATTESTED",
        ))
        coverage = all(
            len(by_step[step]) + len(denied_steps.get(step, [])) == 1
            for step in required
        )
        coverage = coverage and not unexpected and (
            len(all_run) + sum(len(rows) for rows in denied_steps.values()) == 6
        )
        table.append(_row(
            conditions[1], "PASS" if coverage else "FAIL",
            {"step_counts": {key: len(val) for key, val in by_step.items()},
             "policy_rejected_versions": {
                 key: len(val) for key, val in denied_steps.items()},
             "unexpected_action_count": len(unexpected)},
            [item["evidence_ref"] for item in all_run],
            "SIX_DISTINCT_READ_ONLY_COMMANDS" if coverage
            else "DIAGNOSTIC_ACTION_COVERAGE_INCOMPLETE",
        ))
        for idx, step in enumerate(required):
            condition = conditions[idx + 2]
            candidates = by_step[step]
            policy_denials = denied_steps.get(step, [])
            if not candidates and len(policy_denials) == 1:
                denied = policy_denials[0]
                table.append(_row(
                    condition, "PASS",
                    {"classification": "POLICY_REJECTED",
                     "reason_code": str(
                         denied.get("reason_code") or
                         (denied.get("admission_diagnostic") or {}).get("reason_code")
                         or "POLICY_REJECTED"),
                     "executed": False, "authorization_granted": False,
                     "exit_code": None, "stdout": None, "stderr": None},
                    [str(denied.get("event_id") or "")],
                    "TRUSTED_POLICY_REJECTION_OBSERVED",
                ))
                continue
            if len(candidates) != 1 or policy_denials:
                table.append(_row(
                    condition, "UNVERIFIED", {"attempts": len(candidates)}, [],
                    "DIAGNOSTIC_ACTION_MISSING_OR_DUPLICATED",
                ))
                continue
            item = candidates[0]
            verdict, reason, actual = _diagnostic_actual(item, step, paths)
            table.append(_row(
                condition, verdict, actual,
                [item["evidence_ref"]], reason,
            ))
        uncertain = any(
            row["reason_code"] in {
                "EMPTY_OUTPUT_UNVERIFIED",
                "TEST_PATH_UNVERIFIED_EXPLICIT",
                "COMMAND_STREAM_OR_EXIT_EVIDENCE_MISSING",
                "NATIVE_CHILD_TOKEN_UNVERIFIED_EXPLICIT",
                "NATIVE_CHILD_VERSION_OUTPUT_UNVERIFIED",
                "NATIVE_CHILD_EXECUTION_NONZERO_OBSERVED",
            } for row in table
        )
        policy_denied = any(
            row["reason_code"] == "TRUSTED_POLICY_REJECTION_OBSERVED"
            for row in table
        )
        mentions_denial = bool(re.search(
            r"拒絕|不允許|拒絕執行|denied|rejected|blocked",
            str(summary or ""), re.I
        ))
        mentions_uncertainty = bool(re.search(
            r"UNVERIFIED|未驗證|無法驗證|無法確認|無法判定|尚未證明|無法證明",
            str(summary or ""), re.I
        ))
        # The user requested a *report*, not only six tool invocations.
        # Require acknowledgement of each specified result dimension; accept
        # both English metadata keys and conventional Traditional Chinese.
        report_text = str(summary or "")
        report_patterns = {
            "path": r"\bPATH\b",
            "test_path": r"Test-Path",
            "get_command": r"Get-Command",
            "git": r"\bGit\b",
            "python": r"\bPython\b",
            "exit_code": r"Exit\s*Code|exit_code|退出碼",
            "stdout": r"stdout|標準輸出|輸出",
            "stderr": r"stderr|標準錯誤|錯誤輸出",
            "native_error_class": r"Native\s+Error\s+Class|native_error_class|原生錯誤類別",
            "evidence": r"Evidence|證據",
            "sandbox": r"Sandbox|AppContainer|沙盒",
        }
        reported = {
            key: bool(re.search(pattern, report_text, re.I))
            for key, pattern in report_patterns.items()
        }
        report_ok = all(reported.values()) and (
            not uncertain or mentions_uncertainty
        ) and (not policy_denied or mentions_denial)
        table.append(_row(
            conditions[-1], "PASS" if report_ok else "FAIL",
            {"explicit_uncertainty": mentions_uncertainty,
             "uncertainty_in_evidence": uncertain,
             "policy_rejection_in_evidence": policy_denied,
             "policy_rejection_reported": mentions_denial,
             "report_dimensions": reported},
            [item["evidence_ref"] for item in all_run],
            "DIAGNOSTIC_REPORT_CONSISTENT" if report_ok
            else "DIAGNOSTIC_UNCERTAINTY_NOT_REPORTED",
        ))
    else:
        return {
            "schema": COMPARATOR_SCHEMA, "supported": False,
            "verdict": "UNCOMPILED",
            "reason_code": "GOAL_CONTRACT_KIND_UNRECOGNIZED",
            "state_table": [],
        }
    unsatisfied = [x for x in table if x["verdict"] != "PASS"]
    return {
        "schema": COMPARATOR_SCHEMA, "supported": True,
        "contract_sha256": contract["contract_sha256"],
        "goal_kind": kind,
        "verdict": "PASS" if not unsatisfied else "FAIL",
        "reason_code": (
            "FROZEN_GOAL_STATE_SATISFIED" if not unsatisfied
            else "FROZEN_GOAL_STATE_NOT_SATISFIED"
        ),
        "state_table": table,
        "unsatisfied_condition_ids": [
            row["condition_id"] for row in unsatisfied
        ],
        "provenance": "RUNTIME_RECEIPTS_WITH_FROZEN_SOURCE_REQUIREMENT",
        "model_checks_authoritative": False,
    }


__all__ = [
    "SCHEMA", "COMPARATOR_SCHEMA", "compile_requirement_goal_state",
    "validate_frozen_goal_contract", "evaluate_frozen_goal_contract",
]
