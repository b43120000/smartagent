"""Read-only historical replay of a stored task through the real Comparator."""
import argparse
from collections import Counter
import json
from pathlib import Path
from types import SimpleNamespace

from agent_core.runtime_finish_comparator import compare_runtime_finish
from agent_core.executor_run_blackboard import read_executor_event


def replay(root: Path, task_id: str, expected_checks: list[dict] | None = None) -> dict:
    runtime = root / "localdata" / "runtime"

    def load(relative):
        return json.loads((runtime / relative).read_text(encoding="utf-8-sig"))

    task = load("remote_tasks.json")["tasks"][task_id]
    saved = load("task_continuations.json")["records"][task_id]
    board = load(f"executor_run_blackboard/{task_id}.json")
    ledger = task["result_ledger"]["final"]["action_result_ledger"]
    loop = SimpleNamespace(
        task_id=task_id, run_id=task["request_id"], task_epoch=task["task_epoch"],
        action_result_ledger=ledger,
        _canonical_action_proposals={
            key: saved["actions"][key]["action"] for key in ledger
        },
    )
    # Production stores the Finish input digests. Build explicit replay
    # claims from the existing receipt scalars; never change those receipts.
    claims, checks = [], []
    events = board["events"]
    count = board["event_count_total"]
    if count != len(events):
        if not len(events) <= count <= 10000:
            raise ValueError("Invalid or unbounded Blackboard event history")
        events = [
            read_executor_event(root, task_id=task_id, request_id=loop.run_id,
                                task_epoch=loop.task_epoch, sequence=index)
            for index in range(1, count + 1)
        ]
    for event in events:
        if event["kind"] != "TOOL_RESULT":
            continue
        ref = event["runtimeEvidence"]["evidence_ref"]
        receipt = load(f"evidence_store/{task_id}/{ref}.json")["receipt"]
        fields = {"status": receipt["status"]}
        if receipt["status"] == "SUCCEEDED" and receipt["executed"] is True:
            data = receipt["data"]
            for name in ("stdout", "verification_status", "file_saved"):
                value = data.get(name)
                if type(value) in (bool, int, str) and value != "" and len(str(value)) <= 256:
                    field = "data." + name
                    fields[field] = value
                    checks.append({
                        "evidence_ref": ref, "criterion": f"Replay observation {ref} {field}",
                        "field": field, "operator": "eq", "expected": value,
                    })
        claims.append({"evidence_ref": ref, "fields": fields})
    if expected_checks is not None:
        from agent_core.runtime_finish_comparator import _lookup
        by_ref = {claim["evidence_ref"]: claim["fields"] for claim in claims}
        for check in expected_checks:
            ref, field = check["evidence_ref"], check["field"]
            receipt = load(f"evidence_store/{task_id}/{ref}.json")["receipt"]
            present, value = _lookup(receipt, field)
            if present:
                by_ref[ref][field] = value
        checks = expected_checks
    result = compare_runtime_finish(loop, root, task["request"], claims, checks)
    return {
        "task_id": task_id, "claim_source": "RECONSTRUCTED_FROM_RETAINED_RECEIPTS",
        "goal_check_source": ("EXPLICIT_REPLAY_EXPECTATIONS" if expected_checks is not None
                              else "RECONSTRUCTED_FROM_RETAINED_RECEIPTS"),
        "goal_checks_count": len(checks),
        "statuses": Counter(claim["fields"]["status"] for claim in claims),
        "historical_reason": board["terminal"]["finish_comparison"]["reason_code"],
        "replay": {key: value for key, value in result.items() if key not in ("evidence_refs", "tool_results")},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--goal-checks", type=Path,
                        help="Optional JSON array of independently specified replay predicates")
    parser.add_argument("task_id")
    args = parser.parse_args()
    checks = json.loads(args.goal_checks.read_text(encoding="utf-8-sig")) if args.goal_checks else None
    print(json.dumps(replay(args.root, args.task_id, checks), ensure_ascii=False, indent=2))
