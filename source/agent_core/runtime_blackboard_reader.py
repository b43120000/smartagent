"""Git54: read-only, task-bound pagination of Executor Blackboard metadata."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .executor_run_blackboard import read_executor_event, read_executor_run
from .executor_blackboard import read_executor_terminal

SECTIONS = frozenset({"event", "events_index", "metadata", "terminal"})
MAX_SLICE_CHARS = 2400


def read_runtime_blackboard(
    root: str | Path, *, task_id: str, request_id: str, task_epoch: str,
    section: str, sequence: int = 0, offset: int = 0, max_chars: int = 1800,
) -> dict:
    """A projection is never allowed to erase the archived primary event."""
    if (section not in SECTIONS or type(sequence) is not int or sequence < 0
            or type(offset) is not int or offset < 0
            or type(max_chars) is not int or not 1 <= max_chars <= MAX_SLICE_CHARS):
        raise ValueError("RUNTIME_BLACKBOARD_READ_ARGUMENT_INVALID")
    board = read_executor_run(root, task_id=task_id,
                              request_id=request_id, task_epoch=task_epoch)
    if board is None:
        raise ValueError("RUNTIME_BLACKBOARD_REQUEST_BINDING_MISSING")
    if section == "event":
        if sequence == 0:
            raise ValueError("RUNTIME_BLACKBOARD_EVENT_SEQUENCE_REQUIRED")
        obj = read_executor_event(root, task_id=task_id, request_id=request_id,
                                  task_epoch=task_epoch, sequence=sequence)
    elif section == "events_index":
        obj = {
            "event_count_total": board.get("event_count_total", len(board["events"])),
            "active_sequences": [e["sequence"] for e in board["events"]],
            "archive_chunks": list(board.get("archive_chunks", ())),
            "read_event_tool": "read_runtime_blackboard",
        }
    elif section == "metadata":
        obj = {k: v for k, v in board.items() if k != "events"}
    else:
        receipt = read_executor_terminal(root, task_id=task_id,
                                         request_id=request_id,
                                         task_epoch=task_epoch)
        obj = {"executor_run_terminal": board.get("terminal"),
               "executor_terminal": receipt}
    content = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    start = min(offset, len(content))
    stop = min(start + max_chars, len(content))
    return {
        "schema": "SMARTAGENT_RUNTIME_BLACKBOARD_READ_V1",
        "section": section, "sequence": sequence,
        "content": content[start:stop],
        "offset": start, "next_offset": stop, "total_chars": len(content),
        "complete": stop == len(content),
        "source_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "task_bound": True, "read_only": True,
        "terminal_authority_unchanged": True,
    }


__all__ = ["read_runtime_blackboard", "SECTIONS", "MAX_SLICE_CHARS"]
