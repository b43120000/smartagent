"""Closed-environment Goal 1 triage: print bounded summaries, never raw logs.

Example:
  python -B source/WebAgent/tests/goal1_live_triage.py --case 2A-L03 --interface WEB_COPILOT --source-sha NOT_ATTESTED --symptom "Composer 初始化後超時" --log "C:\private\local.jsonl"
"""
from __future__ import annotations
import argparse
import collections
import hashlib
import json
import re
from pathlib import Path

SIGNALS = (
    "WRITER_TIMEOUT", "QUARANTINED", "BOOTSTRAP", "WEBACK", "TURN_COMMIT",
    "ACK_REJECTED", "ACK_ACCEPTED", "PROGRESS_REJECTED",
    "TERMINAL_ACCEPTED", "TASK_COMPLETED", "TASK_PAUSED",
    "OPEN_WRITE_TRANSACTION", "DELIVERY_FAILED", "DELIVERED",
    "SOURCE_ATTESTATION", "PROTOCOL_INTERRUPTED", "MODEL_TIMEOUT",
    "ACTION_REJECTED", "ROUND_STARTED", "RESPONSE_RECEIVED",
    "GAR_TRANSPORT_INTEGRITY", "GAR_RECOVERY_DECISION",
    "GAR_SAFE_PAUSE", "GAR_CHECKPOINT_PERSISTED",
    "GAR_MUTATION_REPLAY_BLOCKED",
    "GAR_VERIFICATION_REQUIREMENT_CLASSIFIED",
)
_SIGNAL_RE = re.compile("|".join(re.escape(token) for token in SIGNALS), re.I)
# Fixed diagnostic vocabulary only; freeform model text and raw paths are
# never copied out of the closed machine.
REASONS = (
    "WRITER_TIMEOUT", "QUARANTINED", "INVALID_PROGRESS_PAYLOAD",
    "RUNTIME_EVIDENCE_REF_UNKNOWN", "TERMINAL_SUCCESS_VERIFICATION_MISSING",
    "DECLARED_NEXT_ACTION_MISSING", "MISSING_TURN_COMMIT",
    "FINAL_RESPONSE_NOT_EXCLUSIVE", "UNRESOLVED_ACTION_REPAIRS",
    "REQUIRED_TRANSITION_ACTION_MISSING", "ACTIVE_ACTION_NOT_COMPLETE",
    "SOURCE_LOAD_ATTESTATION_UNAVAILABLE", "BOUND_PAGE_COUNT_0",
    "REQUEST_ELAPSED_SOFT_DEADLINE", "TERMINAL_REFERENCE_IDENTITY_NOT_SCOPED",
    "DELIVERY_OUTCOME_UNKNOWN", "SCOPE_REJECTED",
    "STALE_TRANSPORT_READBACK", "TRANSPORT_UNCERTAIN",
    "FRESHNESS_UNATTESTED", "GAR_SNAPSHOT_UNVERIFIED",
    "GAR_PENDING_OR_BLOCKED_OPERATION", "GAR_PROGRESS_REBASE",
    "GAR_BOUNDED_REPLAN", "GAR_FRESH_CONTEXT_UNAVAILABLE",
    "GAR_SHARED_BUDGET_EXHAUSTED", "GAR_GOAL_PROMPT_UNSAFE",
    "GAR_POTENTIAL_MUTATION_REPLAY",
)
_REASON_RE = re.compile("|".join(re.escape(token) for token in REASONS), re.I)
_SECRET_RE = re.compile(
    r"(?i)(?:api[_ -]?key|authorization|bearer|password|token|cookie|"
    r"secret|chat[_ -]?id)\s*[=:]\s*\S+"
)
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/home/|/Users/|\\\\)[^\s,，；;]{3,}")
_URL_RE = re.compile(r"https?://[^\s]+", re.I)
_SHA = re.compile(r"^[0-9a-f]{40}$", re.I)
_CASE = re.compile(r"^(?:2A-L0[1-4]|5A-L0[1-7]|5B-(?:FP0[1-8]|L0[1-8]))$")


