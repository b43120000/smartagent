#!/usr/bin/env python3
"""Compare two workspace images and emit deterministic RGB difference metrics."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageStat


SCHEMA = "SMARTAGENT_IMAGE_COMPARE_V1"


def _workspace_file(value: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("image_path_required")
    requested = Path(raw)
    if requested.is_absolute():
        raise ValueError("image_path_must_be_relative")
    root = Path.cwd().resolve()
    resolved = (root / requested).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("image_path_outside_workspace") from exc
    if not resolved.is_file():
        raise FileNotFoundError(raw)
    return resolved


def _round(values) -> list[float]:
    return [round(float(value), 6) for value in values]


def compare(left_path: Path, right_path: Path) -> dict[str, object]:
    with Image.open(left_path) as left_source, Image.open(right_path) as right_source:
        left = left_source.convert("RGB")
        right = right_source.convert("RGB")
        if left.size != right.size:
            return {
                "schema": SCHEMA,
                "status": "ERROR",
                "error": "image_size_mismatch",
                "left_size": list(left.size),
                "right_size": list(right.size),
            }
        diff = ImageChops.difference(left, right)
        stat = ImageStat.Stat(diff)
        mae = _round(stat.mean)
        rmse = _round(stat.rms)
        extrema = diff.getextrema()
        max_abs_diff = [int(channel[1]) for channel in extrema]
        mean_abs_diff = round(sum(mae) / 3.0, 6)
        width, height = left.size
        return {
            "schema": SCHEMA,
            "status": "OK",
            "equal": diff.getbbox() is None,
            "width": width,
            "height": height,
            "channels": 3,
            "pixel_count": width * height,
            "mae": mae,
            "rmse": rmse,
            "max_abs_diff": max_abs_diff,
            "mean_abs_diff": mean_abs_diff,
            "normalized_mean_abs_diff": round(mean_abs_diff / 255.0, 8),
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare two workspace images.")
    parser.add_argument("left")
    parser.add_argument("right")
    args = parser.parse_args(argv)
    try:
        left = _workspace_file(args.left)
        right = _workspace_file(args.right)
        result = compare(left, right)
    except (FileNotFoundError, OSError, ValueError) as exc:
        result = {
            "schema": SCHEMA,
            "status": "ERROR",
            "error": str(exc) or exc.__class__.__name__,
        }
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if result.get("status") == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
