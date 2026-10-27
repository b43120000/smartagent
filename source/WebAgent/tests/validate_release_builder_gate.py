"""Release builder must fail closed while the root update entrypoint is not ready."""
from __future__ import annotations

import argparse
from pathlib import Path

from install_smart_agent.build_release import build_release


def run(root: Path, output_dir: Path) -> dict:
    if output_dir.exists():
        raise AssertionError("test output directory must be new")
    try:
        build_release(root, output_dir, release_id="sa-r010-gate-check")
    except RuntimeError as exc:
        if str(exc) != "release_update_entrypoint_not_ready":
            raise
        assert not output_dir.exists(), "blocked builder must not leave a partial release directory"
        return {
            "release_builder": "BLOCKED_AS_REQUIRED",
            "reason": "release_update_entrypoint_not_ready",
            "artifact_created": False,
        }
    raise AssertionError("release builder must not package a nonfunctional root updater")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    result = run(Path(args.root), Path(args.output_dir))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
