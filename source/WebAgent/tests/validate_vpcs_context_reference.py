"""Regression checks for snapshot-bound lazy context references."""
import tempfile
from pathlib import Path

from agent_core.project_access import build_project_capsule, query_project
from agent_core.project_context_ref import parse_context_ref, ContextReferenceError


def run():
    with tempfile.TemporaryDirectory(prefix="vpcs-ref-") as temp:
        root = Path(temp)
        source = root / "sample.py"
        source.write_text("def answer():\n    return 42\n", encoding="utf-8")
        capsule = build_project_capsule(root)
        args = {
            "snapshot_id": capsule["snapshot_id"],
            "project_handle": capsule["project_handle"],
        }

        metadata = query_project(root, queries=[
            {"operation": "get_file_metadata", "path": "sample.py"}
        ], **args)["results"][0]
        assert metadata["status"] == "OK", metadata
        reference = metadata["context_ref"]
        path, digest = parse_context_ref(reference, args["project_handle"], args["snapshot_id"])
        assert path == "sample.py" and digest == metadata["file"]["sha256"]

        loaded = query_project(root, queries=[
            {"operation": "read_context_ref", "context_ref": reference}
        ], **args)["results"][0]
        assert loaded["status"] == "OK" and "return 42" in loaded["content"], loaded

        wrong_project = query_project(root, queries=[
            {"operation": "read_context_ref", "context_ref": reference.replace(
                args["project_handle"], "PROJECT-WRONG", 1)}
        ], **args)["results"][0]
        assert wrong_project["status"] == "ERROR" and "project_mismatch" in wrong_project["error"]

        source.write_text("def answer():\n    return 99\n", encoding="utf-8")
        stale = query_project(root, queries=[
            {"operation": "read_context_ref", "context_ref": reference}
        ], **args)["results"][0]
        assert stale["status"] == "ERROR" and "project_snapshot_stale" in stale["error"], stale

        new_capsule = build_project_capsule(root)
        cross_snapshot = query_project(root, queries=[
            {"operation": "read_context_ref", "context_ref": reference}
        ], snapshot_id=new_capsule["snapshot_id"], project_handle=new_capsule["project_handle"])["results"][0]
        assert cross_snapshot["status"] == "ERROR" and "snapshot_mismatch" in cross_snapshot["error"]

    print("VPCS_CONTEXT_REFERENCE_PASS")


if __name__ == "__main__":
    run()
