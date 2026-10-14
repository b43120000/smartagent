"""Consume Runtime-owned web upload receipts, never model assertions."""
from dataclasses import asdict
from pathlib import Path

from .attachment_transaction import AttachmentTransaction


def confirmed_delivery(root, *, request_id, task_epoch, paths, started_at, artifacts):
    """Only this ask's CONSUMED transactions can complete staged artifacts.

    CONSUMED proves ready + uniquely acknowledged submit + completed readback,
    not that the model understood the file or that the overall task succeeded.
    """
    if not request_id or not task_epoch or not paths or not artifacts:
        return []
    expected_paths = {str(Path(p).resolve()) for p in paths}
    store = AttachmentTransaction(root)
    receipts = []
    for artifact in artifacts:
        path = str(Path(artifact.get('staged_path', '')).resolve())
        digest = artifact.get('sha256')
        if path not in expected_paths or not digest:
            return []
        candidates = [record for record in store.records.values()
            if record.request_id == request_id and record.task_epoch == task_epoch
            and record.state == 'CONSUMED' and record.conversation_id
            and record.created_at >= started_at
            and str(Path(record.source_path).resolve()) == path
            and record.sha256 == digest]
        if len(candidates) != 1 or not store.verify_file(candidates[0].attachment_id):
            return []
        receipts.append(asdict(candidates[0]))
    if len({r['conversation_id'] for r in receipts}) != 1:
        return []
    return receipts
