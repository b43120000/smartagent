"""Strict read-only OFFLINE preflight for a detached 309305b2 test installation.

No browser, CDP, supervisor, task or writer is ever started by this command.
All report output is allowlisted; absolute paths, nonce and prompt remain local.
Run in the separately provisioned Python interpreter; return 0 only on PASS.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


BASELINE = "309305b2eeeb79aed0925dc4dfee45af5193f8b5"


def normalized(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve(strict=True)))


def distinct(a: str, b: str) -> bool:
    a, b = normalized(a), normalized(b)
    try:
        common = os.path.commonpath((a, b))
        return common not in (a, b)
    except ValueError:
        return True  # different Windows drive roots


def git_full_sha(git: str, root: Path) -> str:
    p = subprocess.run([git, "-C", str(root), "rev-parse", "HEAD"],
                       text=True, capture_output=True, check=True)
    return p.stdout.strip()


def check_git_clean(git: str, root: Path) -> bool:
    p = subprocess.run([git, "-C", str(root), "status", "--porcelain",
                        "--untracked-files=no"], text=True,
                       capture_output=True, check=True)
    return not p.stdout.strip()


def check_manifest(observer: Path) -> tuple[bool, int]:
    data = json.loads((observer / "artifact_manifest.json").read_text(encoding="utf-8"))
    files = data.get("files", {})
    if data.get("product_baseline_sha") != BASELINE or not files:
        return False, 0
    for relative, want in files.items():
        if "/" in relative or "\\" in relative or relative.startswith("."):
            return False, 0
        file = observer / relative
        if not file.is_file() or file.is_symlink():
            return False, 0
        actual = hashlib.sha256(file.read_bytes()).hexdigest()
        if not re.fullmatch("[0-9a-f]{64}", str(want)) or actual != want:
            return False, 0
    return True, len(files)


def hook_probe(python: Path, installation: Path, observer: Path, private: Path) -> bool:
    script = """
import json, sitecustomize, inspect
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.browser_client import WebAgentBrowserClient
from agent_core.web_runtime import WebLLMScraper
import private_capture
methods = [
    WebAgentProtocolLoop._run_impl,
    WebAgentBrowserClient.ask,
    WebLLMScraper._write_prompt_to_composer,
    WebLLMScraper._log_stage,
]
signatures = [
    inspect.signature(WebAgentProtocolLoop.run),
    inspect.signature(WebAgentProtocolLoop._run_impl),
    inspect.signature(WebAgentBrowserClient.ask),
    inspect.signature(WebLLMScraper.ask),
]
print(json.dumps({
    'sitecustomize': bool(sitecustomize.__file__),
    'all_wrapped': all(hasattr(m, '__wrapped__') for m in methods),
    'run_fields': all(x in str(signatures[0]) for x in ('request_id', 'task_id', 'task_epoch')),
    'impl_fields': all(x in str(signatures[1]) for x in ('request_id', 'task_id', 'task_epoch')),
    'client_fields': all(x in str(signatures[2]) for x in ('protocol_expected', 'stage')),
    'scraper_fields': 'protocol_expected' in str(signatures[3]),
    'hook_families': sorted(set(k[0] for k in private_capture._PATCHED))
}, sort_keys=True))
"""
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": os.pathsep.join((str(installation / "source"), str(observer))),
        "SMARTAGENT_REMOTE_WORKER": "1",
        "SMARTAGENT_NLIT_CAPTURE_ENABLED": "1",
        "SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED": "YES",
        "SMARTAGENT_NLIT_CAPTURE_ROOT": str(private),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    p = subprocess.run([str(python), "-c", script], cwd=installation, env=env,
                       capture_output=True, text=True, timeout=45)
    if p.returncode:
        return False
    try:
        answer = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False
    return (all(answer.get(k) is True for k in (
        "sitecustomize", "all_wrapped", "run_fields",
        "impl_fields", "client_fields", "scraper_fields"
    )) and answer.get("hook_families") == [
        "browser_client", "protocol", "scraper"
    ])


def main() -> int:
    p = argparse.ArgumentParser(description="Read-only SmartAgent NLIT offline preflight")
    for field in ("test-install", "production-install", "test-workspace",
                  "production-workspace", "observer-dir", "private-dir",
                  "test-appdata", "production-appdata"):
        p.add_argument("--" + field, type=Path, required=True)
    p.add_argument("--git", default=os.environ.get("SMARTAGENT_NLIT_GIT_EXE", "git"))
    args = p.parse_args()
    problems = []
    paths = {name: getattr(args, name.replace("-", "_")) for name in (
        "test-install", "production-install", "test-workspace",
        "production-workspace", "observer-dir", "private-dir",
        "test-appdata", "production-appdata"
    )}
    try:
        if not all(v.is_dir() for v in paths.values()):
            problems.append("ONE_OR_MORE_REQUIRED_DIRECTORIES_MISSING")
        # No mutation to the live install, workspace, browser profile or writer state.
        pairs = (("test-install", "production-install"),
                 ("test-workspace", "production-workspace"),
                 ("test-workspace", "production-install"),
                 ("test-install", "production-workspace"),
                 ("test-workspace", "test-install"),
                 ("private-dir", "test-install"),
                 ("private-dir", "test-workspace"),
                 ("observer-dir", "test-install"),
                 ("observer-dir", "production-install"),
                 ("test-appdata", "production-appdata"),
                 ("test-appdata", "production-install"),
                 ("test-appdata", "production-workspace"))
        for left, right in pairs:
            if not distinct(paths[left], paths[right]):
                problems.append("ISOLATION_NOT_PROVEN_" + left.replace("-", "_") +
                                "_" + right.replace("-", "_"))
        install = paths["test-install"].resolve()
        observer = paths["observer-dir"].resolve()
        private = paths["private-dir"].resolve()
        python = install / ".venv" / "Scripts" / "python.exe"
        if not python.is_file():
            problems.append("ISOLATED_PYTHON_MISSING")
        if git_full_sha(args.git, install) != BASELINE:
            problems.append("PRODUCT_HEAD_NOT_BASELINE")
        if not check_git_clean(args.git, install):
            problems.append("TRACKED_TREE_DIRTY")
        valid, count = check_manifest(observer)
        if not valid:
            problems.append("OBSERVER_ARTIFACT_DIGEST_MISMATCH")
        if not problems and not hook_probe(python, install, observer, private):
            problems.append("REAL_WORKER_IMPORT_HOOK_NOT_INSTALLED")
    except Exception as exc:
        # No private paths or exception details in console output.
        problems.append("PREFLIGHT_EXCEPTION_" + type(exc).__name__)
    print(json.dumps({
        "schema": "NLIT_OFFLINE_ISOLATION_PREFLIGHT_V1",
        "product_head_expected": BASELINE,
        "product_head_confirmed": "PRODUCT_HEAD_NOT_BASELINE" not in problems
                                 and not any(s.startswith("PREFLIGHT_EXCEPTION") for s in problems),
        "observer_artifact_files_verified": locals().get("count", 0),
        "browser_started": False,
        "runtime_task_submitted": False,
        "test_writer_accessed": False,
        "existing_agent_touched": False,
        "loaded_live_process_sha": "UNVERIFIED",
        "status": "PASS" if not problems else "BLOCKED",
        "reasons": sorted(set(problems)),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if not problems else 2


if __name__ == "__main__":
    raise SystemExit(main())
