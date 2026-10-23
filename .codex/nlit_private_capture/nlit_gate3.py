#!/usr/bin/env python3
"""SA-NLIT Gate3: operator-gated TEST-install configuration and LOCAL_TEST submission.

Runs with the already verified TEST bundled Python (source on isolated _pth).
No product source edits, network Telegram, credential copying or direct queue writes.
Private paths, URLs, nonce, prompts and logs are local-only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

PRODUCT = "309305b2eeeb79aed0925dc4dfee45af5193f8b5"


def canon(path: str) -> Path:
    p = Path(path).expanduser().resolve(strict=True)
    if not p.is_dir():
        raise RuntimeError("NOT_A_DIRECTORY")
    return p


def die(reason: str):
    raise RuntimeError("NLIT_GATE3_BLOCKED:" + reason)


def equal(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def children_or_equal(a: Path, b: Path) -> bool:
    return equal(a, b) or b in a.parents


def disjoint(a: Path, b: Path):
    if children_or_equal(a, b) or children_or_equal(b, a):
        die("ROOT_OVERLAP")


def verify_base(args):
    from agent_core.paths import install_root, telegram_config_path, telegram_pairing_path
    test = canon(args.test_install)
    prod = canon(args.production_install)
    work = canon(args.test_workspace)
    prod_work = canon(args.production_workspace)
    appdata = canon(args.test_appdata)
    prod_appdata = canon(args.production_appdata)
    private = canon(args.private_root)
    obs = canon(args.observer_dir)
    if not equal(install_root(), test):
        die("PYTHON_RUNNING_OUTSIDE_TEST_INSTALL")
    if telegram_config_path(test).is_file() or telegram_pairing_path(test).is_file():
        die("TEST_INSTALL_HAS_TELEGRAM_CREDENTIALS_OR_PAIRING")
    for a in (test, work, appdata, private, obs):
        for b in (prod, prod_work, prod_appdata):
            disjoint(a, b)
    for a, b in ((test, work), (test, obs), (test, private),
                 (work, obs), (work, private), (obs, private)):
        disjoint(a, b)
    if not (equal(appdata, private) or children_or_equal(appdata, private)
            or children_or_equal(private, appdata)):
        # Separation from the private root is permitted; all roots were checked above.
        pass
    if not equal(canon(os.environ.get("APPDATA", "") or "."), appdata):
        die("APPDATA_NOT_TEST_ROOT")
    if not equal(canon(os.environ.get("LOCALAPPDATA", "") or "."), appdata):
        die("LOCALAPPDATA_NOT_TEST_ROOT")
    from agent_core.browser_preferences import browser_launch_selection, load_browser_preferences
    selected = load_browser_preferences()
    b = browser_launch_selection(selected)
    if selected != {"browser": "edge", "instance": 5} or b["port"] != 12815:
        die("NOT_OFFICIAL_EDGE5_SELECTION")
    profile = Path(b["profile_dir"]).resolve()
    if not children_or_equal(profile, appdata):
        die("BROWSER_PROFILE_OUTSIDE_TEST_APPDATA")
    from agent_core.shared_browser import shared_browser_enabled
    if shared_browser_enabled():
        die("SHARED_BROWSER_MODE_NOT_ALLOWED")
    if os.environ.get("SMARTAGENT_ATTACH_CDP") == "1" or os.environ.get("SMARTAGENT_CHATGPT_CDP"):
        die("FOREIGN_CDP_ATTACH_ENV")
    return test, work, prod_work, private, profile


def binding_check(args):
    from agent_core.remote_binding import load, active
    from agent_core.workspace_access import require_writable_workspace
    test, work, _prod_work, _private, _profile = verify_base(args)
    binding = load(test)
    if not equal(canon(binding["workspace"]), work):
        die("SAVED_BINDING_NOT_TEST_WORKSPACE")
    if not equal(canon(require_writable_workspace(str(work))), work):
        die("TEST_WORKSPACE_NOT_AUTHORIZED")
    if active() is not None and not equal(canon(active()["workspace"]), work):
        die("ACTIVE_BINDING_NOT_TEST_WORKSPACE")
    from agent_core.conversation_registry import ConversationRegistry
    reg = ConversationRegistry()
    reg.load()
    matches = reg.list_remote_conversations(enabled_only=True)
    candidates = [x for x in matches if x.get("gpt_url") == binding["gpt_url"]
                  and equal(Path(x.get("workspace", "")).resolve(), work)]
    if len(candidates) < 1:
        die("TEST_REGISTRY_BINDING_MISSING")
    from agent_core.security_preflight import preflight
    status = preflight("remote", str(work))
    if status.get("status") not in ("PASS", "SOFTWARE_GUARD_ONLY"):
        die("TEST_WORKSPACE_SECURITY_PREFLIGHT_FAILED")
    if status.get("restricted_executor_required") and status.get("status") != "PASS":
        die("RESTRICTED_EXECUTOR_OS_ATTESTATION_REQUIRED")
    return binding


def command_bind(args):
    test, work, prod_work, private, profile = verify_base(args)
    from agent_core.remote_binding import load
    from agent_core.workspace_access import save_workspace_access_policy
    from agent_core.workspace import normalize_web_conversation_url
    old = None
    try:
        old = load(test)
    except FileNotFoundError:
        pass
    if old and not equal(canon(old["workspace"]), work):
        die("NON_TEST_BINDING_PRESENT_REFUSE_OVERWRITE")
    raw = input("Enter FRESH authenticated TEST conversation URL (kept local): ").strip()
    if not re.fullmatch(r"https://chatgpt[.]com/c/[A-Za-z0-9-]+/?", raw):
        die("NEW_TEST_CHATGPT_CONVERSATION_URL_REQUIRED")
    url = normalize_web_conversation_url(raw)
    production_binding_path = args.production_install
    from agent_core.paths import remote_binding_path
    original = remote_binding_path(canon(production_binding_path))
    if original.is_file():
        original_binding = json.loads(original.read_text(encoding="utf-8"))
        if original_binding.get("gpt_url") == url:
            die("LIVE_CONVERSATION_REUSE_FORBIDDEN")
    skills = private / "nlit-test-skills"
    skills.mkdir(mode=0o700, exist_ok=True)
    # Uses production-supported workspace authorization persistence on TEST ONLY.
    save_workspace_access_policy([str(work)], [str(prod_work)], [str(prod_work)])
    from RemoteAgent.remote_workspace import cmd_set
    from argparse import Namespace
    cmd_set(Namespace(workspace=str(work), url=url, skill_path=str(skills),
                          enabled=True, poll_profile="normal"))
    bound = binding_check(args)
    if bound["gpt_url"] != url:
        die("BINDING_POSTWRITE_MISMATCH")
    print("NLIT_GATE3_FORMAL_TEST_BINDING_PASS")


def command_verify(args):
    binding_check(args)
    print("NLIT_GATE3_TEST_WORKSPACE_BINDING_PASS")


def command_open_browser(args):
    _, _, _, _, profile = verify_base(args)
    from playwright.sync_api import sync_playwright
    from agent_core.web_ui.manual_browser import ensure_agent_browser
    with sync_playwright() as pw:
        endpoint, created = ensure_agent_browser(pw, headless=False)
        if endpoint != "http://127.0.0.1:12815":
            die("BROWSER_ENDPOINT_NOT_OFFICIAL_EDGE5")
        # Race guard: re-attest the actual listener *before* touching CDP.
        # The PowerShell controller checks both before and after this method.
        import subprocess
        ps_guard = (
            "$ErrorActionPreference='Stop'; "
            "$ls=@(Get-NetTCPConnection -State Listen -LocalPort 12815 -ErrorAction Stop); "
            "if($ls.Count -lt 1){exit 21}; "
            "$ids=@($ls | Select-Object -ExpandProperty OwningProcess -Unique); "
            "if($ids.Count -ne 1){exit 22}; "
            "$proc=Get-CimInstance Win32_Process -Filter ('ProcessId='+[int]$ids[0]); "
            "if($null -eq $proc -or $proc.Name -ine 'msedge.exe'){exit 23}; "
            "$cmd=[string]$proc.CommandLine; "
            "$profile=$env:NLIT_GATE3_EXPECTED_PROFILE; "
            "if($cmd.IndexOf($profile,[StringComparison]::OrdinalIgnoreCase) -lt 0 "
            "-or $cmd.IndexOf('--remote-debugging-port=12815',[StringComparison]::OrdinalIgnoreCase) -lt 0){exit 24}; "
            "foreach($entry in $ls){if($entry.LocalAddress -notin @('127.0.0.1','::1')){exit 25}}"
        )
        child_env = dict(os.environ, NLIT_GATE3_EXPECTED_PROFILE=str(profile))
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_guard],
            capture_output=True, text=True, timeout=15, env=child_env, check=False,
        )
        if result.returncode != 0:
            die("EDGE5_PREATTACH_OWNER_NOT_VERIFIED")
        browser = pw.chromium.connect_over_cdp(endpoint)
        if not browser.contexts:
            die("NEW_EDGE5_CONTEXT_MISSING")
        page = browser.contexts[0].new_page()
        page.goto("https://chatgpt.com/", wait_until="commit", timeout=60000)
        page.bring_to_front()
        # Intentionally disconnect without closing the detached persistent Edge.
    print("NLIT_GATE3_BROWSER_WINDOW_OPEN; MANUAL_LOGIN_UNVERIFIED")
    print("NLIT_GATE3_TEST_PROFILE_PATH_CONFIRMED")


def command_sender(args):
    test, work, _prod_work, private, _profile = verify_base(args)
    binding_check(args)
    if not re.fullmatch(r"NLIT-NEW-R3-[A-Z0-9-]{4,64}", args.case_id):
        die("CASE_ID_NOT_NEW_R3")
    candidate = Path(args.case_file).resolve(strict=True)
    if not candidate.is_file() or private not in candidate.parents:
        die("CASE_FILE_OUTSIDE_PRIVATE_ROOT")
    if candidate.is_symlink():
        die("CASE_FILE_SYMLINK")
    body = candidate.read_text(encoding="utf-8").strip()
    if not body or len(body) > 10000 or "NLIT_CASE_ID=" in body or "NLIT_TRACE_TOKEN=" in body:
        die("INVALID_PRIVATE_CASE_BODY")
    nonce_file = private / "gate3_capture_nonce.txt"
    nonce = nonce_file.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", nonce):
        die("CAPTURE_NONCE_INVALID")
    from agent_core.paths import remote_supervisor_state_path
    supervisor_file = remote_supervisor_state_path(test)
    if not supervisor_file.is_file():
        die("TEST_SUPERVISOR_STATE_MISSING")
    row = json.loads(supervisor_file.read_text(encoding="utf-8"))
    if row.get("status") != "RUNNING" or int(row.get("pid") or 0) <= 0:
        die("TEST_SUPERVISOR_NOT_RUNNING")
    if time.time() - float(row.get("heartbeat_at") or 0) > 15:
        die("TEST_SUPERVISOR_HEARTBEAT_STALE")
    from RemoteAgent.local_telegram_sender import send_and_wait
    request = "NLIT_TRACE_TOKEN=" + nonce + "\nNLIT_CASE_ID=" + args.case_id + "\n" + body
    # Real TelegramReceiver -> Gateway -> test-only durable queue -> supervisor/worker.
    return send_and_wait(request=request, workspace=str(work), timeout_sec=args.timeout,
                         root=test)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["bind", "verify", "open-browser", "send"])
    for name in ("test-install", "production-install", "test-workspace",
                 "production-workspace", "observer-dir", "private-root",
                 "test-appdata", "production-appdata"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--case-id", default="")
    parser.add_argument("--case-file", default="")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    method = {"bind": command_bind, "verify": command_verify,
              "open-browser": command_open_browser, "send": command_sender}[args.mode]
    result = method(args)
    return 0 if result is None else int(result)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print("NLIT_GATE3_BLOCKED:" + type(exc).__name__ + ":" + str(exc), file=sys.stderr)
        raise SystemExit(3)
