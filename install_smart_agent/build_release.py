#!/usr/bin/env python3
"""Build a complete SmartAgent release artifact from a clean checkout."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
import sys


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _outside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return False
    except ValueError:
        return True


def build_release(root: Path, output_dir: Path, *, release_id: str = "") -> dict:
    root = root.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError("release_source_root_missing")
    if not _outside(output_dir, root):
        raise RuntimeError("release_output_must_be_outside_source_root")
    if (root / "config" / "source_build_provenance.json").exists():
        raise RuntimeError("release_provenance_must_be_generated_in_stage")
    entrypoint = root / "update.bat"
    try:
        entrypoint_text = entrypoint.read_text(encoding="utf-8").casefold()
    except OSError as exc:
        raise RuntimeError("release_update_entrypoint_missing") from exc
    if (
        "[blocked]" in entrypoint_text
        or "manifest_update.ps1" not in entrypoint_text
        or "powershell.exe" not in entrypoint_text
        or "executionpolicy bypass" in entrypoint_text
    ):
        raise RuntimeError("release_update_entrypoint_not_ready")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("release_source_tree_dirty")
    source_commit = _git(root, "rev-parse", "HEAD").lower()
    source_tree_sha = _git(root, "rev-parse", "HEAD^{tree}").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit) or not re.fullmatch(r"[0-9a-f]{40}", source_tree_sha):
        raise RuntimeError("release_git_identity_invalid")

    sys.path.insert(0, str(root / "source"))
    sys.path.insert(0, str(root))
    from agent_core import protocol_manifest
    from install_smart_agent import build_update_manifest

    sequence = build_update_manifest.RELEASE_SEQUENCE
    release_id = release_id or f"smartagent-{sequence}-{source_commit[:12]}"
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", release_id):
        raise RuntimeError("release_id_invalid")
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".smartagent-release-stage-", dir=str(output_dir)) as temp:
        stage = Path(temp) / "package"
        stage.mkdir()
        source_files = build_update_manifest.deployable_files(root)
        if not source_files:
            raise RuntimeError("release_source_inventory_empty")
        for relative, expected_sha in sorted(source_files.items()):
            source = root / Path(relative)
            if source.is_symlink() or not source.is_file():
                raise RuntimeError("release_source_file_not_regular:" + relative)
            destination = stage / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            if _sha256(destination) != expected_sha:
                raise RuntimeError("release_copy_hash_mismatch:" + relative)

        protocol = protocol_manifest.build_protocol_manifest(stage)
        if protocol.get("missing_files"):
            raise RuntimeError("release_protocol_core_missing:" + ",".join(protocol["missing_files"]))
        core_files = protocol.get("core_files")
        if not isinstance(core_files, dict) or len(core_files) != len(protocol_manifest.CORE_FILES):
            raise RuntimeError("release_protocol_core_count_mismatch")
        if len({str(name).casefold() for name in core_files}) != len(core_files):
            raise RuntimeError("release_protocol_core_duplicate")
        protocol_path = stage / "config" / "protocol_manifest.json"
        _write_json(protocol_path, protocol)
        strict = protocol_manifest.validate_protocol_manifest(stage, manifest=protocol, manifest_file=protocol_path)
        if strict.get("hash_verification") != "PASS" or strict.get("hash_mismatches"):
            raise RuntimeError("release_protocol_manifest_not_strict_pass")

        from install_smart_agent.build_update_manifest import RELEASE_SEQUENCE
        provenance = {
            "schema": "SMARTAGENT_SOURCE_BUILD_PROVENANCE_V1",
            "source_commit": source_commit,
            "source_tree_sha": source_tree_sha,
            "source_tree_clean_at_build": True,
            "provenance_kind": "UNSIGNED_CLEAN_GIT_CHECKOUT",
            "publisher_signature_status": "NOT_PROVIDED",
            "build_tool": "install_smart_agent/build_release.py",
            "built_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "release_id": release_id,
            "release_sequence": RELEASE_SEQUENCE,
            "protocol_family": protocol["protocol_family"],
            "protocol_version": protocol["protocol_version"],
            "protocol_manifest_sha256": _sha256(protocol_path),
        }
        provenance_path = stage / "config" / "source_build_provenance.json"
        _write_json(provenance_path, provenance)
        manifest = build_update_manifest.build(stage, release_id=release_id)
        manifest_path = stage / "config" / "update_manifest.json"
        _write_json(manifest_path, manifest)
        expected = build_update_manifest.deployable_files(stage)
        listed = manifest.get("files")
        if not isinstance(listed, dict) or set(listed) != set(expected):
            raise RuntimeError("release_update_manifest_inventory_mismatch")
        for relative, digest in expected.items():
            if str(listed.get(relative) or "").lower() != digest.lower():
                raise RuntimeError("release_update_manifest_hash_mismatch:" + relative)
        if str(listed.get("config/source_build_provenance.json") or "").lower() != _sha256(provenance_path):
            raise RuntimeError("release_provenance_not_in_update_manifest")
        if str(listed.get("config/protocol_manifest.json") or "").lower() != _sha256(protocol_path):
            raise RuntimeError("release_protocol_manifest_not_in_update_manifest")
        if manifest.get("release_sequence") != provenance["release_sequence"]:
            raise RuntimeError("release_sequence_provenance_mismatch")

        archive_name = "SmartAgent-" + release_id + ".zip"
        archive_path = output_dir / archive_name
        temporary_archive = output_dir / (archive_name + ".tmp")
        with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for relative in sorted(listed) + ["config/update_manifest.json"]:
                member = stage / Path(relative)
                if not member.is_file() or member.is_symlink():
                    raise RuntimeError("release_archive_member_invalid:" + relative)
                archive.write(member, arcname=Path(relative).as_posix())
        temporary_archive.replace(archive_path)
        report = {
            "schema": "SMARTAGENT_RELEASE_ARTIFACT_REPORT_V1",
            "release_id": release_id,
            "release_sequence": manifest["release_sequence"],
            "source_commit": source_commit,
            "source_tree_sha": source_tree_sha,
            "protocol_core_file_count": len(core_files),
            "deployable_file_count": len(listed),
            "protocol_manifest_sha256": _sha256(protocol_path),
            "update_manifest_sha256": _sha256(manifest_path),
            "artifact_file": archive_name,
            "artifact_sha256": _sha256(archive_path),
            "publisher_signature_status": "NOT_PROVIDED",
        }
        report_path = output_dir / ("SmartAgent-" + release_id + ".release.json")
        _write_json(report_path, report)
        return {"status": "PASS", "artifact": str(archive_path), "report": str(report_path), **report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="clean SmartAgent Git checkout")
    parser.add_argument("--output-dir", required=True, help="directory outside the source checkout")
    parser.add_argument("--release-id", default="")
    args = parser.parse_args(argv)
    result = build_release(Path(args.root), Path(args.output_dir), release_id=args.release_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("RELEASE_BUILD_BLOCKED:" + str(exc), file=sys.stderr)
        raise SystemExit(2)
