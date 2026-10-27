"""Static guardrail check for release manifests and fail-closed update routing."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "source"))
sys.path.insert(0, str(ROOT))


def run(root: Path = ROOT) -> dict:
    updater = (root / "install_smart_agent" / "manifest_update.ps1").read_text(encoding="utf-8")
    entrypoint = (root / "update.bat").read_text(encoding="utf-8").casefold()
    builder = (root / "install_smart_agent" / "build_release.py").read_text(encoding="utf-8")
    required = (
        "source_build_provenance_schema_invalid",
        "update_manifest_source_provenance_hash_mismatch",
        "update_manifest_core_hash_disagreement",
        "UPDATE_RELEASE_SEQUENCE_NOT_NEWER",
        "UPDATE_SOURCE_INVALID:root_update_entrypoint_not_ready",
        "update_stale_file_changed",
        "remainingOwnedStale",
        "Assert-AclOff $target",
    )
    missing = [token for token in required if token.casefold() not in updater.casefold()]
    assert not missing, {"missing": missing}
    assert "[blocked]" in entrypoint and "exit /b 2" in entrypoint
    assert "release_update_entrypoint_not_ready" in builder
    return {
        "release_update_guardrails": "PASS",
        "scope": "static only; not a package deployment or PowerShell ValidateOnly execution",
        "root_entrypoint": "FAIL_CLOSED",
    }


if __name__ == "__main__":
    print(run())
