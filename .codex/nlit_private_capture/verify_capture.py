"""PRIVATE-only, read-only NLIT Planner delivery evidence verifier.

Usage (from isolated installation):
  python .codex/nlit_private_capture/verify_capture.py --private-root PRIVATE_ROOT

Only aggregate classification (no paths, prompts, replies or identifiers)
is printed. The source binary payloads stay on the local encrypted/ACL-protected
disk. Returning PASS here attests production hooks saw a submit/user-turn/
response sequence, NOT Predicate/Task success nor the old 17 original cases.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def analyze_round(directory: Path) -> dict:
    status = "PLANNER_DELIVERY_NOT_OBSERVED"
    checks: list[str] = []
    try:
        file = directory / "events.jsonl"
        evs = [json.loads(s) for s in file.read_text(
            encoding="utf-8").splitlines() if s.strip()]
        if not evs:
            return {"status": status, "failed_checks": ["NO_EVENTS"]}
        identities = {
            (e.get("request_id"), e.get("task_id"), e.get("task_epoch"),
             e.get("round"), e.get("result_id")) for e in evs
        }
        if len(identities) != 1:
            checks.append("IDENTITY_MISMATCH")
        names = {e.get("kind") for e in evs}
        for req in ("PLANNER_TRANSPORT_ENTERED",
                    "PREPARED_NOT_SUBMITTED",
                    "COMPOSER_WRITER_VERIFIED",
                    "SEND_READY_VERIFIED",
                    "SUBMIT_CLICK_RETURNED_NOT_DELIVERY_PROOF",
                    "USER_TURN_CONFIRMED_BY_PRODUCTION_SCRAPER",
                    "MODEL_RESPONSE_RETURNED"):
            if req not in names:
                checks.append("MISSING_" + req)
        for filename in ("composer_input_utf8.bin", "assistant_utf8.bin"):
            records = [e for e in evs if e.get("kind") == filename + "_CAPTURED"]
            try:
                payload = (directory / filename).read_bytes()
            except OSError:
                checks.append("MISSING_" + filename)
                continue
            if len(records) != 1 or records[0].get("sha256") != _sha(payload):
                checks.append("FILE_HASH_MISMATCH_" + filename)
            elif records[0].get("bytes") != len(payload):
                checks.append("FILE_BYTE_COUNT_MISMATCH_" + filename)
        if not checks:
            response = next((e for e in evs
                             if e.get("kind") == "MODEL_RESPONSE_RETURNED"), {})
            if (not response.get("user_turn_confirmed") or
                not response.get("capture_complete")):
                checks.append("RESPONSE_NOT_CORRELATED_OR_CAPTURE_FAILED")
        # For post-tool messages the completed result envelope must appear in
        # the actual composer input; a model response alone is not proof.
        first = evs[0]
        if int(first.get("round") or 0) >= 2:
            result_id = str(first.get("result_id") or "")
            try:
                prompt = (directory / "composer_input_utf8.bin").read_bytes()
                if not result_id or ("RESULT_ID=" + result_id).encode() not in prompt:
                    checks.append("POST_TOOL_RESULT_ID_NOT_IN_EXACT_INPUT")
                if b"[WEBAGENT_TOOL_RESULTS]" not in prompt:
                    checks.append("POST_TOOL_RESULTS_ENVELOPE_MISSING")
            except OSError:
                checks.append("POST_TOOL_INPUT_FILE_MISSING")
        if not checks:
            status = "MODEL_RESPONSE_CORRELATED"
    except Exception:
        checks.append("TRACE_PARSE_OR_IO_ERROR")
    return {"status": status, "failed_checks": sorted(set(checks))}


def main() -> int:
    cli = argparse.ArgumentParser()
    cli.add_argument("--private-root", type=Path, required=True)
    args = cli.parse_args()
    root = args.private_root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit("PRIVATE_ROOT_NOT_DIRECTORY")
    counts: dict[str, int] = {}
    failed_checks: dict[str, int] = {}
    next_turns = 0
    for d in sorted(root.glob("request-*/round-*")):
        if not d.is_dir():
            continue
        if d.name >= "round-0002":
            next_turns += 1
        outcome = analyze_round(d)
        counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1
        for check in outcome["failed_checks"]:
            failed_checks[check] = failed_checks.get(check, 0) + 1
    print(json.dumps({
        "schema": "SMARTAGENT_NLIT_CAPTURE_AUDIT_V1",
        "total_capture_rounds": sum(counts.values()),
        "post_tool_rounds_inspected": next_turns,
        "round_status_counts": counts,
        "negative_gate_counts": failed_checks,
        "original_historical_case_pass_inferred": False,
        "semantic_predicate_pass_inferred": False,
    }, sort_keys=True))
    return 0 if next_turns and counts.get("MODEL_RESPONSE_CORRELATED") else 2


if __name__ == "__main__":
    raise SystemExit(main())
