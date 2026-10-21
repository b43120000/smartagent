# ChatGPT CDP startup recovery (2026-10-22)

`page_starting` means the startup lease has been acquired. The reported incident then timed out twice in `Page.goto(wait_until="commit")`; the driver disconnected on the third attempt. Read-only inspection later found the requested conversation loaded and editable. This fix addresses the navigation wait/retry path, not the separately observed driver termination.

For a pinned ChatGPT conversation over CDP, schedule `location.assign` after returning from JavaScript evaluation and poll provider-aware conversation identity plus a visible provider-owned composer. Refresh the compatibility probe during initial rendering: an early `composer_missing` result must not remain cached for the rest of startup. Existing matching conversations are observed without reloading. Preserve a reached target through a readiness timeout so the next attempt can reuse it. Driver-disconnect errors remain visible. Other-provider and isolated-worker navigation paths are unchanged.

Verification:

- 12 focused startup regressions PASS, including scheduled navigation, project/plain URL aliases, wrong provider/conversation rejection, delayed/hidden composer, refreshed compatibility, context replacement, closed page, driver error propagation and full-start retry reuse (mocked CDP integration).
- Browser preferences/managed routing, remote provider binding and focused startup: 30 tests PASS together. The routing fixture mocks the new readiness boundary; readiness is tested separately above.
- Five shared-submit identity tests PASS in a real isolated headless Chromium against fixture pages. These are fixture IT, not external website tests.
- Real Chrome/CDP ChatGPT Web IT PASS: newly created test tab reached the existing target conversation and visible composer in 2.27 seconds, sent zero messages, then closed only that test tab. All original tabs were preserved. Evidence remains in ignored `localdata/attached_chatgpt_startup_web_it.json`.

Reproduce the live check with the package Python, `PYTHONPATH=source`, and `source/WebAgent/tests/live_attached_chatgpt_startup.py --endpoint http://127.0.0.1:12801 --output localdata/attached_chatgpt_startup_web_it.json`. It requires an already authenticated browser with an existing ChatGPT conversation open.

Additional legacy checks were not counted as PASS: `validate_chatgpt_edge_launch.py` expects persistent-context launches superseded by the managed browser path; `validate_web_ui_provider_boundary.py` rejects provider names already present in the unchanged base adapter. These unrelated older checks require separate maintenance. No prompt submission/end-to-end model-response test was performed for this navigation fix.