def safe_symptom(text: str) -> str:
    value = str(text or "").replace("\r", " ").replace("\n", " ")
    value = _SECRET_RE.sub("[REDACTED_SECRET]", value)
    value = _PATH_RE.sub("[REDACTED_LOCAL_PATH]", value)
    value = _URL_RE.sub("[REDACTED_URL]", value)
    return value[:500] or "未提供自然語言症狀"


def observe_log(path: Path) -> dict:
    """Return only signal counts, not user data, paths, IDs or raw lines."""
    if not path.is_file():
        return {"error": "LOG_NOT_FOUND"}
    if path.stat().st_size > 64 * 1024 * 1024:
        return {"error": "LOG_OVER_BUDGET_64M"}
    digest = hashlib.sha256()
    count = 0
    hits = collections.Counter()
    reason_hits = collections.Counter()
    last = "NONE"
    last_reason = "NONE"
    last_reason_line = 0
    with path.open("rb") as handle:
        for raw in handle:
            digest.update(raw)
            count += 1
            if len(raw) > 131072:
                hits["OVERSIZED_LOG_LINE"] += 1
                continue
            line = raw.decode("utf-8", errors="replace")
            for match in _SIGNAL_RE.finditer(line):
                signal = match.group(0).upper()
                hits[signal] += 1
                last = signal
            for match in _REASON_RE.finditer(line):
                reason = match.group(0).upper()
                reason_hits[reason] += 1
                last_reason = reason
                last_reason_line = count
    return {
        "log_sha256": digest.hexdigest(),
        "log_bytes": path.stat().st_size,
        "log_lines": count,
        "last_known_signal": last,
        "signal_counts": dict(sorted(hits.items())),
        "last_known_reason": last_reason,
        "last_reason_line": last_reason_line,
        "reason_counts": dict(sorted(reason_hits.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Goal1 offline Live triage")
    parser.add_argument("--case", required=True)
    parser.add_argument("--interface", required=True,
                        choices=("TELEGRAM", "LOCAL_TEST", "WEB_COPILOT"))
    parser.add_argument("--source-sha", default="NOT_ATTESTED")
    parser.add_argument("--symptom", default="")
    parser.add_argument("--runtime-state", default="NOT_OBSERVED")
    parser.add_argument("--receipt", default="NOT_OBSERVED",
                        choices=("NOT_OBSERVED", "ACCEPTED", "REJECTED", "MISSING"))
    parser.add_argument("--delivery", default="NOT_OBSERVED",
                        choices=("NOT_OBSERVED", "DELIVERED", "FAILED", "PENDING", "UNKNOWN"))
    parser.add_argument("--log", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if not _CASE.fullmatch(args.case):
        parser.error("unrecognized case ID")
    if args.source_sha != "NOT_ATTESTED" and not _SHA.fullmatch(args.source_sha):
        parser.error("source SHA must be 40 hex characters or NOT_ATTESTED")
    state = args.runtime_state.strip().upper()
    if state not in {"NOT_OBSERVED", "PAUSED", "INTERRUPTED", "COMPLETED",
                     "RUNNING", "UNKNOWN", "FAILED", "BLOCKED"}:
        parser.error("runtime state must be known enum")
    evidence = observe_log(args.log) if args.log is not None else {
        "log_evidence": "NOT_SUPPLIED"
    }
    lines = [
        "SmartAgent Goal1 Live 診斷摘要（無原始 log）",
        "case=" + args.case, "origin=" + args.interface,
        "loaded_source_sha=" + args.source_sha,
        "runtime_state=" + state,
        "terminal_receipt=" + args.receipt,
        "delivery=" + args.delivery,
        "使用者觀察到的中斷位置：" + safe_symptom(args.symptom),
        "有限 log 訊號：" + json.dumps(evidence, ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":")),
        "NOT_OBSERVED/NOT_ATTESTED 不代表 PASS；原始 log 保留本機。"
    ]
    report = "\n".join(lines) + "\n"
    if args.output:
        if args.output.exists():
            parser.error("output file exists; historical evidence must not be overwritten")
        args.output.write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
