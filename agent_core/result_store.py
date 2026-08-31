"""Content-addressed local storage for full stage results."""
from __future__ import annotations
import hashlib, json
from pathlib import Path

class ResultStore:
    def __init__(self, root: str | Path): self.root = Path(root)
    def put(self, value: dict) -> dict:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest(); self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{digest}.json"
        if not path.exists():
            temp = path.with_suffix(".tmp"); temp.write_bytes(raw); temp.replace(path)
        return {"result_ref": f"sha256:{digest}", "sha256": digest, "result_bytes": len(raw), "path": str(path)}
    @staticmethod
    def compact(result: dict, ref: dict) -> dict:
        # Only non-sensitive deterministic verdicts leave local storage.  Full
        # stdout/stderr/error evidence remains content-addressed on this host.
        actions=dict(result.get("actions") or {})
        evidence={key:{"status":item.get("status"),"reason":str(item.get("reason", ""))[:160]} for key,item in actions.items()}
        safe_ref={key:ref[key] for key in ("result_ref","sha256","result_bytes") if key in ref}
        return {"schema": "SMARTAGENT_STAGE_COMPACT_RESULT_V1", "stage_id": result.get("stage_id"), "seq": result.get("seq"), "status": result.get("status"), "action_evidence": evidence, **safe_ref}
