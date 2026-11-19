"""Staged path observations using isolated storage and browser fixtures.

Only this harness, its reports and its test output may be written. Production
task stores are read as bytes. No worker, Executor or live transport is invoked.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from WebAgent.tests.sidecar_git67_reproduction import (
    HISTORICAL_REQUEST_SHA256, HISTORICAL_TASKS, identity, restore_once,
)

GOOD = "E:" + chr(92) + "AIspace" + chr(92) + ".case_d_backups"
BAD = "E:" + chr(92) + "AIspace.case_d_backups"
HEX_GOOD = "45 3a 5c 41 49 73 70 61 63 65 5c 2e 63 61 73 65 5f 64 5f 62 61 63 6b 75 70 73"


def source_of(function):
    return {
        "file": str(Path(inspect.getsourcefile(function)).relative_to(
            Path(__file__).resolve().parents[3])),
        "function": function.__qualname__,
        "line": inspect.getsourcelines(function)[1],
    }


def sample(value):
    return None if value is None else {"text": value, **identity(value)}


class Matrix:
    def __init__(self, run_id):
        self.run_id = run_id
        self.rows = []
        self.captures = []
        self.blobs = {}

    def add(self, case, stage, expected, observed, *, source, kind="SIMULATED",
            task="", request="", message=None, update=None, note="", result=None):
        verdict = result or ("INCONCLUSIVE" if observed is None else
                             "PASS" if observed == expected else "FAIL")
        capture_id = len(self.captures)
        self.captures.append({
            "case_id": case, "stage": stage, "input": sample(expected),
            "output": sample(observed), "note": note,
        })
        values = identity(observed) if observed is not None else {}
        self.rows.append({
            "test_case_id": case, "test_run_id": self.run_id,
            "task_id": task, "request_id": request,
            "telegram_message_id": message, "telegram_update_id": update,
            "stage_name": stage, "source": source,
            "input_path": expected, "output_path": observed,
            "input_sha256": identity(expected)["sha256"] if expected is not None else None,
            "output_sha256": values.get("sha256"),
            "utf8_byte_count": values.get("utf8_bytes"),
            "code_point_count": values.get("chars"),
            "code_points": values.get("codepoints"),
            "backslash_positions": values.get("backslash_positions"),
            "path_hex": values.get("hex_utf8"),
            "evidence_source": kind,
            "evidence_file_reference": f"captures.json#/captures/{capture_id}",
            "result": verdict, "first_mismatch_indicator": False, "note": note,
            "result_scope": "Input/output fidelity, not goal success or path authorization",
            "execution_status": "OBSERVATION_GAP" if observed is None else "EXECUTED",
        })

    def capture(self, case, name, value):
        if len(value) > 2000:
            raw = value.encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            name_on_disk = digest + ".txt"
            self.blobs[name_on_disk] = value
            captured = {"sha256": digest, "chars": len(value),
                        "utf8_bytes": len(raw), "text_file": name_on_disk}
        else:
            captured = sample(value)
        self.captures.append({"case_id": case, "stage": name, "sample": captured})


def forms(path):
    return {
        "A_PLAIN": path,
        "B_INLINE": chr(96) + path + chr(96),
        "C_FENCE": chr(96) * 3 + "\n" + path + "\n" + chr(96) * 3,
        "D_JSON": json.dumps({"path": path}, ensure_ascii=False),
        "E_JOIN_PATH": "Join-Path -Path 'E:" + chr(92) + "AIspace' -ChildPath '.case_d_backups'",
        "F_GOLDEN": "".join(chr(ord(c)) for c in path),
    }


def path_in_text(text, form):
    if form == "D_JSON":
        return json.loads(text)["path"]
    if form == "E_JOIN_PATH":
        # Compare the actual Join-Path expression as text, without executing it.
        return text
    if form == "B_INLINE":
        return text[1:-1]
    if form == "C_FENCE":
        return text.split("\n", 2)[1]
    return text


def telegram_storage(matrix, case, text, form, path, index, root):
    from RemoteAgent.telegram_transport import (
        TelegramBotClient, TelegramReceiverConfig, TelegramUpdateAdapter,
    )
    from agent_core.agent_gateway import AgentIngressGateway
    from agent_core.task_state import TaskStateStore, RemoteTaskQueue
    from agent_core.transport_sessions import TransportSessionRouter
    from WebAgent.pokeclaw_executor_loop import _prompt
    from agent_core.task_continuation import parse_continuation

    message_id, update_id = 20000 + index, 10000 + index
    update = {"update_id": update_id, "message": {
        "message_id": message_id, "date": 1,
        "chat": {"id": 123, "type": "private"},
        "from": {"id": 123, "is_bot": False}, "text": text,
    }}
    body = json.dumps({"ok": True, "result": [update]},
                      ensure_ascii=False).encode("utf-8")
    matrix.capture(case, "T1_SIMULATED_HTTP_BODY", body.decode("utf-8"))
    config = TelegramReceiverConfig(bot_token="LOCAL-TEST-NO-NETWORK",
                                   allowed_user_ids=(123,), allowed_chat_ids=(123,))
    client = TelegramBotClient(config, opener=lambda *a, **kw: io.BytesIO(body))
    fetched = client.get_updates(offset=0, timeout=1)[0]
    normalized, reason = TelegramUpdateAdapter(config).normalize(fetched)
    if normalized is None:
        raise AssertionError(reason)
    store = TaskStateStore(root / "tasks.json")
    gateway = AgentIngressGateway(
        task_queue=RemoteTaskQueue(store),
        session_router=TransportSessionRouter(root / "sessions.json"))
    locals_seen = {}
    prior_trace = sys.gettrace()
    def trace(frame, event, arg):
        if frame.f_code is AgentIngressGateway.accept.__code__ and event == "return":
            locals_seen.update({k: frame.f_locals.get(k) for k in ("original_text", "text")})
        return trace
    # Isolate only workspace selection. Admission is never granted or dispatched.
    # The real Gateway, queue, JSON writer and task reload run in this temp root.
    try:
        sys.settrace(trace)
        with patch("agent_core.task_workspace_resolver.resolve_task_workspace",
                   return_value={"workspace": str(root), "source": "SIDECAR_STORAGE",
                                 "explicit_paths": [], "mentioned_paths": []}):
            accepted = gateway.accept(normalized, workspace=str(root))
    finally:
        sys.settrace(prior_trace)
    reloaded = TaskStateStore(root / "tasks.json")
    reloaded.load()
    task = reloaded.get(accepted.task.task_id)
    binding = dict(task=task.task_id, request=task.request_id,
                   message=message_id, update=update_id)
    stages = [
        ("T1", fetched["message"]["text"], TelegramBotClient._call),
        ("T2", fetched["message"]["text"], TelegramBotClient._call),
        ("T3", normalized.text, TelegramUpdateAdapter.normalize),
        ("T4", locals_seen["original_text"], AgentIngressGateway.accept),
        ("T5", task.metadata["source_user_command"], AgentIngressGateway.accept),
        ("T6", task.request, RemoteTaskQueue.enqueue_remote_request),
        ("T6_SOURCE_TEXT", task.metadata["source_text"], RemoteTaskQueue.enqueue_remote_request),
    ]
    for name, value, function in stages:
        assert value == text, (case, name)
        matrix.add(case, name, path, path_in_text(value, form),
                   source=source_of(function), **binding,
                   note="T1 is generated HTTP bytes, not a live Telegram capture."
                        if name == "T1" else "")
    matrix.add(case, "T7", path, None, source={
        "file": "source/RemoteAgent/telegram_webagent_worker.py",
        "function": "run_task", "line": 187,
    }, **binding, note="Worker not launched. T7 is covered separately by historical records.")
    revision = parse_continuation("resume TASK-SIDECAR\n調整需求：" + text)
    assert revision["goal"] == text
    matrix.add(case, "S6_CONTINUATION_TEXT", path, path_in_text(revision["goal"], form),
               source=source_of(parse_continuation), **binding)
    tools = SimpleNamespace(workspace_root=root, interface_name="remote",
                            web_file_capabilities=lambda: {})
    prompt = _prompt(task.request, tools, [], round_id=1, max_rounds=1)
    block = prompt.split("Original user request:\n", 1)[1].split(
        "\nFrozen Requirement-to-Goal", 1)[0]
    assert block == text
    matrix.capture(case, "T8_FULL_PROMPT_IDENTITY", prompt)
    matrix.add(case, "T8", path, path_in_text(block, form),
               source=source_of(_prompt), **binding,
               note="Path projection from the real full Prompt Builder output.")
    return prompt, binding


class ComposerFixture:
    def __init__(self, page):
        self._page = page

    def _composer_locator(self):
        return self._page.locator("#editor")

    def _focus_composer_dom(self):
        editor = self._composer_locator()
        editor.focus()
        return editor

    def _composer_matches_prompt(self, prompt):
        from agent_core.web_ui.providers.chatgpt.composer import (
            read_composer_snapshot, validate_composer_prompt,
        )
        self._last_composer_validation = validate_composer_prompt(
            prompt, read_composer_snapshot(self._composer_locator()))
        return self._last_composer_validation.matched

    def _read_composer_text_candidates(self):
        from agent_core.web_ui.providers.chatgpt.composer import read_composer_snapshot
        view = read_composer_snapshot(self._composer_locator())
        return [view.primary_text, view.semantic_text]

    def _normalize_composer_text(self, value):
        from agent_core.web_ui.providers.chatgpt.composer import canonical_composer_text
        return canonical_composer_text(value)

    def _check_cancel_requested(self, stage):
        pass

    def _log_stage(self, *args):
        pass


def browser_case(matrix, case, prompt, form, path, binding, page, root):
    from agent_core.composer_operation import _write_continuous_prompt
    from agent_core.web_ui.providers.chatgpt.composer import read_composer_snapshot
    from agent_core.web_ui.message_text import read_message_text, read_answer_text
    page.set_content('<textarea id="editor"></textarea>')
    record = {"url": page.url}
    _write_continuous_prompt(ComposerFixture(page), prompt, record, root / "writer.json")
    view = read_composer_snapshot(page.locator("#editor"))
    assert view.semantic_text == prompt
    for name, text in (("T9", prompt), ("T10", view.semantic_text)):
        block = text.split("Original user request:\n", 1)[1].split(
            "\nFrozen Requirement-to-Goal", 1)[0]
        matrix.add(case, name, path, path_in_text(block, form),
                   source=source_of(_write_continuous_prompt), **binding,
                   note="Isolated textarea, real default Git67 writer; no Send/network. Provider ProseMirror not exercised.")
    for property_name in ("value", "innerText", "textContent"):
        value = page.locator("#editor").evaluate("el => el." + property_name)
        matrix.capture(case, "T10_" + property_name, value)
    # Controlled HTML rendering only. No external Markdown engine is claimed.
    content = "<code>" + html.escape(path) + "</code>"
    page.set_content('<p id="message">' + content + '</p>')
    observed = read_message_text(page.locator("#message"))
    matrix.add(case, "S6_WEBUI_MESSAGE_DOM", path, observed,
               source=source_of(read_message_text), **binding,
               note="Synthetic HTML code node, not Telegram App or ChatGPT Markdown.")
    response = json.dumps({"type": "tool_call", "name": "create_directory",
                           "arguments": {"path": GOOD}}, ensure_ascii=False)
    for repeat in range(1, 4):
        page.set_content('<pre id="answer"><code>' + html.escape(response) + '</code></pre>')
        extracted = read_answer_text(page.locator("#answer"))
        assert json.loads(extracted)["arguments"]["path"] == GOOD
        matrix.add(case + f"-REPLY-{repeat}", "T12_SIMULATED_RESPONSE_EXTRACTION",
                   GOOD, json.loads(extracted)["arguments"]["path"],
                   source=source_of(read_answer_text), **binding,
                   note="Fixed synthetic reply. No model participated.")


def parser_case(matrix, case, path, binding=None):
    from WebAgent.minimal_decision import parse_minimal_decision_with_diagnostics
    from WebAgent.minimal_admission import admit_minimal_action
    response = json.dumps({"type": "tool_call", "name": "create_directory",
                           "arguments": {"path": path}}, ensure_ascii=False)
    decision, diagnostic = parse_minimal_decision_with_diagnostics(response, original_goal=path)
    captured = []
    def preflight(action):
        captured.append(dict(action))
        return {"allowed": False, "scope_diagnostic": {
            "reason_code": "SIDECAR_CAPTURE_ONLY", "failure_stage": "SIDECAR",
        }}
    context = SimpleNamespace(interface_name="remote", current_request_text=path,
                              preview_tool_scope=preflight)
    admitted, reason = admit_minimal_action(decision, context)
    assert admitted is None and captured, reason
    assert diagnostic["normalization"] == "NONE"
    for stage, value, function in (
        ("T13", json.loads(response)["arguments"]["path"], parse_minimal_decision_with_diagnostics),
        ("T14", decision.arguments["path"], parse_minimal_decision_with_diagnostics),
        ("T15", captured[0]["path"], admit_minimal_action),
        ("T16", captured[0]["path"], preflight),
    ):
        matrix.add(case, stage, path, value, source=source_of(function), **(binding or {}),
                   note="Real parser/admission, capture-only preflight rejects; no dispatch.")


def historical(matrix, history_root, root):
    task_path = history_root / "localdata/runtime/remote_tasks.json"
    continuation_path = history_root / "localdata/runtime/task_continuations.json"
    if not task_path.is_file() or not continuation_path.is_file():
        return {label: {"status": "HISTORICAL_REPLAY_NOT_AVAILABLE"}
                for label in HISTORICAL_TASKS}
    raw_tasks, raw_continuations = task_path.read_bytes(), continuation_path.read_bytes()
    tasks = json.loads(raw_tasks)["tasks"]
    continuations = json.loads(raw_continuations)["records"]
    results = {}
    for label, tid in HISTORICAL_TASKS.items():
        task, source = tasks.get(tid), continuations.get(tid)
        if not task or not source:
            results[label] = {"status": "HISTORICAL_REPLAY_NOT_AVAILABLE"}
            continue
        request_sha = identity(task["request"])["sha256"]
        request_valid = request_sha == HISTORICAL_REQUEST_SHA256
        binding = dict(task=tid, request=task["request_id"],
                       message=task["metadata"].get("source_message_id"),
                       update=task["metadata"].get("ingress_metadata", {}).get("update_id"))
        case = "HISTORY-" + label
        history_values = [
            ("T5", task["metadata"]["source_user_command"]),
            ("T6", task["request"]),
            ("T6_SOURCE_TEXT", task["metadata"]["source_text"]),
        ]
        worker_path = history_root / f"localdata/runtime/remote_workers/{tid}/runtime.jsonl"
        worker = [json.loads(line) for line in worker_path.read_bytes().splitlines()]
        started = next((r for r in worker if r.get("stage") == "request_started"), None)
        if started:
            history_values.append(("T7", started["request"]))
        for stage, value in history_values:
            assert BAD in value and GOOD not in value and identity(value)["sha256"] == request_sha
            matrix.add(case, stage, BAD, BAD, kind="HISTORICAL", **binding,
                       source={"file": str(task_path.relative_to(history_root)),
                               "function": "stored historical field", "line": None},
                       note="Already wrong at earliest saved input. No preceding correct sample.")
            matrix.rows[-1]["historical_file_reference"] = (
                str(worker_path.relative_to(history_root)) + "#request_started.request"
                if stage == "T7" else str(task_path.relative_to(history_root)) + "#" + tid
            )
        response_row = next(r for r in worker if r.get("stage") == "browser_response_completed"
                            and '"name":"create_directory"' in r.get("response", "").replace(" ", ""))
        from WebAgent.minimal_decision import parse_minimal_decision_with_diagnostics
        parsed, diagnostic = parse_minimal_decision_with_diagnostics(
            response_row["response"], original_goal=task["request"])
        board_path = history_root / f"localdata/runtime/executor_run_blackboard/{tid}.json"
        board = json.loads(board_path.read_bytes())
        event = next(e for e in board["events"] if e.get("toolName") == "create_directory")
        assert parsed.arguments == event["arguments"]
        for stage, value in (
            ("T13", json.loads(response_row["response"])["arguments"]["path"]),
            ("T14", parsed.arguments["path"]), ("T16", event["arguments"]["path"]),
        ):
            matrix.add(case, stage, BAD, value, kind="HISTORICAL", **binding,
                       source=source_of(parse_minimal_decision_with_diagnostics),
                       note="Historical browser-extracted reply, not service-side raw model tokens.")
        replay = restore_once(copy.deepcopy(source), root)
        frames = []
        for line in replay.get("traceback", "").splitlines():
            if "_restore_continuation" in line or "action_digest" in line:
                frames.append(line)
        results[label] = {
            "status": "HISTORICAL_REPLAY_COMPLETED",
            "request_sha256": request_sha, "request_sha_matches": request_valid,
            "source_file_sha256": hashlib.sha256(raw_tasks).hexdigest(),
            "continuation_file_sha256": hashlib.sha256(raw_continuations).hexdigest(),
            "source_result_count": len(source["results"]),
            "missing_action_digest_count": sum("action_digest" not in r for r in source["results"].values()),
            "restore_result": replay, "relevant_frames": frames,
            "parser_normalization": diagnostic["normalization"],
            "browser_extracted_create_directory_json": response_row["response"],
            "browser_response_sha256": identity(response_row["response"])["sha256"],
            "browser_response_log_line": next(
                i for i, row in enumerate(worker, 1) if row is response_row),
            "admission_round": event["round"],
            "admission_arguments": event["arguments"],
            "admission_reason": event["admission_diagnostic"]["reason_code"],
            "executed": event["runtimeEvidence"]["executed"],
            "original_file_references": [str(p.relative_to(history_root)) for p in
                                         (task_path, continuation_path, worker_path, board_path)],
            "followup_revisions": [{
                "task_id": child_id, "state": child.get("state"),
                "goal_changed": child.get("goal_changed"),
                "effective_goal_has_correct_path": GOOD in child.get("effective_goal", ""),
                "effective_goal_has_wrong_path": BAD in child.get("effective_goal", ""),
                "error": tasks.get(child_id, {}).get("error"),
            } for child_id, child in continuations.items()
                if child.get("source_task_id") == tid],
        }
        assert request_valid and replay["status"] == "KEYERROR_ACTION_DIGEST_REPRODUCED"
    # No production stores are written. Verify their bytes after replays as well.
    assert task_path.read_bytes() == raw_tasks
    assert continuation_path.read_bytes() == raw_continuations
    return results


def trace_probe(root):
    from agent_core import path_integrity_trace as pit
    marked = "NLIT_CASE_ID=IT10\nNLIT_TEST_PURPOSE=PATH_INTEGRITY_E2E\n" + GOOD
    with patch.dict(os.environ, {
        "SMARTAGENT_IT10_PATH_TRACE": "1",
        "SMARTAGENT_IT10_PATH_TRACE_PRIVATE_ACL_CONFIRMED": "YES",
        "SMARTAGENT_IT10_PATH_TRACE_RUN_ID": "SIDECAR-IT10-CONTROL",
        "SMARTAGENT_IT10_PATH_TRACE_ROOT": str(root),
    }):
        pit.begin(marked, request_id="RR-SIDECAR-TRACE", task_id="TASK-SIDECAR-TRACE")
        status = pit.diagnostic_status(marked)
        pit.record("02_telegram_message_text", marked, paths=[GOOD],
                   extra={"message_id": 222, "update_id": 111})
        rows = [json.loads(line) for p in root.glob("it10-path-*.jsonl")
                for line in p.read_text(encoding="utf-8").splitlines()]
        assert status == "READY" and rows and rows[0]["path_values"][0]["value"] == GOOD
        pit.begin("not an opted-in request")
    return {
        "controlled_offline_probe": "PASS", "source": source_of(pit.record),
        "controlled_trace_sample": rows[0],
        "production_settings_changed": False, "acl_changed": False,
        "acknowledgement": "Synthetic diagnostic fixture only, not a live ACL attestation",
        "live_T1_hook": "OBSERVATION_GAP",
        "reason": "TelegramBotClient._call reads body inside JSON decode; IT10 begins at decoded message.text.",
        "minimum_hook": "Select opted-in update inside _call and retain its original HTTP-body evidence in a private controlled collector. Record message/update/case identity. Do not retain unrelated batch messages.",
    }


def run(output, history_root, *, browser=True):
    output, history_root = output.resolve(), history_root.resolve()
    # Never use the formal runtime or its fixtures as an output root.
    if output == history_root or output.is_relative_to(history_root / "localdata") or str(output).lower().startswith("e:"):
        raise ValueError("sidecar_output_must_be_separate_diagnostic_artifact_directory")
    output.mkdir(parents=True, exist_ok=False)
    matrix = Matrix(output.name)
    assert GOOD.encode().hex(" ") == HEX_GOOD and len(GOOD) == 26 and len(BAD) == 25
    assert json.loads(json.dumps(GOOD)) == GOOD
    for name, value in (("correct", GOOD), ("incorrect", BAD)):
        (output / (name + ".txt")).write_bytes(value.encode("utf-8"))
        (output / (name + ".utf8.bin")).write_bytes(value.encode("utf-8"))
        matrix.add("S0-" + name, "S0_GOLDEN", value, value,
                   source=source_of(forms))
    with tempfile.TemporaryDirectory(prefix="git67-path-localization-") as temporary:
        temp = Path(temporary)
        trace = trace_probe(temp)
        contexts = []
        for index, (form, text) in enumerate(forms(GOOD).items(), 1):
            case = output.name + "-" + form
            root = temp / form
            root.mkdir()
            expected = text if form == "E_JOIN_PATH" else GOOD
            prompt, binding = telegram_storage(matrix, case, text, form, expected, index, root)
            parser_case(matrix, case, GOOD, binding)
            contexts.append((case, prompt, form, expected, binding, root))
        # A deliberately bad path is a negative preservation control.
        bad_root = temp / "bad"
        bad_root.mkdir()
        _, bad_binding = telegram_storage(
            matrix, output.name + "-BAD-CONTROL", BAD, "A_PLAIN", BAD, 99, bad_root)
        parser_case(matrix, output.name + "-BAD-CONTROL", BAD, bad_binding)
        browser_status = "NOT_EXECUTED"
        if browser:
            from playwright.sync_api import sync_playwright
            executable = os.environ.get("SMARTAGENT_OFFLINE_BROWSER")
            if not executable and os.name == "nt":
                executable = next((str(p) for p in (
                    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
                    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
                ) if p.is_file()), None)
            with sync_playwright() as pw:
                engine = pw.chromium.launch(headless=True, executable_path=executable)
                try:
                    page = engine.new_page()
                    page.route("**/*", lambda route: route.abort())
                    for context in contexts:
                        browser_case(matrix, *context[:-1], page, context[-1])
                    browser_status = "PASS_ISOLATED_DOM"
                finally:
                    engine.close()
        for repeat in range(1, 4):
            for stage in ("T10_LIVE", "T11", "T12_LIVE", "T13_LIVE"):
                matrix.add(f"S4-LIVE-{repeat}", stage, GOOD, None, kind="LIVE",
                           source={"file": None, "function": None, "line": None},
                           note="NOT_EXECUTED. No isolated live model/transport observation was attached.")
        for stage in ("T0_SENDER_LIVE", "T1_LIVE", "T2_LIVE"):
            matrix.add("S1-LIVE-GAP", stage, GOOD, None, kind="LIVE",
                       source=trace["source"], note="NOT_EXECUTED / OBSERVATION_GAP")
        history = historical(matrix, history_root, temp)
    counts = Counter((r["evidence_source"], r["result"]) for r in matrix.rows)
    report = {
        "schema": "GIT67_PATH_LOCALIZATION_V1",
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=history_root).decode().strip(),
        "harness_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "test_run_id": matrix.run_id, "browser": browser_status,
        "counts": {f"{k[0]}_{k[1]}": v for k, v in sorted(counts.items())},
        "live_executed_count": 0,
        "simulated_form_cases": 7, "synthetic_response_cases": 18 if browser else 0,
        "historical_task_cases": sum(v.get("status") == "HISTORICAL_REPLAY_COMPLETED" for v in history.values()),
        "PATH_CORRUPTION_FIRST_CONFIRMED_STAGE": "T5_source_user_command_ALREADY_BAD"
            if report_history_available(history) else "HISTORICAL_REPLAY_NOT_AVAILABLE",
        "PATH_CORRUPTION_FIRST_MISMATCH_INTERVAL": "UNOBSERVED_SENDER_INPUT_TO_T5; no paired correct-to-bad historical transition",
        "PATH_CORRUPTION_ROOT_CAUSE_CLASS": "UNKNOWN_INSUFFICIENT_EVIDENCE",
        "CONTINUATION_KEYERROR_REPRODUCTION": "REAL_HISTORICAL_LEDGER_REPRODUCED"
            if report_history_available(history) else "HISTORICAL_REPLAY_NOT_AVAILABLE",
        "RCA_CONCLUSION_CONSISTENCY": "CONSISTENT_WITH_PRIOR_UNKNOWN_AND_INDEPENDENT_RESTORE_FAILURE",
        "it10": trace, "history": history,
        "production_functions_modified": False, "production_ledgers_modified": False,
        "browser_control": "Isolated native textarea; provider-specific editor behavior is unverified",
        "harness_setup_notes": [
            "An initial legacy staged-writer/generic-contenteditable fixture failed chunk readback before evidence publication. It was replaced with the actual default continuous writer and a supported native textarea. That setup failure is not a localized backslash-loss finding."
        ],
        "dispatch_count": 0, "network_calls": 0,
        "stage_summary": {
            "S0": "PASS", "S1": "SIMULATED_PASS / LIVE_INCONCLUSIVE",
            "S2": "SIMULATED_T2_T6_PASS / LIVE_INCONCLUSIVE",
            "S3": "ISOLATED_DOM_PASS / LIVE_INCONCLUSIVE" if browser else "INCONCLUSIVE",
            "S4": "INCONCLUSIVE_NO_LIVE_MODEL; synthetic responses are controls only",
            "S5": "PASS_PATH_FIDELITY; preflight capture only, no authorization assertion",
            "S6": "OFFLINE_MATRIX_PASS / LIVE_NOT_EXECUTED",
            "S7": "HISTORICAL_REPLAY_COMPLETED" if report_history_available(history) else "INCONCLUSIVE",
        },
    }
    artifacts = {"matrix.json": {"rows": matrix.rows},
                 "captures.json": {"captures": matrix.captures},
                 "summary.json": report}
    for name, data in artifacts.items():
        (output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, value in matrix.blobs.items():
        (output / name).write_bytes(value.encode("utf-8"))
    files = [{"file": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
              "bytes": p.stat().st_size} for p in sorted(output.iterdir()) if p.is_file()]
    (output / "index.json").write_text(json.dumps({
        "schema": "GIT67_PATH_EVIDENCE_INDEX_V1", "files": files,
        "source_commit": report["git_head"], "run_id": matrix.run_id,
        "harness_source_sha256": report["harness_source_sha256"],
    }, indent=2), encoding="utf-8")
    return report


def report_history_available(history):
    return all(isinstance(v, dict) and v.get("status") == "HISTORICAL_REPLAY_COMPLETED"
               for v in history.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--history-root", required=True, type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    report = run(args.output, args.history_root, browser=not args.no_browser)
    print(json.dumps({k: v for k, v in report.items() if k != "history"},
                     ensure_ascii=False, indent=2))
    return 2 if any(k.endswith("_FAIL") for k in report["counts"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
