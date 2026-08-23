#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from .artifact_bundle_protocol import ArtifactBundleError, load_bundle
from .workspace import AGENT_PROJECT_ROOT


class ArtifactBundleReceiver:
    def __init__(self, state_root: str | Path | None = None):
        self.state_root = Path(state_root or (AGENT_PROJECT_ROOT / ".agents" / "artifact_bundles"))

    @staticmethod
    def _atomic(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def _state_path(self, bundle_id: str) -> Path:
        return self.state_root / bundle_id / "state.json"

    def _load_state(self, bundle_id: str) -> dict:
        p = self._state_path(bundle_id)
        try:
            value = json.loads(p.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _save_state(self, bundle_id: str, state: dict) -> None:
        state["updated_at"] = time.time()
        self._atomic(self._state_path(bundle_id), state)

    def execute(
        self,
        path: str | Path,
        *,
        execute_action: Callable[[dict], str],
        validate_action: Callable[[dict], tuple[bool, dict | None]],
        expected_sha256: str = "",
    ) -> str:
        manifest, actions, evidence = load_bundle(path, expected_sha256=expected_sha256)
        bundle_id = evidence["bundle_id"]
        state = self._load_state(bundle_id)

        old_hash = str(state.get("zip_sha256", "") or "")
        if old_hash and old_hash != evidence["zip_sha256"]:
            raise ArtifactBundleError(f"bundle_id reused with different ZIP: {bundle_id}")

        committed = state.get("committed", {})
        if not isinstance(committed, dict):
            committed = {}

        state.update({
            "protocol_name": manifest["protocol_name"],
            "protocol_version": manifest["protocol_version"],
            "bundle_id": bundle_id,
            "zip_sha256": evidence["zip_sha256"],
            "actions_sha256": evidence["actions_sha256"],
            "action_count": evidence["action_count"],
            "status": "RUNNING",
            "committed": committed,
        })
        self._save_state(bundle_id, state)

        results = []
        replayed = 0
        executed = 0

        for index, action in enumerate(actions, 1):
            action_id = action["action_id"]

            if action_id in committed:
                replayed += 1
                results.append({
                    "index": index,
                    "action_id": action_id,
                    "tool": action.get("tool", ""),
                    "status": "REUSED_COMMITTED",
                    "result": committed[action_id].get("result", ""),
                })
                continue

            valid, diagnostic = validate_action(action)
            if not valid:
                state["status"] = "FAILED"
                state["failed_action_id"] = action_id
                state["failure"] = {"kind": "SCHEMA_REJECTED", "diagnostic": diagnostic or {}}
                self._save_state(bundle_id, state)
                return json.dumps({
                    "status": "ARTIFACT_BUNDLE_FAILED",
                    "bundle_id": bundle_id,
                    "failed_action_id": action_id,
                    "reason": "SCHEMA_REJECTED",
                    "diagnostic": diagnostic or {},
                    "executed": executed,
                    "replayed": replayed,
                }, ensure_ascii=False, separators=(",", ":"))

            state["active_action"] = {
                "index": index,
                "action_id": action_id,
                "tool": action.get("tool", ""),
                "state": "STARTED_UNCONFIRMED",
                "started_at": time.time(),
            }
            self._save_state(bundle_id, state)

            try:
                result = execute_action(action)
            except Exception as exc:
                state["status"] = "FAILED"
                state["failed_action_id"] = action_id
                state["failure"] = {"kind": type(exc).__name__, "message": str(exc)[:4000]}
                self._save_state(bundle_id, state)
                raise

            result_text = str(result)
            if action.get("tool") == "run_command":
                if "VERIFICATION_STATUS: FAIL" in result_text or "VERIFICATION_STATUS: UNVERIFIED" in result_text:
                    state["status"] = "FAILED"
                    state["failed_action_id"] = action_id
                    state["failure"] = {
                        "kind": "RUN_COMMAND_VERIFICATION_FAILED",
                        "result": result_text[:12000],
                    }
                    self._save_state(bundle_id, state)
                    return json.dumps({
                        "status": "ARTIFACT_BUNDLE_FAILED",
                        "bundle_id": bundle_id,
                        "failed_action_id": action_id,
                        "reason": "RUN_COMMAND_VERIFICATION_FAILED",
                        "result": result_text,
                        "executed": executed,
                        "replayed": replayed,
                    }, ensure_ascii=False, separators=(",", ":"))

            committed[action_id] = {
                "tool": action.get("tool", ""),
                "result": result_text,
                "committed_at": time.time(),
            }
            executed += 1
            state["committed"] = committed
            state["active_action"] = {}
            self._save_state(bundle_id, state)
            results.append({
                "index": index,
                "action_id": action_id,
                "tool": action.get("tool", ""),
                "status": "COMMITTED",
                "result": result_text,
            })

        state["status"] = "COMPLETED"
        state["active_action"] = {}
        state["failed_action_id"] = ""
        state["failure"] = {}
        self._save_state(bundle_id, state)

        return json.dumps({
            "status": "ARTIFACT_BUNDLE_COMPLETED",
            "bundle_id": bundle_id,
            "zip_sha256": evidence["zip_sha256"],
            "action_count": evidence["action_count"],
            "executed": executed,
            "replayed": replayed,
            "results": results,
        }, ensure_ascii=False, separators=(",", ":"))
