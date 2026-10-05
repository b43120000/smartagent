#!/usr/bin/env python3
"""Validate that oversized tool output stays local unless attachment use is justified."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = APP_ROOT / "source"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_core.result_exchange import prepare_tool_result


class FakeAgent:
    def __init__(self, workspace: Path):
        self.workspace_root = workspace
        self.current_run_id = "RUN-RESULT-POLICY"
        self.current_request_id = "REQUEST-RESULT-POLICY"
        self.current_task_id = "TASK-RESULT-POLICY"
        self.queued: list[str] = []

    def queue_attachments(self, paths: list[str]) -> str:
        self.queued.extend(paths)
        return "QUEUED"


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="smartagent-result-policy-") as temp:
        root = Path(temp).resolve()
        agent = FakeAgent(root)
        large_build_log = (
            "[COMMAND_RESULT]\ncommand: gradlew clean assembleDebug\n"
            "exit_code: 0\nstdout:\n" + ("compile output line\n" * 3000)
        )

        automatic = prepare_tool_result(
            {"tool": "run_command", "action_id": "ACT-AUTO"},
            large_build_log,
            agent,
        )
        assert "[SMARTAGENT_RESULT_LOCAL_REF]" in automatic
        assert "reason=RUN_COMMAND_OUTPUT_LOCAL_ONLY" in automatic
        assert "未上傳" in automatic
        assert agent.queued == []
        assert list((root / ".agents" / "results").glob("*.txt"))

        unjustified = prepare_tool_result(
            {
                "tool": "run_command",
                "action_id": "ACT-NO-PURPOSE",
                "result_transport": "ATTACHMENT",
                "full_result_required": True,
            },
            large_build_log,
            agent,
        )
        assert "reason=ATTACHMENT_JUSTIFICATION_REQUIRED" in unjustified
        assert agent.queued == []

        explicit = prepare_tool_result(
            {
                "tool": "run_command",
                "action_id": "ACT-EXPLICIT",
                "result_transport": "ATTACHMENT",
                "full_result_required": True,
                "result_purpose": "Exact raw compiler trace is required for offline parser compatibility diagnosis.",
            },
            large_build_log,
            agent,
        )
        assert "[SMARTAGENT_RESULT_ATTACHMENT]" in explicit
        assert "reason=INLINE_BUDGET_EXCEEDED" in explicit
        assert "attachment_policy=EXPLICIT_FULL_RESULT" in explicit
        assert "result_purpose=Exact raw compiler trace" in explicit
        assert len(agent.queued) == 1
        assert Path(agent.queued[0]).is_file()

        forced = prepare_tool_result(
            {"tool": "round_results", "action_id": "ACT-RUNTIME-FORCE"},
            large_build_log,
            agent,
            force_attachment=True,
        )
        assert "attachment_policy=RUNTIME_FORCE" in forced
        assert len(agent.queued) == 2

        # AUTO is local-only for other oversized tool output too. Attachment
        # transfer is an explicit exception, never a consequence of size alone.
        other = prepare_tool_result(
            {"tool": "read_file", "action_id": "ACT-OTHER"},
            "x" * 40000,
            agent,
        )
        assert "reason=AUTO_LOCAL_ONLY" in other
        assert len(agent.queued) == 2

        small = prepare_tool_result(
            {"tool": "run_command", "action_id": "ACT-SMALL"},
            "exit_code: 0\nVERIFICATION_STATUS: PASS",
            agent,
        )
        assert small == "exit_code: 0\nVERIFICATION_STATUS: PASS"

    print("RESULT_EXCHANGE_UPLOAD_POLICY_OK")


if __name__ == "__main__":
    run()
