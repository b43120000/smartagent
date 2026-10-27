#!/usr/bin/env python3
"""Build the author-owned full-file manifest consumed by manifest_update.ps1."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path


SCHEMA = "SMARTAGENT_UPDATE_MANIFEST_V1"
SECURITY_POLICY_VERSION = 1
RELEASE_SEQUENCE = 2026102701
EXCLUDED_DIRS = {
    ".git", ".venv", "localdata", ".agents", "__pycache__", ".pytest_cache",
    "browser_profile", ".codex", ".aws", ".ssh", "dom_config.history", "defaultworkspace",
}
EXCLUDED_SUFFIXES = {".bak", ".tmp",".pyc", ".pyo", ".lnk", ".pem", ".key", ".pfx", ".p12", ".ppk"}
EXCLUDED_FILES = {
    "config/debug_config.json",
    "config/browser_session.json",
    "config/update_manifest.json",
    "doc/portable_git_handoff.md",
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
            and not name.casefold().startswith(".env")
            and not name.casefold().startswith(".update_")
        )
        base = Path(current)
        for name in sorted(files):
            if name.casefold().startswith(".env") or name.casefold() == "dom_config.json":
                continue
            path = base / name
            relative = path.relative_to(root).as_posix()
            if relative.casefold() in EXCLUDED_FILES:
                continue
            if name.casefold().startswith('dom_config - ') or (relative.startswith('source/run_t16_') and path.suffix == '.py'):
                continue
            if path.suffix.casefold() in EXCLUDED_SUFFIXES:
                continue
            output[relative] = sha256(path)
    # The only distributable venv content is a clean, immutable payload.
    # Existing extracted interpreters, packages and machine configuration stay private.
    payload=root / '.venv' / 'portable-runtime'
    if payload.is_dir() and not payload.is_symlink():
        for path in payload.iterdir():
            if re.fullmatch(r'(manifest\.json|requirements-lock\.json|LICENSES\.json|README\.md|runtime\.zip(?:\.part[0-9]+)?)',path.name) and path.is_file() and not path.is_symlink():
                output[path.relative_to(root).as_posix()]=sha256(path)
    git_payload = root / '.venv' / 'portable-git'
    if git_payload.is_dir() and not git_payload.is_symlink():
        for name in ('manifest.json', 'git.zip'):
            path = git_payload / name
            if path.is_file() and not path.is_symlink():
                output[path.relative_to(root).as_posix()] = sha256(path)
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
