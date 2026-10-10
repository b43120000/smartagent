"""Close every web page exposed by SmartAgent-owned local CDP endpoints."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import urlopen

from .paths import agent_host_state_path, remote_runtime_state_path


DEFAULT_CHATGPT_CDP_ENDPOINT = "http://127.0.0.1:1272"
PAGE_TARGET_TYPES = frozenset({"page", "webview"})


def _local_endpoint(value: object) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    if not endpoint:
        return ""
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return ""
    if parsed.port is None:
        return ""
    return endpoint


def _state_endpoint(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return _local_endpoint(payload.get("cdp_endpoint") or payload.get("cdp"))


def discover_endpoints(root: str | Path | None = None) -> tuple[str, ...]:
    candidates = {
        DEFAULT_CHATGPT_CDP_ENDPOINT,
        _state_endpoint(agent_host_state_path(root)),
        _state_endpoint(remote_runtime_state_path(root)),
    }
    return tuple(sorted(endpoint for endpoint in candidates if endpoint))


def _read_json(url: str, timeout_sec: float) -> object:
    with urlopen(url, timeout=timeout_sec) as response:
        return json.loads(response.read().decode("utf-8"))


def close_endpoint_pages(endpoint: str, *, timeout_sec: float = 3.0) -> dict:
    safe_endpoint = _local_endpoint(endpoint)
    if not safe_endpoint:
        return {"endpoint": str(endpoint), "status": "REJECTED", "closed": 0}
    try:
        targets = _read_json(safe_endpoint + "/json/list", timeout_sec)
    except (OSError, HTTPError, URLError, ValueError, json.JSONDecodeError) as exc:
        return {
            "endpoint": safe_endpoint,
            "status": "UNAVAILABLE",
            "closed": 0,
            "detail": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(targets, list):
        return {
            "endpoint": safe_endpoint,
            "status": "INVALID_RESPONSE",
            "closed": 0,
        }

    closed = 0
    failed: list[dict[str, str]] = []
    matched = 0
    for target in targets:
        if not isinstance(target, dict):
            continue
        if str(target.get("type", "")).lower() not in PAGE_TARGET_TYPES:
            continue
        target_id = str(target.get("id", "") or "").strip()
        if not target_id:
            continue
        matched += 1
        close_url = safe_endpoint + "/json/close/" + quote(target_id, safe="")
        try:
            with urlopen(close_url, timeout=timeout_sec) as response:
                response.read()
            closed += 1
        except (OSError, HTTPError, URLError) as exc:
            failed.append({
                "id": target_id,
                "detail": f"{type(exc).__name__}: {exc}",
            })

    return {
        "endpoint": safe_endpoint,
        "status": "PASS" if not failed else "PARTIAL",
        "matched": matched,
        "closed": closed,
        "failed": failed,
    }


def close_all_web_pages(
    root: str | Path | None = None,
    *,
    endpoints: Iterable[str] | None = None,
    timeout_sec: float = 3.0,
) -> dict:
    selected = tuple(endpoints) if endpoints is not None else discover_endpoints(root)
    results = [
        close_endpoint_pages(endpoint, timeout_sec=timeout_sec)
        for endpoint in selected
    ]
    return {
        "status": (
            "PASS"
            if all(item["status"] in {"PASS", "UNAVAILABLE"} for item in results)
            else "PARTIAL"
        ),
        "endpoint_count": len(results),
        "closed_page_count": sum(int(item.get("closed", 0)) for item in results),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Close all pages owned by SmartAgent browser endpoints."
    )
    parser.add_argument("--root", default="")
    parser.add_argument("--timeout-sec", type=float, default=3.0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        assert _local_endpoint("http://127.0.0.1:1272")
        assert _local_endpoint("http://localhost:9222")
        assert not _local_endpoint("https://example.com:443")
        assert not _local_endpoint("file:///tmp/browser")
        print("CLOSE_WEB_PAGES_SELF_TEST_OK")
        return 0

    report = close_all_web_pages(
        args.root or None,
        timeout_sec=max(0.5, float(args.timeout_sec)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
