"""Shared runtime sequencing for receiver-bound project-sync transactions."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Callable, Mapping

from .attachment_staging import stage_attachments
from .project_sync_protocol import (
    ProjectSyncAckIncompleteError, ProjectSyncProtocol, ProjectSyncProtocolError,
    build_ack_repair_prompt,
)


ProjectSyncTransport = Callable[[str, list[str]], str | Mapping[str, object]]
MAX_SCHEMA_REPAIRS = 2
MAX_INCOMPLETE_REUPLOADS = 2


def _remove_dom_linebreak_before_json_quote(text: str) -> tuple[str, bool]:
    """Remove only raw CR/LF inserted at the end of a JSON string by DOM text.

    ChatGPT may render a URL as a link and expose a line break immediately
    before the closing JSON quote through ``inner_text``.  That byte is invalid
    JSON transport.  This normalization cannot add fields or change non-layout
    characters, and embedded/newline content elsewhere remains rejected.
    """
    source = str(text or "")
    output: list[str] = []
    in_string = False
    escaped = False
    changed = False
    length = len(source)
    index = 0
    while index < length:
        character = source[index]
        if not in_string:
            output.append(character)
            if character == '"':
                in_string = True
                escaped = False
            index += 1
            continue

        if escaped:
            output.append(character)
            escaped = False
            index += 1
            continue
        if character == "\\":
            output.append(character)
            escaped = True
            index += 1
            continue
        if character == '"':
            output.append(character)
            in_string = False
            index += 1
            continue
        if character in "\r\n":
            lookahead = index
            while lookahead < length and source[lookahead] in "\r\n":
                lookahead += 1
            if lookahead < length and source[lookahead] == '"':
                changed = True
                index = lookahead
                continue
        output.append(character)
        index += 1
    return "".join(output), changed


def parse_project_sync_ack(reply: str | Mapping[str, object]) -> dict:
    if isinstance(reply, Mapping):
        value = dict(reply)
        if value.get("type") == "PROJECT_SYNC_BATCH_ACK": return value
        raise ProjectSyncProtocolError("project_sync_ack_not_found")
    text = str(reply or ""); decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{": continue
        candidate = text[index:]
        variants = [candidate]
        normalized, changed = _remove_dom_linebreak_before_json_quote(candidate)
        if changed:
            variants.append(normalized)
        for variant in variants:
            try: value, _end = decoder.raw_decode(variant)
            except json.JSONDecodeError: continue
            if isinstance(value, dict) and value.get("type") == "PROJECT_SYNC_BATCH_ACK": return value
    raise ProjectSyncProtocolError("project_sync_ack_not_found")


def _aliases(
    staged: list[object], bundle_indexes: list[int], receipt_tokens: list[str]
) -> list[dict]:
    """Describe the files selected for one concrete browser send attempt."""
    if len(staged) != len(bundle_indexes) or len(staged) != len(receipt_tokens):
        raise ProjectSyncProtocolError("project_sync_alias_count_mismatch")
    return [
        {
            "bundle_index": index,
            "source_path": item.source_path,
            "upload_name": item.upload_name,
            "runtime_sha256": item.sha256,
            "receipt_token": receipt_token,
        }
        for index, item, receipt_token in zip(
            bundle_indexes, staged, receipt_tokens
        )
    ]


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_staged_by_index(staged: list[object], bundle_indexes: list[int], expected_hashes: list[str]) -> dict:
    """Fail closed if staging deduplicated or altered any planned attachment."""
    if len(staged) != len(bundle_indexes):
        raise ProjectSyncProtocolError("project_sync_staged_attachment_count_mismatch")
    actual_hashes = [str(item.sha256) for item in staged]
    if all(len(str(value)) == 64 for value in expected_hashes) and actual_hashes != list(expected_hashes):
        raise ProjectSyncProtocolError("project_sync_staged_attachment_hash_mismatch")
    return dict(zip(bundle_indexes, staged))


def _verify_selected_staged(staged: list[object]) -> None:
    if any(_sha256(str(item.staged_path)) != str(item.sha256) for item in staged):
        raise ProjectSyncProtocolError("project_sync_staged_attachment_changed")


def run_project_sync_transaction(
    workspace: str | Path, plan: Mapping[str, object], send_to_webgpt: ProjectSyncTransport, *,
    interface_name: str, conversation_id: str, session_id: str,
    event_sink: Callable[..., None] | None = None, request_id: str = "",
) -> dict:
    """Upload every batch with a receiver-bound, attempt-bound ACK.

    Malformed replies receive text-only repairs.  A receiver-authenticated false ACK
    instead re-uploads only diagnosed missing/unreadable bundles (or the whole batch
    when its diagnosis is unusable), with an independent bounded retry budget.
    """
    binding = {"interface_name": interface_name, "conversation_id": conversation_id, "session_id": session_id}
    protocol = ProjectSyncProtocol(workspace, plan, receiver_binding=binding, request_id=request_id)
    if protocol.state.get("status") == "PROJECT_SYNC_READY" and int(plan.get("batch_count", 0)) == 0:
        if event_sink is not None:
            event_sink("project_sync_no_changes", sync_id=plan["sync_id"], snapshot_id=plan["snapshot_id"], request_id=request_id)
    while protocol.state.get("status") != "PROJECT_SYNC_READY":
        notification = protocol.next_notification(); payload = notification["payload"]
        batch_index, batch_count = int(payload["batch_index"]), int(payload["batch_count"])
        print(f"[Project Sync/{interface_name}] batch {batch_index}/{batch_count}", flush=True)
        if event_sink is not None: event_sink("project_sync_batch_started", sync_id=plan["sync_id"], batch_index=batch_index, batch_count=batch_count, request_id=request_id)
        all_indexes = list(payload["bundle_indexes"])
        all_paths = list(notification["attachments"])
        batch_plan = dict(plan["batches"][batch_index - 1])
        all_hashes = list(batch_plan.get("bundle_hashes", []))
        all_receipts = list(batch_plan.get("receipt_tokens", []))
        if (
            len(all_paths) != len(all_indexes)
            or len(all_hashes) != len(all_indexes)
            or len(all_receipts) != len(all_indexes)
        ):
            raise ProjectSyncProtocolError("project_sync_batch_attachment_mapping_mismatch")
        source_by_index = dict(zip(all_indexes, all_paths))
        hash_by_index = dict(zip(all_indexes, all_hashes))
        receipt_by_index = dict(zip(all_indexes, all_receipts))
        schema_repairs = incomplete_reuploads = 0; logical_attempt = 0; current_kind = "initial"
        current_prompt = str(notification["prompt"]); selected_indexes = list(notification["payload"]["reupload_bundle_indexes"])
        send_attempt_id = str(notification["payload"]["send_attempt_id"])

        while True:
            # A selective re-upload must not reuse the browser-visible filename
            # from the earlier send.  ChatGPT renames an uploaded duplicate to
            # ``name(1).ext``; that breaks both the composer READY gate and the
            # attachment alias contract.  Stage fresh, attempt-scoped copies so
            # every send has a unique name while bundle index and content hash
            # remain stable.
            selected_paths = [source_by_index[index] for index in selected_indexes]
            selected_hashes = [hash_by_index[index] for index in selected_indexes]
            selected_receipts = [receipt_by_index[index] for index in selected_indexes]
            selected_staged = stage_attachments(
                workspace,
                conversation_id,
                session_id,
                f"{plan['sync_id']}-batch-{batch_index}-{send_attempt_id}",
                selected_paths,
                name_tokens=[
                    f"B{index:06d}-{receipt}"
                    for index, receipt in zip(selected_indexes, selected_receipts)
                ],
            ) if selected_indexes else []
            staged_by_index = _validated_staged_by_index(
                selected_staged, selected_indexes, selected_hashes
            )
            selected_staged = [staged_by_index[index] for index in selected_indexes]
            _verify_selected_staged(selected_staged)
            aliases = _aliases(selected_staged, selected_indexes, selected_receipts)
            prompt = current_prompt
            protocol.record_upload_attempt(batch_index, send_attempt_id, current_kind, selected_indexes, aliases)
            try:
                reply = send_to_webgpt(prompt, [item.staged_path for item in selected_staged])
            except Exception as exc:
                protocol.record_ack_attempt(batch_index, logical_attempt, "", validation_error=f"project_sync_transport_failed:{type(exc).__name__}", repair_kind=current_kind, send_attempt_id=send_attempt_id)
                raise
            parsed = None
            try:
                parsed = parse_project_sync_ack(reply)
                state = protocol.accept_ack(parsed, send_attempt_id=send_attempt_id)
                protocol.record_ack_attempt(batch_index, logical_attempt, reply, accepted=True, repair_kind=current_kind, send_attempt_id=send_attempt_id)
                if current_kind != "initial" and event_sink is not None: event_sink("project_sync_ack_recovery_succeeded", sync_id=plan["sync_id"], batch_index=batch_index, attempt=logical_attempt, recovery_kind=current_kind, request_id=request_id)
                break
            except ProjectSyncAckIncompleteError as exc:
                details = exc.details; protocol.record_ack_attempt(batch_index, logical_attempt, reply, validation_error=str(exc), repair_kind="incomplete", send_attempt_id=send_attempt_id, recovery_details=details)
                if incomplete_reuploads >= MAX_INCOMPLETE_REUPLOADS:
                    if event_sink is not None: event_sink("project_sync_ack_recovery_failed", sync_id=plan["sync_id"], batch_index=batch_index, attempt=logical_attempt, validation_error=str(exc), recovery_kind="incomplete", request_id=request_id)
                    raise
                targets = list(details["target_bundle_indexes"]); protocol.record_recovery(batch_index, send_attempt_id, targets, details)
                incomplete_reuploads += 1; logical_attempt += 1; current_kind = "selective_reupload"
                if event_sink is not None: event_sink("project_sync_attachment_reupload_started", sync_id=plan["sync_id"], batch_index=batch_index, attempt=logical_attempt, bundle_indexes=targets, reason=details["reason"], request_id=request_id)
                notification = protocol.reupload_notification(targets, str(details["reason"]))
                current_prompt = str(notification["prompt"]); selected_indexes = list(notification["payload"]["reupload_bundle_indexes"]); send_attempt_id = str(notification["payload"]["send_attempt_id"])
                continue
            except (ProjectSyncProtocolError, ValueError, TypeError) as exc:
                error_code = str(exc); protocol.record_ack_attempt(batch_index, logical_attempt, reply, validation_error=error_code, repair_kind="schema", send_attempt_id=send_attempt_id)
                if error_code in {"project_sync_ack_receiver_binding_mismatch", "project_sync_ack_send_attempt_mismatch"}:
                    # A reply from another conversation or another send is stale, never repairable.
                    raise
                if schema_repairs >= MAX_SCHEMA_REPAIRS:
                    if event_sink is not None: event_sink("project_sync_ack_repair_failed", sync_id=plan["sync_id"], batch_index=batch_index, attempt=logical_attempt, validation_error=error_code, request_id=request_id)
                    raise
                schema_repairs += 1; logical_attempt += 1; current_kind = "schema_repair"
                if event_sink is not None: event_sink("project_sync_ack_repair_started", sync_id=plan["sync_id"], batch_index=batch_index, attempt=logical_attempt, validation_error=error_code, request_id=request_id)
                send_attempt_id = protocol.new_send_attempt_id()
                current_prompt = build_ack_repair_prompt(plan, batch_index, payload.get("previous_batch_ack", ""), error_code, receiver_binding=binding, send_attempt_id=send_attempt_id)
                selected_indexes = []
                continue
        if event_sink is not None: event_sink("project_sync_batch_acknowledged", sync_id=plan["sync_id"], batch_index=batch_index, batch_count=batch_count, request_id=request_id)
    ready = protocol.require_ready()
    return {"schema": "PROJECT_SYNC_RUNTIME_RESULT_V1", "status": "PROJECT_SYNC_READY", "sync_id": plan["sync_id"], "snapshot_id": plan["snapshot_id"], "batch_count": plan["batch_count"], "acknowledged_batches": ready["acknowledged_batches"]}


__all__ = ["ProjectSyncTransport", "parse_project_sync_ack", "run_project_sync_transaction"]
