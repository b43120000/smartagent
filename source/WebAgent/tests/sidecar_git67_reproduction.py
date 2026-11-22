"""Git67 sidecar: read-only, actual-code reproduction, no production changes.

The 12/2 action shapes are RCA-derived synthetic fixtures, NOT the historical
Task A/B ledger records. Historical request content must be supplied explicitly.
No Telegram network calls or Runtime mutations occur.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import traceback
from pathlib import Path
from types import SimpleNamespace


GOOD = r"E:\AIspace\.case_d_backups"
BAD = r"E:\AIspace.case_d_backups"
HISTORICAL_REQUEST_SHA256 = "01ba6338d521f40c25e8f56c274a73562d6f1147273d32fdbd0c721914db741a"
HISTORICAL_TASKS = {
    "A": "TASK-6362942614ED21147E5C",
    "B": "TASK-8DD4841172C1D3FDACD5",
}


def identity(text: str) -> dict:
    raw = text.encode("utf-8")
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "chars": len(text),
        "utf8_bytes": len(raw),
        "backslash_positions": [i for i, ch in enumerate(text) if ch == "\\"],
        "codepoints": [f"U+{ord(ch):04X}" for ch in text],
        "hex_utf8": raw.hex(" "),
    }


def synthetic_source(count: int, *, action_digest: bool) -> dict:
    """RCA-derived ledger SHAPE ONLY. IDs and receipts are synthetic."""
    source = {
        "task_id": "TASK-SIDECAR-ORIGINAL",
        "request_id": "RR-SIDECAR-ORIGINAL",
        "epoch": "epoch-original",
        "legacy": False,
        "actions": {}, "results": {}, "prepared": {}, "progress": {}, "pending": {},
    }
    for index in range(count):
        aid = f"SIDECAR-ACTION-{index:03d}"
        signature = hashlib.sha256(aid.encode("utf-8")).hexdigest()
        source["actions"][aid] = {
            "signature": signature,
            "action": {"tool": "observe_fact", "action_id": aid},
            "execution_status": "SUCCEEDED", "result": "{}",
        }
        result = {
            "action_id": aid, "tool": "observe_fact", "executed": True,
            "execution_status": "SUCCEEDED", "result_status": "SUCCEEDED",
            "verification_status": "PASS",
        }
        if action_digest:
            result["action_digest"] = signature
        source["results"][aid] = result
    return source


def restore_once(source: dict, root: Path) -> dict:
    """Call the REAL git67 method on an isolated in-memory loop substitute.

    FOLLOWUP_VERIFY skips RESUME progress writes. Empty authorized_paths
    prevents filesystem probing; no Executor is created or invoked.
    """
    from WebAgent.protocol_loop import WebAgentProtocolLoop
    observed_events = []
    scope = SimpleNamespace(workspace_root=root, interface_name="sidecar")
    loop = SimpleNamespace(
        continuation_source=copy.deepcopy(source),
        continuation_mode="FOLLOWUP_VERIFY",
        tools=scope, authorized_paths=[],
        task_id="TASK-SIDECAR-CHILD", run_id="RR-SIDECAR-CHILD",
        task_epoch="epoch-child", task_request="sidecar only",
        action_ledger={}, action_result_ledger={},
        _emit=lambda *args, **kwargs: observed_events.append(args),
    )
    try:
        WebAgentProtocolLoop._restore_continuation(loop)
    except Exception as exc:
        return {
            "status": "KEYERROR_ACTION_DIGEST_REPRODUCED"
            if isinstance(exc, KeyError) and exc.args == ("action_digest",)
            else "UNEXPECTED_EXCEPTION",
            "exception_type": type(exc).__name__,
            "exception_value": str(exc),
            "traceback": traceback.format_exc(),
            "executed_tools": 0,
            "source_ledger_unchanged": source == loop.continuation_source,
        }
    return {
        "status": "RESTORED",
        "imported_action_count": len(loop.action_result_ledger),
        "events": len(observed_events),
        "executed_tools": 0,
        "source_ledger_unchanged": source == loop.continuation_source,
    }


def path_fidelity(path: str) -> dict:
    """HTTP body is generated in memory, never sent to Telegram's servers."""
    from RemoteAgent.telegram_transport import (
        TelegramBotClient, TelegramReceiverConfig, TelegramUpdateAdapter,
    )
    from WebAgent.minimal_decision import parse_minimal_decision_with_diagnostics
    from agent_core.task_continuation import parse_continuation

    update = {
        "update_id": 111, "message": {
            "message_id": 222, "date": 1000000000,
            "chat": {"id": 123, "type": "private"},
            "from": {"id": 123, "is_bot": False},
            "text": "請建立備份目錄 " + path,
        },
    }
    class MockResponse:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def read(self):
            return json.dumps({"ok": True, "result": [update]},
                              ensure_ascii=False).encode("utf-8")

    config = TelegramReceiverConfig(
        bot_token="LOCAL-TEST-NO-NETWORK",
        allowed_user_ids=(123,), allowed_chat_ids=(123,),
    )
    client = TelegramBotClient(config, opener=lambda *args, **kw: MockResponse())
    fetched = client.get_updates(offset=0, timeout=1)[0]
    normalized, reason = TelegramUpdateAdapter(config).normalize(fetched)
    if normalized is None:
        raise AssertionError(f"telegram sidecar normalization failed: {reason}")
    stored = json.loads(json.dumps(normalized.text, ensure_ascii=False))
    msg = "接續工作 TASK-SIDECAR\n調整需求：正式改為 " + path
    revision = parse_continuation(msg)
    if revision is None:
        raise AssertionError("continuation text missing")
    model_json = json.dumps({
        "type": "tool_call", "name": "create_directory",
        "arguments": {"path": path},
    }, ensure_ascii=False)
    decision, diagnostics = parse_minimal_decision_with_diagnostics(
        model_json, original_goal=stored,
    )
    stages = {
        "provided": path,
        "telegram_fake_http_json": fetched["message"]["text"].split("目錄 ", 1)[1],
        "telegram_normalized": normalized.text.split("目錄 ", 1)[1],
        "json_storage_roundtrip": stored.split("目錄 ", 1)[1],
        "continuation_revision": revision["goal"].split("改為 ", 1)[1],
        "model_decision_parser": decision.arguments["path"],
    }
    return {
        "status": "PRESERVED" if all(value == path for value in stages.values())
                  else "TRANSFORMED",
        "input": identity(path),
        "stages": {name: identity(value) for name, value in stages.items()},
        "parser_normalization": diagnostics.get("normalization"),
        "live_telegram_tested": False,
        "live_chatgpt_prompt_tested": False,
    }


