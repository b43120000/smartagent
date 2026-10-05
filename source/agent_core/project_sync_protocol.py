"""Shared project-sync batch protocol, ACK validation, and persistence."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Mapping


class ProjectSyncProtocolError(ValueError):
    pass


class ProjectSyncAckIncompleteError(ProjectSyncProtocolError):
    """A receiver-bound ACK explicitly says that a batch was not complete."""

    def __init__(self, details: Mapping[str, object]):
        self.details = dict(details)
        super().__init__("project_sync_ack_incomplete")


def _receiver_binding(binding: Mapping[str, object] | None = None) -> dict:
    value = dict(binding or {})
    return {"interface_name": str(value.get("interface_name", "") or ""), "conversation_id": str(value.get("conversation_id", "") or ""), "session_id": str(value.get("session_id", "") or "")}


def _batch_receipts(plan: Mapping[str, object], batch_index: int) -> list[dict]:
    batch = dict(list(plan.get("batches", []))[batch_index - 1])
    indexes = [int(value) for value in batch.get("bundle_indexes", [])]
    tokens = [str(value) for value in batch.get("receipt_tokens", [])]
    if len(indexes) != len(tokens) or any(
        not token.startswith("PSR1-") or len(token) != 29 for token in tokens
    ):
        raise ProjectSyncProtocolError("project_sync_receipt_contract_invalid")
    return [
        {"bundle_index": index, "receipt_token": token}
        for index, token in zip(indexes, tokens)
    ]


def _ack_schema_text(identity: Mapping[str, object]) -> str:
    return (
        "ACK identity（值必須完全照抄）：\n"
        + json.dumps(dict(identity), ensure_ascii=False, separators=(",", ":"))
        + "\nACK schema：{\"type\":\"PROJECT_SYNC_BATCH_ACK\","
        "\"sync_id\":\"...\",\"snapshot_id\":\"...\",\"batch_index\":1,"
        "\"received_receipts\":[{\"bundle_index\":1,\"receipt_token\":\"PSR1-...\"}],"
        "\"batch_complete\":true,\"missing_bundle_indexes\":[],"
        "\"unreadable_bundle_indexes\":[],\"incomplete_reason\":\"\","
        "\"previous_batch_ack\":\"...\",\"receiver_binding\":{},"
        "\"send_attempt_id\":\"...\",\"web_batch_ack_id\":\"<new unique id>\"}"
    )


def build_batch_ack_contract(plan: Mapping[str, object], batch_index: int, previous_batch_ack: str = "", receiver_binding: Mapping[str, object] | None = None, send_attempt_id: str = "") -> dict:
    batch = dict(list(plan.get("batches", []))[batch_index - 1])
    return {"type": "PROJECT_SYNC_BATCH_ACK", "sync_id": plan["sync_id"], "snapshot_id": plan["snapshot_id"], "batch_index": batch_index, "received_receipts": _batch_receipts(plan, batch_index), "batch_complete": True, "missing_bundle_indexes": [], "unreadable_bundle_indexes": [], "incomplete_reason": "", "previous_batch_ack": str(previous_batch_ack or ""), "receiver_binding": _receiver_binding(receiver_binding), "send_attempt_id": str(send_attempt_id or "<unique-send-attempt-id>"), "web_batch_ack_id": "<unique-non-empty-id>"}


def build_ack_repair_prompt(plan: Mapping[str, object], batch_index: int, previous_batch_ack: str, error_code: str, *, receiver_binding: Mapping[str, object] | None = None, send_attempt_id: str = "") -> str:
    identity = {"type": "PROJECT_SYNC_BATCH_ACK", "sync_id": plan["sync_id"], "snapshot_id": plan["snapshot_id"], "batch_index": batch_index, "previous_batch_ack": str(previous_batch_ack or ""), "receiver_binding": _receiver_binding(receiver_binding), "send_attempt_id": str(send_attempt_id)}
    return "[PROJECT_SYNC_ACK_REPAIR]\nvalidation_error=" + str(error_code) + "\nThe prior ACK failed schema validation. Correct only the ACK format; do not re-analyze the project.\nDo not request attachment re-upload; this repair turn has no attachments. Preserve receipt tokens actually read from the prior attachments; runtime-owned SHA-256 values must never be returned.\nReturn exactly one JSON object.\n" + _ack_schema_text(identity) + "\nbatch_complete must be a JSON boolean; string \"true\" is invalid. received_receipts, missing_bundle_indexes, and unreadable_bundle_indexes must be mutually exclusive and together cover this batch.\n[/PROJECT_SYNC_ACK_REPAIR]"


def _notification(plan: Mapping[str, object], batch_index: int, previous_batch_ack: str, receiver_binding: Mapping[str, object], send_attempt_id: str, selected_bundle_indexes: list[int] | None = None, recovery_reason: str = "") -> dict:
    batches = list(plan.get("batches", []))
    if batch_index < 1 or batch_index > len(batches): raise ProjectSyncProtocolError("project_sync_batch_index_out_of_range")
    batch = dict(batches[batch_index - 1]); selected = list(selected_bundle_indexes or batch["bundle_indexes"])
    identity = {"type": "PROJECT_SYNC_BATCH_ACK", "sync_id": plan["sync_id"], "snapshot_id": plan["snapshot_id"], "batch_index": batch_index, "previous_batch_ack": str(previous_batch_ack or ""), "receiver_binding": _receiver_binding(receiver_binding), "send_attempt_id": str(send_attempt_id)}
    payload = {"type": "PROJECT_SYNC_BATCH", "sync_id": plan["sync_id"], "snapshot_id": plan["snapshot_id"], "batch_index": batch_index, "batch_count": plan["batch_count"], "bundle_indexes": list(batch["bundle_indexes"]), "expected_total_bundles": plan["bundle_count"], "previous_batch_ack": str(previous_batch_ack or ""), "is_final_batch": batch["is_final_batch"], "receiver_binding": _receiver_binding(receiver_binding), "send_attempt_id": str(send_attempt_id), "reupload_bundle_indexes": selected}
    action = "This is a selective re-upload. Verify the listed attachment indexes again before ACKing.\n" if selected != list(batch["bundle_indexes"]) or recovery_reason else "只驗證並保存附件；不得開始完整分析、不得修改 project、不得產生 edit plan。\n"
    prompt = f"[PROJECT_SYNC_BATCH]\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n目前正在上傳第 {batch_index}/{plan['batch_count']} 批。\n{action}每個附件的檔名及內容首尾都有 B<bundle_index>/PSR1 receipt；請從附件實際讀取 receipt_token，禁止計算或回填 SHA-256。\n請只回覆一個 PROJECT_SYNC_BATCH_ACK JSON object，不得加前後說明。\n{_ack_schema_text(identity)}\nreceived_receipts 只放實際讀到有效 receipt 的附件；missing_bundle_indexes 與 unreadable_bundle_indexes 不得與 received_receipts 重疊，三者必須完整覆蓋本批 bundle_indexes。\n若未完整收到或無法讀取，回 batch_complete:false 並提供非空 incomplete_reason；不得假造 true。\n[/PROJECT_SYNC_BATCH]"
    selected_set = set(selected); attachments = [path for index, path in zip(batch["bundle_indexes"], batch["bundle_paths"]) if index in selected_set]
    return {"payload": payload, "prompt": prompt, "attachments": attachments}


def build_batch_notification(plan: Mapping[str, object], batch_index: int, previous_batch_ack: str = "", receiver_binding: Mapping[str, object] | None = None, send_attempt_id: str = "") -> dict:
    return _notification(plan, batch_index, previous_batch_ack, _receiver_binding(receiver_binding), send_attempt_id or "<initial-send-attempt-id>")


class ProjectSyncProtocol:
    def __init__(self, workspace: str | Path, plan: Mapping[str, object], *, receiver_binding: Mapping[str, object] | None = None, request_id: str = ""):
        self.workspace = Path(workspace).expanduser().resolve(); self.plan = dict(plan); self.receiver_binding = _receiver_binding(receiver_binding); self.request_id = str(request_id or "")
        if not all(self.receiver_binding.values()): raise ProjectSyncProtocolError("project_sync_receiver_binding_missing")
        self.state_dir = self.workspace / ".agents" / "project_sync_transactions"; self.state_path = self.state_dir / f"{self.plan['sync_id']}.json"; self.state = self._load_or_create()

    def _is_empty_transaction(self) -> bool:
        return (
            int(self.plan.get("bundle_count", 0)) == 0
            and int(self.plan.get("batch_count", 0)) == 0
            and not list(self.plan.get("batches", []))
        )

    def _new_state(self) -> dict:
        empty = self._is_empty_transaction()
        return {"schema": "PROJECT_SYNC_PROTOCOL_STATE_V3", "sync_id": self.plan["sync_id"], "snapshot_id": self.plan["snapshot_id"], "receiver_binding": self.receiver_binding, "request_id": self.request_id, "status": "PROJECT_SYNC_READY" if empty else "LOCAL_READY", "next_batch_index": 0 if empty else 1, "previous_batch_ack": "", "seen_web_batch_ack_ids": [], "acknowledged_batches": {}, "ack_attempts": {}, "upload_attempts": {}, "recovery_records": []}

    def _load_or_create(self) -> dict:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_path.is_file():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get("sync_id") != self.plan.get("sync_id") or state.get("snapshot_id") != self.plan.get("snapshot_id"): raise ProjectSyncProtocolError("project_sync_persisted_identity_mismatch")
            # Older states did not prove attachment-owned receipt tokens and
            # must never authorize a resumed V3 transaction.
            if state.get("schema") == "PROJECT_SYNC_PROTOCOL_STATE_V3" and state.get("receiver_binding") == self.receiver_binding:
                if self._is_empty_transaction() and state.get("status") != "PROJECT_SYNC_READY":
                    state["status"] = "PROJECT_SYNC_READY"
                    state["next_batch_index"] = 0
                    self._persist(state)
                return state
            previous_summary = {
                "schema": str(state.get("schema", "") or ""),
                "status": str(state.get("status", "") or ""),
                "acknowledged_batches": sorted(
                    str(key) for key in dict(state.get("acknowledged_batches") or {})
                ),
                "reason": "receipt_contract_upgrade_or_receiver_change",
            }
        else:
            previous_summary = None
        state = self._new_state()
        if previous_summary:
            state["superseded_state"] = previous_summary
        self._persist(state); return state

    def _persist(self, state: dict | None = None) -> None:
        value = state if state is not None else self.state; self.state_dir.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(f".json.{id(self)}.tmp"); temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"); temp.replace(self.state_path)

    @staticmethod
    def new_send_attempt_id() -> str: return "PSEND-" + uuid.uuid4().hex.upper()

    def next_notification(self, send_attempt_id: str | None = None) -> dict:
        if self.state["status"] == "PROJECT_SYNC_READY": raise ProjectSyncProtocolError("project_sync_already_ready")
        index = int(self.state["next_batch_index"]); attempt_id = send_attempt_id or self.new_send_attempt_id(); self.state["status"] = "UPLOADING"; self._persist()
        notification = _notification(self.plan, index, self.state["previous_batch_ack"], self.receiver_binding, attempt_id); self.state["status"] = "WAITING_BATCH_ACK"; self._persist(); return notification

    def reupload_notification(self, bundle_indexes: list[int], reason: str, send_attempt_id: str | None = None) -> dict:
        index = int(self.state["next_batch_index"]); expected = set(self.plan["batches"][index - 1]["bundle_indexes"]); selected = sorted({int(value) for value in bundle_indexes})
        if not selected or not set(selected).issubset(expected): raise ProjectSyncProtocolError("project_sync_reupload_indexes_invalid")
        return _notification(self.plan, index, self.state["previous_batch_ack"], self.receiver_binding, send_attempt_id or self.new_send_attempt_id(), selected, reason)

    def record_upload_attempt(self, batch_index: int, send_attempt_id: str, kind: str, bundle_indexes: list[int], aliases: list[dict]) -> None:
        self.state.setdefault("upload_attempts", {}).setdefault(str(batch_index), []).append({"send_attempt_id": str(send_attempt_id), "kind": str(kind), "bundle_indexes": list(bundle_indexes), "aliases": list(aliases), "receiver_binding": self.receiver_binding, "request_id": self.request_id}); self._persist()

    def record_ack_attempt(self, batch_index: int, attempt: int, raw_reply: object, *, validation_error: str = "", accepted: bool = False, repair_kind: str = "", send_attempt_id: str = "", recovery_details: Mapping[str, object] | None = None) -> None:
        raw = json.dumps(dict(raw_reply), ensure_ascii=False, sort_keys=True) if isinstance(raw_reply, Mapping) else str(raw_reply or "")
        record = {"attempt": int(attempt), "raw_reply": raw, "validation_error": str(validation_error or ""), "accepted": bool(accepted), "repair_kind": str(repair_kind or ""), "send_attempt_id": str(send_attempt_id or ""), "receiver_binding": self.receiver_binding, "request_id": self.request_id}
        if recovery_details: record["recovery_details"] = dict(recovery_details)
        self.state.setdefault("ack_attempts", {}).setdefault(str(batch_index), []).append(record); self._persist()

    def record_recovery(self, batch_index: int, source_attempt_id: str, target_indexes: list[int], details: Mapping[str, object]) -> None:
        self.state.setdefault("recovery_records", []).append({"batch_index": int(batch_index), "source_send_attempt_id": str(source_attempt_id), "target_bundle_indexes": list(target_indexes), "details": dict(details), "receiver_binding": self.receiver_binding, "request_id": self.request_id}); self._persist()

    def _expected(self, send_attempt_id: str = "") -> dict: return build_batch_ack_contract(self.plan, int(self.state["next_batch_index"]), self.state["previous_batch_ack"], self.receiver_binding, send_attempt_id)

    def _validate_identity(self, ack: Mapping[str, object], send_attempt_id: str) -> dict:
        expected = self._expected(send_attempt_id)
        for field, error in (("type", "project_sync_ack_type_mismatch"), ("sync_id", "project_sync_ack_sync_id_mismatch"), ("snapshot_id", "project_sync_ack_snapshot_id_mismatch")):
            if ack.get(field) != expected[field]: raise ProjectSyncProtocolError(error)
        try: actual_index = int(ack.get("batch_index", 0))
        except (TypeError, ValueError): actual_index = 0
        if actual_index != expected["batch_index"]: raise ProjectSyncProtocolError("project_sync_ack_batch_index_mismatch")
        if str(ack.get("previous_batch_ack", "")) != expected["previous_batch_ack"]: raise ProjectSyncProtocolError("project_sync_ack_chain_mismatch")
        actual_binding = _receiver_binding(ack.get("receiver_binding") if isinstance(ack.get("receiver_binding"), Mapping) else None)
        legacy = not str(self.receiver_binding.get("conversation_id", "")).startswith(("http://", "https://"))
        if actual_binding != self.receiver_binding and not (legacy and not ack.get("receiver_binding")):
            raise ProjectSyncProtocolError("project_sync_ack_receiver_binding_mismatch")
        if str(ack.get("send_attempt_id", "") or "") != str(send_attempt_id) and not (legacy and not ack.get("send_attempt_id")):
            raise ProjectSyncProtocolError("project_sync_ack_send_attempt_mismatch")
        return expected

    def incomplete_details(self, ack: Mapping[str, object], send_attempt_id: str) -> dict:
        expected = self._validate_identity(ack, send_attempt_id)
        if ack.get("batch_complete") is not False: raise ProjectSyncProtocolError("project_sync_ack_not_explicitly_incomplete")
        expected_receipts = {
            int(item["bundle_index"]): str(item["receipt_token"])
            for item in expected["received_receipts"]
        }
        expected_indexes = list(expected_receipts)
        def indexes(name: str) -> list[int] | None:
            raw = ack.get(name)
            if not isinstance(raw, list): return None
            try: return [int(value) for value in raw]
            except (TypeError, ValueError): return None
        receipts = ack.get("received_receipts")
        received: list[int] | None = None
        if isinstance(receipts, list):
            received = []
            for item in receipts:
                if not isinstance(item, Mapping):
                    received = None; break
                try: index = int(item.get("bundle_index", 0))
                except (TypeError, ValueError): received = None; break
                if str(item.get("receipt_token", "") or "") != expected_receipts.get(index):
                    received = None; break
                received.append(index)
        missing, unreadable = indexes("missing_bundle_indexes"), indexes("unreadable_bundle_indexes"); reason = str(ack.get("incomplete_reason", "") or "").strip()
        valid = all(value is not None for value in (received, missing, unreadable)) and bool(reason)
        if valid:
            received, missing, unreadable = list(received or []), list(missing or []), list(unreadable or []); report = received + missing + unreadable
            valid = len(report) == len(set(report)) and set(report) == set(expected_indexes)
        if not valid: return {"diagnosis_valid": False, "reason": "receiver_report_missing_or_invalid_details", "missing_bundle_indexes": list(expected_indexes), "unreadable_bundle_indexes": [], "target_bundle_indexes": list(expected_indexes)}
        targets = sorted(set((missing or []) + (unreadable or [])))
        if not targets:
            return {"diagnosis_valid": False, "reason": "receiver_reported_incomplete_without_missing_indexes", "missing_bundle_indexes": list(expected_indexes), "unreadable_bundle_indexes": [], "target_bundle_indexes": list(expected_indexes)}
        return {"diagnosis_valid": True, "reason": reason, "missing_bundle_indexes": list(missing or []), "unreadable_bundle_indexes": list(unreadable or []), "target_bundle_indexes": targets}

    def accept_ack(self, ack: Mapping[str, object], *, send_attempt_id: str = "") -> dict:
        expected = self._validate_identity(ack, send_attempt_id)
        if ack.get("batch_complete") is False: raise ProjectSyncAckIncompleteError(self.incomplete_details(ack, send_attempt_id))
        if ack.get("batch_complete") is not True: raise ProjectSyncProtocolError("project_sync_ack_incomplete")
        if list(ack.get("received_receipts", [])) != expected["received_receipts"]: raise ProjectSyncProtocolError("project_sync_ack_receipts_mismatch")
        if ("missing_bundle_indexes" in ack and ack.get("missing_bundle_indexes") != []) or ("unreadable_bundle_indexes" in ack and ack.get("unreadable_bundle_indexes") != []) or str(ack.get("incomplete_reason", "") or ""): raise ProjectSyncProtocolError("project_sync_ack_complete_details_invalid")
        ack_id = str(ack.get("web_batch_ack_id", "") or "")
        if not ack_id: raise ProjectSyncProtocolError("project_sync_ack_id_missing")
        if ack_id in self.state["seen_web_batch_ack_ids"]: raise ProjectSyncProtocolError("project_sync_ack_id_reused")
        expected_index = int(self.state["next_batch_index"]); self.state["seen_web_batch_ack_ids"].append(ack_id); self.state["previous_batch_ack"] = ack_id
        batch = dict(self.plan["batches"][expected_index - 1])
        self.state["acknowledged_batches"][str(expected_index)] = {"bundle_indexes": list(batch["bundle_indexes"]), "bundle_hashes": list(batch["bundle_hashes"]), "received_receipts": list(ack["received_receipts"]), "web_batch_ack_id": ack_id, "send_attempt_id": str(send_attempt_id), "receiver_binding": self.receiver_binding}; self.state["next_batch_index"] = expected_index + 1
        if expected_index == int(self.plan["batch_count"]): self._final_barrier()
        else: self.state["status"] = "LOCAL_READY"; self._persist()
        return dict(self.state)

    def _final_barrier(self) -> None:
        acknowledged = self.state["acknowledged_batches"]; expected_batches = list(range(1, int(self.plan["batch_count"]) + 1))
        if sorted(int(key) for key in acknowledged) != expected_batches: raise ProjectSyncProtocolError("project_sync_final_barrier_missing_batch")
        indexes, hashes, receipts = [], [], []
        for index in expected_batches:
            record = acknowledged[str(index)]
            indexes.extend(record["bundle_indexes"])
            hashes.extend(record["bundle_hashes"])
            receipts.extend(
                str(item.get("receipt_token", "") or "")
                for item in record.get("received_receipts", [])
            )
        if indexes != list(range(1, int(self.plan["bundle_count"]) + 1)): raise ProjectSyncProtocolError("project_sync_final_barrier_index_mismatch")
        if hashes != list(self.plan["expected_hashes"]): raise ProjectSyncProtocolError("project_sync_final_barrier_hash_mismatch")
        if receipts != list(self.plan.get("expected_receipts", [])): raise ProjectSyncProtocolError("project_sync_final_barrier_receipt_mismatch")
        self.state["status"] = "PROJECT_SYNC_READY"; self._persist()

    def require_ready(self) -> dict:
        if self.state.get("status") != "PROJECT_SYNC_READY": raise ProjectSyncProtocolError("project_sync_final_barrier_not_ready")
        return dict(self.state)


__all__ = ["ProjectSyncProtocol", "ProjectSyncProtocolError", "ProjectSyncAckIncompleteError", "build_ack_repair_prompt", "build_batch_ack_contract", "build_batch_notification"]
