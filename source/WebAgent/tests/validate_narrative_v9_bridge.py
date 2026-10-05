#!/usr/bin/env python3
"""Focused acceptance tests for the v9 natural-language decision bridge."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_core.narrative_bridge import (
    NARRATIVE_TOOL_FIELDS,
    NarrativeBridgeError,
    NarrativeDecisionBridge,
)
from agent_core.protocol_v9 import parse_v9_tool_transport
from WebAgent.protocol import WEBAGENT_PROTOCOL_BODY, WEBAGENT_PROTOCOL_VERSION


def expected(root: Path, *, round_id: int = 7) -> dict:
    return {
        "run_id": "RR-V9-TEST",
        "turn_id": round_id,
        "narrative_draft_root": str(root),
        "narrative_recovery_context": {
            "goal": "inspect the workspace",
            "progress": {
                "base_evaluation": "workspace is available",
                "total_steps": 2,
                "current_step": 1,
                "steps": [
                    {"step": 1, "desc": "inspect", "status": "IN_PROGRESS"},
                    {"step": 2, "desc": "report", "status": "PENDING"},
                ],
                "current_focus": "inspect files",
                "next_action": "run a command",
            },
        },
    }


def run() -> dict:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)

        action = NarrativeDecisionBridge.create(
            expected=expected(root),
            source_text="I should run a directory listing next.",
            root=root,
        )
        assert action.draft.pending_slot == "decision_kind"
        assert "exactly one token" in action.next_prompt()
        action.accept_reply("ACTION")
        assert action.draft.pending_slot == "tool"
        persisted_draft = json.loads(action.path.read_text(encoding="utf-8"))
        assert persisted_draft["decision_kind"] == "ACTION"
        assert persisted_draft["pending_slot"] == "tool"
        action.accept_reply("RUN_COMMAND")
        assert action.draft.pending_slot == "field:command"
        persisted_draft = json.loads(action.path.read_text(encoding="utf-8"))
        assert persisted_draft["tool"] == "run_command"
        assert persisted_draft["pending_slot"] == "field:command"
        action.accept_reply("Get-ChildItem -LiteralPath C:/workspace")
        assert action.ready
        persisted_draft = json.loads(action.path.read_text(encoding="utf-8"))
        assert persisted_draft["decision_kind"] == "ACTION"
        assert persisted_draft["tool"] == "run_command"
        assert persisted_draft["fields"] == {"command": "Get-ChildItem -LiteralPath C:/workspace"}
        canonical = action.canonical_response()
        calls, errors = parse_v9_tool_transport(canonical)
        assert not errors
        assert calls[1]["tool"] == "run_command"
        assert calls[1]["command"] == "Get-ChildItem -LiteralPath C:/workspace"
        assert calls[0]["action_id"].startswith("V9-PROGRESS-")
        assert calls[1]["action_id"].startswith("V9-ACTION-")
        assert calls[-1] == {"tool": "turn_commit", "action_count": 2}
        persisted = json.loads(action.path.read_text(encoding="utf-8"))
        assert persisted["state"] == "VALIDATED"
        assert persisted["source_response_sha256"]
        resumed = NarrativeDecisionBridge.create(
            expected=expected(root),
            source_text="I should run a directory listing next.",
            root=root,
        )
        assert resumed.ready
        assert resumed.canonical_response() == canonical
        assert NarrativeDecisionBridge.mark_terminal_result(
            action_id=calls[1]["action_id"], root=root, result="ok"
        )
        executed = json.loads(action.path.read_text(encoding="utf-8"))
        assert executed["state"] == "EXECUTED"
        assert executed["execution_result_sha256"]

        final = NarrativeDecisionBridge.create(
            expected=expected(root, round_id=8),
            source_text="盤點完成，沒有需要執行的本機動作。",
            root=root,
        )
        final.accept_reply("FINAL_RESPONSE")
        assert final.draft.pending_slot == "terminal_outcome"
        final.accept_reply("FAILED")
        final_calls, final_errors = parse_v9_tool_transport(final.canonical_response())
        assert not final_errors
        assert final_calls[1]["tool"] == "final_response"
        assert final_calls[1]["content"] == "盤點完成，沒有需要執行的本機動作。"
        assert final_calls[0]["current_step"] == final_calls[0]["total_steps"]
        assert final_calls[0]["outcome"] == "FAILED"

        refusal_expected = expected(root, round_id=14)
        refusal_expected["narrative_recovery_context"]["authorized_paths"] = [r"C:\workspace"]
        refusal = NarrativeDecisionBridge.create(
            expected=refusal_expected,
            source_text="我無法存取本機路徑，請上傳檔案。",
            root=root,
        )
        refusal_prompt = refusal.next_prompt()
        assert "V9_RUNTIME_CAPABILITY_RECOVERY" in refusal_prompt
        assert "list_directory" in refusal_prompt
        assert "Do not ask the user to upload" in refusal_prompt

        extended_expected = expected(root, round_id=10)
        extended_expected["narrative_recovery_context"]["progress"].update({
            "current_step": 2,
            "steps": [
                {"step": 1, "desc": "inspect", "status": "COMPLETED"},
                {"step": 2, "desc": "report", "status": "COMPLETED"},
            ],
        })
        extended = NarrativeDecisionBridge.create(
            expected=extended_expected,
            source_text="A newly discovered action is still required.",
            root=root,
        )
        extended.accept_reply("ACTION")
        extended.accept_reply("RUN_COMMAND")
        extended.accept_reply("Write-Output verify")
        extended_calls, extended_errors = parse_v9_tool_transport(
            extended.canonical_response()
        )
        assert not extended_errors
        assert extended_calls[0]["decision"] == "CONTINUE"
        assert extended_calls[0]["current_step"] < extended_calls[0]["total_steps"]

        typed = NarrativeDecisionBridge.create(
            expected=expected(root, round_id=12), source_text="remove a path", root=root,
        )
        typed.accept_reply("ACTION")
        typed.accept_reply("DELETE_PATH")
        assert typed.draft.pending_slot == "field:path"
        typed.accept_reply(r"C:\workspace\obsolete.txt")
        assert typed.draft.pending_slot == "field:reason"
        typed.accept_reply("\n")
        assert typed.draft.pending_slot == "field:reason"
        typed.accept_reply("user requested cleanup")
        assert typed.draft.pending_slot == "field:recursive"
        typed.accept_reply("true")
        assert typed.ready
        typed_calls, typed_errors = parse_v9_tool_transport(typed.canonical_response())
        assert not typed_errors
        assert typed_calls[1]["recursive"] is True

        directory = NarrativeDecisionBridge.create(
            expected=expected(root, round_id=13), source_text="list a directory", root=root,
        )
        assert NARRATIVE_TOOL_FIELDS["delete_path"] == ("path", "reason", "recursive")
        assert NARRATIVE_TOOL_FIELDS["list_directory"] == ("path",)
        directory.accept_reply("ACTION")
        directory.accept_reply("LIST_DIRECTORY")
        assert "string value" in directory.next_prompt()
        directory.accept_reply(r"C:\workspace")
        assert directory.ready
        assert directory.draft.fields == {"path": r"C:\workspace"}

        bounded = NarrativeDecisionBridge.create(
            expected=expected(root, round_id=9), source_text="unclear", root=root,
        )
        bounded.accept_reply("maybe this or that")
        try:
            bounded.accept_reply("still unclear")
        except NarrativeBridgeError:
            pass
        else:
            raise AssertionError("ambiguous slot must stop after the bounded retry")
        assert bounded.draft.state == "FAILED"

        legacy_collecting = NarrativeDecisionBridge.create(
            expected=expected(root, round_id=11),
            source_text="legacy collecting draft",
            root=root,
        )
        legacy_payload = json.loads(legacy_collecting.path.read_text(encoding="utf-8"))
        legacy_payload.pop("bridge_mode", None)
        legacy_payload["pending_slot"] = "decision_kind"
        legacy_payload["total_attempts"] = 1
        legacy_collecting.path.write_text(
            json.dumps(legacy_payload, ensure_ascii=False), encoding="utf-8"
        )
        migrated = NarrativeDecisionBridge.create(
            expected=expected(root, round_id=11),
            source_text="legacy collecting draft",
            root=root,
        )
        assert migrated.draft.pending_slot == "decision_kind"
        assert migrated.draft.total_attempts == 0

        raw_calls, raw_errors = parse_v9_tool_transport(json.dumps({
            "tool": "final_response", "content": "raw JSON accepted",
        }))
        assert not raw_errors
        assert raw_calls[0]["action_id"].startswith("V9-RUNTIME-")
        assert raw_calls[-1] == {"tool": "turn_commit", "action_count": 1}

        owned_calls, owned_errors = parse_v9_tool_transport(json.dumps([
            {"tool": "final_response", "action_id": "MODEL-OWNED", "content": "runtime owns controls"},
            {"tool": "turn_commit", "action_count": 999},
        ]))
        assert not owned_errors
        assert owned_calls[0]["action_id"].startswith("V9-RUNTIME-")
        assert owned_calls[-1] == {"tool": "turn_commit", "action_count": 1}

        fenced_calls, fenced_errors = parse_v9_tool_transport(
            "```json\n" + json.dumps({"tool": "final_response", "content": "json fence"}) + "\n```"
        )
        assert not fenced_errors and fenced_calls[0]["content"] == "json fence"

        prose_calls, prose_errors = parse_v9_tool_transport(
            "Decision follows: " + json.dumps({"tool": "final_response", "content": "one object"})
        )
        assert not prose_calls and prose_errors

        ambiguous_calls, ambiguous_errors = parse_v9_tool_transport(
            json.dumps({"tool": "final_response", "content": "one"})
            + "\n"
            + json.dumps({"tool": "final_response", "content": "two"})
        )
        assert not ambiguous_calls and ambiguous_errors

        runtime_owned_calls, runtime_owned_errors = parse_v9_tool_transport(json.dumps({
            "tool": "run_command",
            "command": "Write-Output unsafe",
            "request_id": "MODEL-MUST-NOT-OWN-THIS",
        }))
        assert not runtime_owned_calls and runtime_owned_errors

        duplicate_calls, duplicate_errors = parse_v9_tool_transport(
            '{"tool":"final_response","content":"one","content":"two"}'
        )
        assert not duplicate_calls and duplicate_errors

        assert WEBAGENT_PROTOCOL_VERSION == 9
        assert "Every normal reply must contain only fenced" in WEBAGENT_PROTOCOL_BODY
        assert "Protocol v9 accepts either" not in WEBAGENT_PROTOCOL_BODY
        source_root = Path(__file__).resolve().parents[2]
        ownership_source = (
            source_root / "agent_core" / "request_ownership.py"
        ).read_text(encoding="utf-8")
        loop_source = (
            source_root / "WebAgent" / "protocol_loop.py"
        ).read_text(encoding="utf-8")
        assert "若輸出自然語言" not in ownership_source
        assert "只能輸出 compact v9 smartagent_tool blocks" in ownership_source
        assert "不得輸出 blocks 以外的自然語言" in loop_source

    print("NARRATIVE_V9_BRIDGE_OK")
    return {
        "one_bounded_slot_at_a_time": True,
        "confirmed_slots_persisted_immediately": True,
        "typed_field_validation_and_retry": True,
        "runtime_canonicalization": True,
        "tolerant_exclusive_json_recovery": True,
        "tolerant_json_runtime_owns_ids_and_commit": True,
        "prose_embedded_json_not_executable": True,
        "runtime_owned_fields_still_rejected": True,
        "duplicate_json_keys_still_rejected": True,
        "ambiguous_json_fail_closed": True,
        "normal_prompt_does_not_advertise_narrative_fallback": True,
        "natural_final_response": True,
        "terminal_outcome_collected_explicitly": True,
        "false_local_access_refusal_gets_runtime_capabilities": True,
        "bounded_failure": True,
        "persistent_audit_record": True,
        "collecting_full_envelope_draft_migrated": True,
        "restart_resume_and_terminal_state": True,
    }


if __name__ == "__main__":
    run()