def check_historical_request(path: Path) -> dict:
    """Optional local artifact; no assumptions about its filename or provenance."""
    payload = path.read_bytes()
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        return {"status": "UNUSABLE_NON_UTF8", "filename": path.name}
    actual = identity(content)
    return {
        "status": "HISTORICAL_SHA_MATCH" if actual["sha256"] == HISTORICAL_REQUEST_SHA256
                  else "NOT_HISTORICAL_SHA_MATCH",
        "file": str(path),
        "sha256": actual["sha256"],
        "chars": actual["chars"], "utf8_bytes": actual["utf8_bytes"],
        "contains_good_path": GOOD in content,
        "contains_bad_path": BAD in content,
    }


def make_report(*, request_a: Path | None = None, request_b: Path | None = None,
                root: Path | None = None) -> dict:
    root = root or Path.cwd()
    wrong = restore_once(synthetic_source(12, action_digest=False), root)
    short = restore_once(synthetic_source(2, action_digest=False), root)
    control = restore_once(synthetic_source(2, action_digest=True), root)
    good = path_fidelity(GOOD)
    bad = path_fidelity(BAD)
    history = {}
    for key, file in (("A", request_a), ("B", request_b)):
        history[key] = check_historical_request(file) if file else {
            "status": "OBSERVATION_GAP",
            "reason": "historical request bytes not provided",
            "task_id": HISTORICAL_TASKS[key],
        }
    return {
        "schema": "SMARTAGENT_GIT67_SIDECAR_REPRODUCTION_V1",
        "branch": "git67/case-c-action-contract-repair",
        "source_status": "RCA_DERIVED_SYNTHETIC_LEDGER",
        "continuation": {"missing_digest_12": wrong,
                         "missing_digest_2": short, "complete_digest_control": control},
        "path": {"correct": good, "wrong": bad},
        "history": history,
        "first_corruption_point": "UNKNOWN_INSUFFICIENT_EVIDENCE",
        "changes_to_production_code": False,
        "changes_to_workspace": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-a", type=Path,
                        help="actual original Task A UTF-8 request TEXT (optional)")
    parser.add_argument("--request-b", type=Path,
                        help="actual original Task B UTF-8 request TEXT (optional)")
    args = parser.parse_args()
    report = make_report(request_a=args.request_a, request_b=args.request_b)
    # Avoid printing the whole 4021-character request or any private payload.
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (
        all(report["continuation"][x]["status"] == "KEYERROR_ACTION_DIGEST_REPRODUCED"
            for x in ("missing_digest_12", "missing_digest_2"))
        and report["continuation"]["complete_digest_control"]["status"] == "RESTORED"
        and all(report["path"][x]["status"] == "PRESERVED" for x in ("correct", "wrong"))
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
