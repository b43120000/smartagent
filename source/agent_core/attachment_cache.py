"""Conversation/session/hash attachment cache; metadata only, never content."""
from __future__ import annotations
import hashlib, json, os, shutil
from pathlib import Path

class AttachmentCache:
    def __init__(self, root: str|Path): self.root=Path(root)
    def _path(self, conversation_id: str, session_id: str, digest: str) -> Path:
        safe=lambda s: hashlib.sha256(str(s).encode()).hexdigest()
        return self.root/safe(conversation_id)/safe(session_id)/f"{digest}.json"
    def contains(self, conversation_id: str, session_id: str, digest: str) -> bool: return self._path(conversation_id,session_id,digest).is_file()
    def mark(self, conversation_id: str, session_id: str, data: bytes) -> str:
        digest=hashlib.sha256(data).hexdigest(); p=self._path(conversation_id,session_id,digest); p.parent.mkdir(parents=True,exist_ok=True)
        if not p.exists(): p.write_text(json.dumps({"sha256":digest}),encoding="utf-8")
        return digest

    def stage_file(self, conversation_id: str, session_id: str, source: str | Path) -> tuple[Path, bool, str]:
        """Make a content-addressed local staging copy without changing UI flow.

        Callers must still make an isolated request copy for every browser
        upload.  This cache only avoids repeatedly copying identical source
        bytes from the user file into local staging.
        """
        source = Path(source)
        h = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
        digest = h.hexdigest()
        marker = self._path(conversation_id, session_id, digest)
        target = marker.parent / digest / source.name
        hit = target.is_file() and _digest_file(target) == digest
        if not hit:
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + f".{os.getpid()}.tmp")
            shutil.copy2(source, temp)
            if _digest_file(temp) != digest:
                temp.unlink(missing_ok=True)
                raise ValueError("attachment_cache_copy_hash_mismatch")
            temp.replace(target)
            marker.write_text(json.dumps({"sha256": digest}), encoding="utf-8")
        return target, hit, digest

def _digest_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b""): h.update(chunk)
    return h.hexdigest()
