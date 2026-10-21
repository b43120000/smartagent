"""Real-site IT for adapterUI/1.writeSendBox.bat (one tab, capture + verify)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

from ..dom_config import load_dom_config
from ..provider_registry import provider_from_url
from ..manual_browser import active_agent_endpoint
from .live_composer_it import driver_context


def run(url: str) -> dict:
    from ...paths import log_root
    provider = provider_from_url(url)
    app_root = Path(__file__).resolve().parents[4]
    evidence = log_root() / "manual_dom_it" / ("unified_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    evidence.mkdir(parents=True, exist_ok=True)
    report = {"provider": provider, "passed": False, "real_site": True, "submitted": False}
    process = None
    try:
        with driver_context(provider, "") as (context, endpoint):
            original_pages = [page for page in context.pages if page not in context._manual_it_bootstrap_pages]
            opened_pages = []
            context.on("page", lambda page: opened_pages.append(page))
            automatic_agent_connection = endpoint == active_agent_endpoint(provider)
            args = ["cmd.exe", "/c", str(app_root / "adapterUI" / "1.writeSendBox.bat"),
                    "--url", url, "--no-wait",
                    "--screenshot", str(evidence / "02_verified.png")]
            if not automatic_agent_connection:
                args += ['--cdp-endpoint', endpoint]
            env = dict(os.environ, SMARTAGENT_TEST_NO_PAUSE="1", PYTHONIOENCODING="utf-8")
            with context.expect_page(timeout=60000) as opened:
                process = subprocess.Popen(args, cwd=app_root, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, encoding="utf-8")
            page = opened.value
            panel = page.locator("#smartagent-composer-picker")
            panel.wait_for(state="visible", timeout=60000)
            candidates = page.locator('textarea:visible,[contenteditable="true"]:visible,[contenteditable="plaintext-only"]:visible')
            candidates.first.wait_for(state="visible", timeout=60000)
            targets = [item for item in candidates.all() if item.is_editable()]
            if len(targets) != 1:
                raise AssertionError(f"live_it_ambiguous_composer:{len(targets)}")
            # Match the time a human takes to inspect the page before selecting;
            # M365 initially renders a server editor before mounting Lexical.
            page.wait_for_timeout(2000)
            # SSR can replace the editor while the test is waiting. Resolve the
            # live editable again rather than using a locator's stale ordinal.
            candidates = page.locator('textarea:visible,[contenteditable="true"]:visible,[contenteditable="plaintext-only"]:visible')
            targets = [item for item in candidates.all() if item.is_editable()]
            if len(targets) != 1:
                raise AssertionError(f"live_it_ambiguous_mounted_composer:{len(targets)}")
            panel.get_by_role("button", name="開始選取", exact=True).click()
            targets[0].click()
            page.wait_for_function('() => !!window.__smartAgentComposerPicker?.selected', timeout=5000)
            selection = page.evaluate("() => window.__smartAgentComposerPicker.selected")
            if not selection or not targets[0].evaluate("(el,s)=>document.querySelector(s)===el", selection["selector"]):
                raise AssertionError("selected_wrong_composer")
            page.screenshot(path=str(evidence / "01_selected.png"))
            panel.get_by_role("button", name="儲存輸入框").click()
            if page.evaluate("() => window.__smartAgentComposerPicker?.action") != "save":
                page.screenshot(path=str(evidence / "save_rejected.png"))
                raise AssertionError("picker_save_rejected:" + panel.inner_text())
            output, _ = process.communicate(timeout=60)
            (evidence / "launcher.log").write_text(output, encoding="utf-8")
            if process.returncode or "RESULT=CALIBRATED_AND_VERIFIED" not in output:
                raise AssertionError(f"unified_batch_exit={process.returncode}: {output[-2000:]}")
            result = next(json.loads(line[line.index('{'):]) for line in output.splitlines() if line.startswith('[writeSendBox] {'))
            if not result["passed"] or not result["runtime_config_loaded"] or not result["roundtrip_match"]:
                raise AssertionError("verification_not_completed")
            if len(opened_pages) != 1 or any(item.is_closed() for item in original_pages):
                raise AssertionError("unified_workflow_page_ownership_failed")
            if load_dom_config(provider)["elements"]["composer"]["selector"] != selection["selector"]:
                raise AssertionError("saved_config_mismatch")
            report.update(result, passed=True, unified_batch_completed=True, verified_in_same_page=True,
                          opened_page_count=len(opened_pages), shared_tabs_preserved=True,
                          automatic_agent_connection=automatic_agent_connection, agent_endpoint=endpoint)
        return report
    except Exception as exc:
        report["passed"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if process is not None and not (evidence / "launcher.log").exists():
            try:
                output, _ = process.communicate(timeout=5)
                (evidence / "launcher.log").write_text(output, encoding="utf-8")
                print(output[-1600:], flush=True)
            except subprocess.TimeoutExpired:
                pass
        if process is not None and process.poll() is None:
            process.terminate()
        (evidence / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({**report, "evidence": str(evidence)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    run(parser.parse_args().url)
