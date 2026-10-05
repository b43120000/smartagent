#!/usr/bin/env python3
"""Build the author-owned full-file manifest consumed by update.ps1."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path


SCHEMA = "SMARTAGENT_UPDATE_MANIFEST_V1"
SECURITY_POLICY_VERSION = 1
RELEASE_SEQUENCE = 2026100601
EXCLUDED_DIRS = {
    ".git", ".venv", "localdata", ".agents", "__pycache__", ".pytest_cache",
    "legacy",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".lnk"}
EXCLUDED_FILES = {
    "config/debug_config.json",
    "config/update_manifest.json",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deployable_files(root: Path) -> dict[str, str]:
    output: dict[str, str] = {}
    for current, directories, files in os.walk(root):
        directories[:] = sorted(
            name for name in directories
            if name.casefold() not in EXCLUDED_DIRS
            and not name.casefold().startswith(".update_")
        )
        base = Path(current)
        for name in sorted(files):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if relative.casefold() in EXCLUDED_FILES:
                continue
            if path.suffix.casefold() in EXCLUDED_SUFFIXES:
                continue
            output[relative] = sha256(path)
    return output


def build(root: Path, *, release_id: str = "") -> dict:
    protocol_path = root / "config" / "protocol_manifest.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    requirements: dict[str, str] = {}
    for relative in (
        "install_smart_agent/requirements.txt",
        "install_smart_agent/requirements-webdirect.txt",
    ):
        candidate = root / relative
        requirements[relative] = sha256(candidate) if candidate.is_file() else ""
    return {
        "schema": SCHEMA,
        "release_id": release_id or time.strftime("release-%Y%m%d-%H%M%S", time.gmtime()),
        "release_sequence": RELEASE_SEQUENCE,
        "protocol_family": str(protocol["protocol_family"]),
        "protocol_version": int(protocol["protocol_version"]),
        "security_policy_version": SECURITY_POLICY_VERSION,
        "requirements": requirements,
        "files": deployable_files(root),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--release-id", default="")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    output = root / "config" / "update_manifest.json"
    payload = build(root, release_id=args.release_id)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
