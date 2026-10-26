#!/usr/bin/env python3
"""IT19 Worker loaded-code attestation gate: safe native offline regression.

This test imports the real core tools and verifies in-process code identity;
it does not start a Worker, browser, Telegram sender or live transaction.
"""
from __future__ import annotations

import copy
from pathlib import Path

from agent_core import tools as runtime_tools
from agent_core.source_attestation import (
    FUNCTIONS, capture, require_writer_loaded_source_attestation,
)


def require_rejected(record, label: str) -> None:
    try:
        require_writer_loaded_source_attestation(record)
    except RuntimeError as exc:
        assert str(exc) == "writer_loaded_source_attestation_failed", (label, exc)
    else:
        raise AssertionError("unexpected writer attestation accept: " + label)


def main() -> None:
    names = FUNCTIONS["agent_core.tools"]
    assert names == ("_task_scoped_chunked_write_manager", "_execute_tool_impl")
    assert runtime_tools.__file__
    assert Path(runtime_tools.__file__).resolve().is_file()
    snapshot = capture(role="telegram_webagent_worker", manifest_verified=True)
    passed = require_writer_loaded_source_attestation(snapshot)
    assert passed["status"] == "PASS"
    entry = snapshot["modules"]["agent_core.tools"]
    assert entry["loaded"] is True
    assert entry["source_path"] == "source/agent_core/tools.py"
    for name in names:
        row = entry["code_identity_comparisons"][name]
        assert all(row[k] is True for k in (
            "code_objects_equal", "filename_verified",
            "first_line_equal", "qualname_equal"
        ))
        assert entry["live_matches_recompiled_disk"][name] is True

    # A valid disk Git HEAD or source hash alone must not authorize tool use.
    for label, change in [
        ("wrong_manifest", lambda x: x.update(manifest_verified_before_capture=False)),
        ("not_loaded", lambda x: x["modules"]["agent_core.tools"].update(loaded=False)),
        ("wrong_path", lambda x: x["modules"]["agent_core.tools"].update(source_path="foreign.py")),
        ("missing_disk_hash", lambda x: x["modules"]["agent_core.tools"].update(disk_sha256_at_capture=None)),
        ("missing_module", lambda x: x["modules"].pop("agent_core.tools")),
    ]:
        sample = copy.deepcopy(snapshot)
        change(sample)
        require_rejected(sample, label)

    for name in names:
        sample = copy.deepcopy(snapshot)
        sample["modules"]["agent_core.tools"]["live_matches_recompiled_disk"][name] = False
        require_rejected(sample, name + ":loaded-code-mismatch")
        sample = copy.deepcopy(snapshot)
        sample["modules"]["agent_core.tools"]["live_matches_recompiled_disk"].pop(name)
        require_rejected(sample, name + ":missing-proof")

    print("IT19_WORKER_WRITER_SOURCE_ATTESTATION_NATIVE_PASS")


if __name__ == "__main__":
    main()
