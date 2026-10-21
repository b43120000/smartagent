# ChatGPT CDP startup recovery (2026-10-22)

`page_starting` means the startup lease has been acquired. The reported incident then timed out twice in `Page.goto(wait_until="commit")`; the driver disconnected on the third attempt. Read-only inspection later found the requested conversation loaded and editable. This fix addresses the navigation wait/retry path, not the separately observed driver termination.

For a pinned ChatGPT conversation over CDP, schedule `location.assign` after returning from JavaScript evaluation and poll provider-aware conversation identity plus a visible provider-owned composer. Refresh the compatibility probe during initial rendering: an early `composer_missing` result must not remain cached for the rest of startup. Existing matching conversations are observed without reloading. Preserve a reached target through a readiness timeout so the next attempt can reuse it. Driver-disconnect errors remain visible. Recognized login pages return to the existing human-login flow; after login the same readiness check runs without another load-event wait. Other-provider and isolated-worker navigation paths are unchanged.

Verification:

- 13 focused startup regressions PASS, including scheduled navigation, project/plain URL aliases, wrong provider/conversation rejection, delayed/hidden composer, refreshed compatibility, context replacement, closed page, driver error propagation and full-start retry reuse with manual login (mocked CDP integration).
- Browser preferences/managed routing, remote provider binding and focused startup: 31 tests PASS together. The routing fixture mocks the new readiness boundary; readiness is tested separately above.
- Five shared-submit identity tests PASS in a real isolated headless Chromium against fixture pages. These are fixture IT, not external website tests.
- Real Chrome/CDP ChatGPT Web IT PASS: newly created test tab reached the existing target conversation and visible composer in 2.27 seconds, sent zero messages, then closed only that test tab. All original tabs were preserved. Evidence remains in ignored `localdata/attached_chatgpt_startup_web_it.json`.

Reproduce the live check with the package Python, `PYTHONPATH=source`, and `source/WebAgent/tests/live_attached_chatgpt_startup.py --endpoint http://127.0.0.1:12801 --output localdata/attached_chatgpt_startup_web_it.json`. It requires an already authenticated browser with an existing ChatGPT conversation open.

Additional legacy checks were not counted as PASS: `validate_chatgpt_edge_launch.py` expects persistent-context launches superseded by the managed browser path; `validate_web_ui_provider_boundary.py` rejects provider names already present in the unchanged base adapter. These unrelated older checks require separate maintenance. No prompt submission/end-to-end model-response test was performed for this navigation fix.

## Composer confirmation follow-up

The 07:43 initialization reached PAGE_READY and wrote all 31,065 characters. Its independent observer failed and the generic UI label obscured the distinction. Read-only reproduction showed CDP attachment timing out at five seconds. Connection failures now return controlled CDP_CONNECT_TIMEOUT/CDP_CONNECTION_CLOSED codes; the bounded helper reserves connection time within its parent deadline (up to 12 seconds). The read-only observer has a 15-second minimum process budget and retries a connection failure at most once; it never retries a writer or bypasses draft/turn verification. Composer failure has a separate UI status stating the page opened but the draft was not confirmed and remains unsent.

Validation: 36 focused startup/observer/provider/routing UT PASS. Existing validate_composer_operation.py PASS, including real isolated Python child timeout/cancellation cleanup, quarantine and no replay. Its synthetic scraper fixture now supplies a CDP endpoint, so the prewrite assertion actually reaches the mocked operation. No real website test was run for this final change, at the user's request; manual verification is pending. The preexisting quarantined journal and user draft were preserved.
