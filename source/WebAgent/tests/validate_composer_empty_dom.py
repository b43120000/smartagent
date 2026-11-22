"""Native DOM regression using an isolated page on a supplied CDP browser."""
import argparse
from pathlib import Path
import sys
from types import SimpleNamespace as NS
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_core.composer_operation import _composer_is_empty


def run(endpoint):
    from playwright.sync_api import sync_playwright
    assert not _composer_is_empty(NS(_composer_locator=lambda: None))
    def unreadable(): raise RuntimeError("read failed")
    assert not _composer_is_empty(NS(_composer_locator=lambda: NS(
        evaluate=lambda _: unreadable())))
    cases = [
        ("<div contenteditable=true></div>", True),
        ('<div contenteditable=true><p><br class="ProseMirror-trailingBreak"></p></div>', True),
        ("<div contenteditable=true><p></p></div>", True),
        ("<textarea></textarea>", True),
        ("<textarea> </textarea>", False),
        ("<div contenteditable=true><p> </p></div>", False),
        ("<div contenteditable=true><p>&nbsp;</p></div>", False),
        ("<div contenteditable=true><p>文字</p></div>", False),
        ("<div contenteditable=true><p><br></p></div>", False),
        ('<div contenteditable=true><p><br class="ProseMirror-trailingBreak"></p><p></p></div>', False),
        ('<div contenteditable=true><p><br><br class="ProseMirror-trailingBreak"></p></div>', False),
        ('<div contenteditable=true><img src="about:blank"></div>', False),
    ]
    with sync_playwright() as p:
        b=p.chromium.connect_over_cdp(endpoint,timeout=10000)
        page=b.contexts[0].new_page()
        try:
            for html,expected in cases:
                page.set_content(html)
                editor=page.locator("textarea,div[contenteditable]")
                actual=_composer_is_empty(NS(_composer_locator=lambda:editor))
                assert actual is expected,(html,actual,expected)
        finally:
            page.close()
    print(f"PASS: {len(cases)} native DOM cases plus missing/read-failure checks")

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--cdp",required=True)
    run(parser.parse_args().cdp)
