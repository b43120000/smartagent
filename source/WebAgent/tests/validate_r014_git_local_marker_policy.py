"""R014-B targeted policy regression. Fixtures only, no Runtime or Git writes."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "source"))

from agent_core.git_local_marker_policy import (
    classify_comparison_path, summarize_cross_install_hashes,
)
from agent_core.protocol_manifest import (
    CORE_FILES, ProtocolManifestError, validate_protocol_manifest,
)


def run() -> dict:
    assert classify_comparison_path("localdata/runtime/state.json") == "LOCAL_ONLY"
    assert classify_comparison_path(".agents/chunked_writes/ledger.json") == "LOCAL_ONLY"
    assert classify_comparison_path("source/WebAgent/dom_config.json") == "LOCAL_ONLY"
    assert classify_comparison_path("config/browser_session.json") == "LOCAL_ONLY"
    assert classify_comparison_path("source/agent_core/protocol_manifest.py") == "VERSIONED_PRODUCT"
    assert classify_comparison_path("config/protocol_manifest.json") == "CANONICAL_BUILD_METADATA"
    assert classify_comparison_path("config/update_manifest.json") == "CANONICAL_BUILD_METADATA"
    for bad in ("../secret", "C:/secret", "/abs", "", "x//y", "x/./y"):
        try:
            classify_comparison_path(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe comparison path admitted: " + bad)

    candidate = {
        "config/protocol_manifest.json": "a" * 64,
        "config/update_manifest.json": "b" * 64,
        "source/agent_core/protocol_manifest.py": "c" * 64,
        "source/WebAgent/controller.py": "d" * 64,
        "localdata/runtime/state.json": "e" * 64,
    }
    installed = {
        "config/protocol_manifest.json": "f" * 64,
        "config/update_manifest.json": "1" * 64,
        "source/agent_core/protocol_manifest.py": "c" * 64,
        "source/WebAgent/controller.py": "2" * 64,
        "localdata/runtime/state.json": "3" * 64,
    }
    summary = summarize_cross_install_hashes(
        candidate, installed,
        dirty_tracked_paths=frozenset({"config/protocol_manifest.json"}),
    )
    assert summary["versioned_match"] == ["source/agent_core/protocol_manifest.py"]
    assert summary["versioned_difference"] == ["source/WebAgent/controller.py"]
    assert summary["local_exempt_count"] == 1
    assert summary["canonical_cross_install_exempt"] == [
        "config/protocol_manifest.json", "config/update_manifest.json",
    ]
    assert summary["tracked_conflicts"] == ["config/protocol_manifest.json"]
    assert summary["strict_same_tree_manifest_required"] is True
    assert summary["loaded_worker_sha_attested"] is False

    # Cross-install exemption CANNOT turn stale canonical SAME-TREE hashes to PASS.
    expected = {name: "a" * 64 for name in CORE_FILES}
    canonical = {
        "schema": "SMARTAGENT_PROTOCOL_MANIFEST_V1",
        "manifest_schema_version": 1,
        "protocol_family": "SMARTAGENT_V9",
        "protocol_version": 9,
        "core_files": expected,
    }
    actual = {"missing_files": [], "core_files": dict(expected)}
    with TemporaryDirectory(prefix="r014-local-marker-policy-") as temp:
        with patch("agent_core.protocol_manifest.build_protocol_manifest", return_value=actual), patch(
            "agent_core.protocol_manifest._software_only_mode_enabled", return_value=False
        ):
            ok = validate_protocol_manifest(Path(temp), manifest=canonical)
            assert ok["hash_verification"] == "PASS"
            assert not ok["hash_mismatches"]

            changed = dict(canonical, core_files=dict(expected))
            changed["core_files"]["source/agent_core/protocol_manifest.py"] = "b" * 64
            try:
                validate_protocol_manifest(Path(temp), manifest=changed)
            except ProtocolManifestError as exc:
                assert "protocol_manifest_hash_mismatch" in str(exc)
            else:
                raise AssertionError("same-tree modified verifier hash was accepted")
    return {"status": "PASS", "test_id": "R014-GIT-U01..U07", "manifest_entries": len(CORE_FILES)}


if __name__ == "__main__":
    print(run())
