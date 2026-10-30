"""Create a bounded, sanitized GAR L4 Live evidence bundle without stopping Controller.

Read-only inputs: webagent_runtime.jsonl, Git, Manifest, most recent regression
summaries. Output: a fresh localdata/gar_debug_collections folder and ZIP.
No raw prompts, replies, browser URLs, tokens, profiles, or cookies are copied.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ALLOWED_FIELDS = {
    "timestamp", "event", "pid", "request_id", "round", "level", "reason",
    "last_reason", "exception_type", "error_type", "stage", "status",
    "task_id", "task_outcome", "verification_status", "admission_status",
    "ack_id", "rejected_rounds", "local_repair_exhausted", "local_rejected_total",
    "max_global_rejections", "rebase_count", "replan_count", "fresh_session_count",
    "old_planner_fenced", "supervisor_review_returned", "fresh_repair_attempts",
    "planner_owner", "original_ack_preserved", "advice_sha256",
    "completed_step_count", "remaining_step_count", "injected_count",
    "sequence", "fault_code", "fault_mode", "max_faults",
    "action_executed", "single_use", "release_reason", "eligible_input",
    "enabled", "fresh_target_configured", "same_context", "bootstrap_sent",
    "fresh_context_digest", "rejected_turns_total",
    "rejected_turns_without_new_evidence", "local_reason_codes",
    "schema", "final_content_sha256",
}
EVENT_STEPS = (
    "gar_d1_test_armed",
    "gar_local_repair_handoff",
    "gar_l4_webgpt1_paused_for_review",
    "gar_l4_webgpt2_advice_received",
    "gar_l4_webgpt1_resumed",
    "gar_d1_test_resume_release",
    "protocol_loop_completed",
)
TOKEN = re.compile(
    r"(?i)\b(?:authorization|bearer|api[_-]?key|password|secret|token|cookie)"
    r"\s*(?:=|:)\s*[^\s,;}]+"
)
URL = re.compile(r"https?://[^\s\"'<>]+", flags=re.I)
EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}\b")
USERPATH = re.compile(r"(?i)(?:[a-z]:\\users\\|/users/|/home/)[^\\/\s\"']+")


def scrub(value: object, max_chars: int = 500) -> str:
    result = str(value or "")
    result = TOKEN.sub("[REDACTED_SECRET]", result)
    result = URL.sub("[REDACTED_URL]", result)
    result = EMAIL.sub("[REDACTED_EMAIL]", result)
    result = USERPATH.sub("[REDACTED_USER_PATH]", result)
    return result[:max_chars]


def load_jsonl(path: Path, max_lines: int = 60000) -> tuple[list[dict], int]:
    if not path.is_file():
        return [], 0
    parsed = []
    invalid = 0
    with path.open(encoding="utf-8", errors="replace") as fp:
        for line in deque(fp, maxlen=max_lines):
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    parsed.append(item)
                else:
                    invalid += 1
            except (ValueError, TypeError):
                invalid += 1
    return parsed, invalid


def select_request(events: list[dict], specified: str = "") -> tuple[str, list[dict]]:
    if specified:
        request_id = specified
    else:
        candidates = [e for e in events
                      if e.get("request_id") and e.get("event") in {
                          "protocol_loop_completed", "protocol_loop_paused",
                          "gar_l4_supervisor_review_failed", "gar_l4_webgpt1_resumed",
                          "gar_recovery_decision", "gar_semantic_stagnation_decision",
                      }]
        request_id = str(candidates[-1].get("request_id")) if candidates else ""
    if not request_id:
        return "", []
    indexed = [(i, e) for i, e in enumerate(events)
               if str(e.get("request_id", "")) == request_id]
    if not indexed:
        return request_id, []
    first, last = indexed[0][0], indexed[-1][0]
    pid = indexed[-1][1].get("pid")
    # D1 events do not carry request_id: capture the nearest preceding arm.
    previous_armed = [i for i, e in enumerate(events[:first])
                      if e.get("event") == "gar_d1_test_armed"
                      and e.get("pid") == pid]
    start = first
    if previous_armed:
        arm_index = previous_armed[-1]
        if first - arm_index <= 80:
            start = arm_index
    # Include response/cleanup events between the selected request's events.
    selection = []
    for i in range(start, last + 1):
        event = events[i]
        identity = str(event.get("request_id", "") or "")
        if identity == request_id:
            selection.append(event)
        elif not identity and event.get("pid") == pid:
            if (str(event.get("event", "")).startswith("gar_d1_test_")
                    or event.get("event") in {
                        "browser_send_started", "browser_response_completed",
                        "browser_response_failed", "browser_wait_heartbeat",
                    }):
                selection.append(event)
    return request_id, selection


def review_info(response: object) -> dict:
    raw = str(response or "")
    info = {
        "response_length": len(raw),
        "mentions_smartagent_tool": "smartagent_tool" in raw.lower(),
        "contains_markdown_fence": raw.lstrip().startswith("```"),
    }
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        info["json_valid"] = False
        return info
    info["json_valid"] = isinstance(obj, dict)
    if not isinstance(obj, dict):
        return info
    info["schema"] = scrub(obj.get("schema"), 120)
    info["field_names"] = sorted(obj)
    info["nonce_present"] = bool(obj.get("nonce"))
    advice_keys = (
        "completed_steps", "blocked_step", "failure_analysis",
        "revised_step", "remaining_plan", "safety_notes",
    )
    # Only a bounded, visibly redacted representation of a review, no nonce.
    info["advice_redacted"] = {
        key: ([scrub(x, 600) for x in obj.get(key, [])[:20]]
              if isinstance(obj.get(key), list) else scrub(obj.get(key), 1800))
        for key in advice_keys if key in obj
    }
    return info


def event_for_export(event: dict) -> dict:
    clean = {}
    for key in ALLOWED_FIELDS:
        if key not in event:
            continue
        value = event[key]
        if isinstance(value, (bool, int, float)) or value is None:
            clean[key] = value
        elif isinstance(value, list):
            clean[key] = [scrub(item, 140) for item in value[:16]]
        else:
            clean[key] = scrub(value, 500)
    if (event.get("event") in {"browser_response_completed", "browser_response_failed"}
            and event.get("stage") == "GAR L4 WebGPT2 read-only recovery review"):
        if event.get("event") == "browser_response_completed":
            clean["review_metadata"] = review_info(event.get("response"))
        else:
            clean["browser_error_category"] = scrub(event.get("error_type"), 100)
    return clean


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                               default=str) + "\n", encoding="utf-8")


def git_info(root: Path) -> dict:
    def invoke(*argv: str) -> tuple[int, str]:
        try:
            r = subprocess.run(["git", "-C", str(root), *argv],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=8, check=False)
            return r.returncode, r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return 127, ""
    rc, head = invoke("rev-parse", "HEAD")
    _, branch = invoke("branch", "--show-current")
    status_rc, status = invoke("status", "--porcelain")
    return {
        "head_sha": head if rc == 0 and re.fullmatch(r"[0-9a-f]{40}", head) else "UNKNOWN",
        "branch": scrub(branch, 100),
        "status_observed": status_rc == 0,
        "working_tree_dirty": bool(status) if status_rc == 0 else None,
        "status_entry_count": len(status.splitlines()) if status_rc == 0 else None,
    }


def manifest_info(root: Path) -> dict:
    try:
        sys.path.insert(0, str(root / "source"))
        from agent_core.protocol_manifest import require_protocol_manifest
        report = require_protocol_manifest(str(root))
        return {
            "status": "PASS" if report.get("hash_verification") == "PASS"
                      and not report.get("hash_mismatches") else "FAIL",
            "core_file_count": report.get("core_file_count"),
            "hash_verification": report.get("hash_verification"),
            "mismatch_count": len(report.get("hash_mismatches") or []),
        }
    except Exception as exc:
        return {"status": "ERROR", "exception_type": type(exc).__name__}


def regression_summaries(root: Path, outdir: Path) -> list[dict]:
    found = []
    for kind in ("gar-a0-regression", "gar-d1-regression"):
        base = root / "localdata" / "logs" / kind
        summaries = sorted(base.glob("*/summary.txt"), key=lambda p: p.stat().st_mtime)
        if summaries:
            last = summaries[-1]
            data = scrub(last.read_text(encoding="utf-8", errors="replace"), 5000)
            (outdir / (kind + "-latest-summary.txt")).write_text(
                data + "\n", encoding="utf-8")
            found.append({"kind": kind, "summary_found": True,
                          "mtime": datetime.fromtimestamp(
                              last.stat().st_mtime).isoformat(timespec="seconds")})
        else:
            found.append({"kind": kind, "summary_found": False})
    return found


def collect(root: Path, *, request_id: str = "", now: datetime | None = None) -> Path:
    root = root.resolve()
    now = now or datetime.now().astimezone()
    base = root / "localdata" / "gar_debug_collections"
    base.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M%S_%f")
    outdir = base / ("GAR_L4_" + stamp)
    outdir.mkdir(parents=True, exist_ok=False)
    log = root / "localdata" / "logs" / "webagent_runtime.jsonl"
    records, invalid = load_jsonl(log)
    selected_id, matching = select_request(records, request_id)
    sanitized = [event_for_export(item) for item in matching]
    with (outdir / "runtime_events_sanitized.jsonl").open("w", encoding="utf-8") as handle:
        for item in sanitized:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    names = [str(row.get("event", "")) for row in matching]
    success = any(row.get("event") == "protocol_loop_completed"
                  and row.get("task_outcome") == "SUCCESS"
                  for row in matching)
    checks = {event: (event in names) for event in EVENT_STEPS}
    checks["terminal_success"] = success
    # A Controller can complete without L4. Never infer L4 from task SUCCESS.
    checks["l4_full_e2e"] = all(checks.values())
    reviews = [row.get("review_metadata") for row in sanitized
               if row.get("review_metadata")]
    report = {
        "schema": "GAR_L4_DEBUG_COLLECTION_V1",
        "collected_at": now.isoformat(timespec="seconds"),
        "selected_request_id": selected_id or "NONE",
        "log_present": log.is_file(),
        "log_tail_lines_scanned": len(records),
        "log_invalid_jsonl_lines": invalid,
        "selected_event_count": len(sanitized),
        "first_selected_timestamp": (sanitized[0].get("timestamp") if sanitized else None),
        "last_selected_timestamp": (sanitized[-1].get("timestamp") if sanitized else None),
        "event_checks": checks,
        "review_count": len(reviews),
        "gar_l4_review_errors": [
            {"reason": row.get("reason"), "exception_type": row.get("exception_type")}
            for row in sanitized if row.get("event") == "gar_l4_supervisor_review_failed"
        ],
        "interpretation": ("D1 v2 E2E event signature observed"
                           if checks["l4_full_e2e"] else
                           "NOT PROVEN: see event_checks; no success inferred"),
    }
    write_json(outdir / "acceptance_report.json", report)
    write_json(outdir / "webgpt2_review_redacted.json", reviews)
    write_json(outdir / "checkout_identity.json", git_info(root))
    write_json(outdir / "strict_manifest_gate.json", manifest_info(root))
    write_json(outdir / "regression_index.json", regression_summaries(root, outdir))
    readme = (
        "GAR L4 live debug evidence (sanitized)\n"
        "Selected request: " + (selected_id or "NONE") + "\n"
        "Event PASS is based on same-request Runtime observations, not screenshots.\n"
        "Original raw Runtime JSONL is deliberately NOT included.\n"
        "WebGPT2 advice may contain user task wording; inspect before sharing.\n"
        "No browser cookies, profiles, ChatGPT URLs, passwords or raw prompts are copied.\n"
        "Contents: acceptance_report.json, runtime_events_sanitized.jsonl,\n"
        "webgpt2_review_redacted.json, checkout_identity.json,\n"
        "strict_manifest_gate.json, regression summaries/index.\n"
        "To collect a different request: collectData.bat --request-id WA-...\n"
    )
    (outdir / "README.txt").write_text(readme, encoding="utf-8")
    archive = outdir.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(outdir.iterdir()):
            if file.is_file():
                zf.write(file, arcname=outdir.name + "/" + file.name)
    print("\n" + "=" * 65)
    print("[GAR COLLECT] Evidence folder:")
    print(str(outdir))
    print("[GAR COLLECT] Ready-to-upload ZIP:")
    print(str(archive))
    print("[GAR COLLECT] Request ID: " + (selected_id or "NOT FOUND"))
    print("[GAR COLLECT] L4 E2E: " + ("PASS (event signature)" if checks["l4_full_e2e"] else "NOT PROVEN"))
    print("[GAR COLLECT] Files: " + str(len(list(outdir.iterdir()))))
    print("=" * 65)
    return outdir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--request-id", default="")
    args = parser.parse_args()
    try:
        collect(args.root, request_id=args.request_id)
        return 0
    except Exception as exc:
        print("[GAR COLLECT][ERROR] " + type(exc).__name__ + ": " + scrub(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
