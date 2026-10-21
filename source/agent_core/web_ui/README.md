# Web UI boundary

`web_ui` is the anti-corruption layer between volatile provider DOM and stable
SmartAgent workflow state. Provider selection is explicit and fail-closed:

1. `factory.py` constructs exactly the requested provider adapter.
2. `providers/<provider>/profiles.py` owns versioned DOM selectors.
3. `providers/<provider>/composer.py` reverses the provider editor DOM into a
   stable `ComposerSnapshot` and requires full-content equality before submit.
4. `providers/<provider>/adapter.py` translates DOM into stable `TurnRef`,
   `RequestScope`, `ActivitySnapshot`, and final-text observations.
5. `request_scope.py`, `web_runtime.py`, and validators make workflow decisions
   only from the provider-neutral contract.

`base_adapter.py` owns the provider-neutral request-scope, turn-ownership,
activity, media, and normalized-state algorithms. ChatGPT, Gemini, and Claude
adapters inherit this base directly; provider adapters never inherit or invoke
another provider adapter.

Every adapter exposes `authentication_state()` as a normalized
`AUTHENTICATED`, `LOGIN_REQUIRED`, or `UNKNOWN` result. Browser startup and
`adapterUI` consume this state before compatibility probing or calibration.

ChatGPT, Gemini, and Claude are registered providers. Provider metadata,
factory construction, profile-store eligibility, and calibration eligibility
come from `provider_registry.py`. There is no cross-provider selector fallback.
Gemini uses its own bootstrap/calibrated profiles and provider package; unknown
providers remain fail-closed.

`adapterUI.bat` provides a bounded calibration workflow for ChatGPT, Gemini,
and Claude.
It gives a working Planner page structural Target DOM evidence (never message
text, URLs, cookies, or browser storage), accepts only a strict CSS-selector
JSON proposal, and validates that proposal on the Target page. A versioned
profile is published to `localdata/persistent/web_ui_profiles/<provider>` only
after a live composer, send, user-turn, assistant-turn, final-content, and idle
probe succeeds. Runtime adapters load that active data profile through this
same boundary and fall back only to their own built-in provider profile when
no valid active profile exists. Use `adapterUI.bat --rollback chatgpt`,
`adapterUI.bat --rollback gemini`, or `adapterUI.bat --rollback claude` to
restore the previous validated profile.

For each submit, the runtime captures a `RequestScope`, binds the newly rendered
user turn by prompt content, and accepts only an assistant turn that is both new
to the scope and located after that user turn. After reload, the user anchor is
rebound semantically before assistant output is considered.

When a provider changes its UI, update only that provider package. Do not spread
replacement selectors into workflow, protocol, transport, or task code. The
top-level `profiles.py`, `adapter.py`, `composer.py`, `compatibility.py`, and
`bridge.py` files are temporary compatibility exports for existing callers.

Composer verification first compares the visible editor value. If renderer
layout changes that value, the second layer joins semantic top-level blocks to
invert the browser rendering step. It still requires complete content equality;
length, substring, and arbitrary whitespace deletion are diagnostic-only or
explicitly rejected.

The existing attachment transaction DOM and the paused `meta_recovery` workflow
are migration boundaries for a later milestone; their behavior is intentionally
not redesigned as part of the request/response ownership change.

## Manual file DOM calibration (11–15)

The numbered launchers in `adapterUI` continue the same configured shared browser
and login used by stages1–10. Each launcher owns only its newly opened tab.

| Step | Launcher | Functional acceptance |
| --- | --- | --- |
| 11 | `11.attachmentEntry.bat` | Select the composer attachment button; one click must reveal a new upload menu/input or a file chooser. An already-present hidden input alone is insufficient. |
| 12 | `12.fileUpload.bat` | Select the local-upload menu item; capture the real file chooser, derive its file-input selector and select a tiny generated fixture. Upload completion is verified separately in13. |
| 13 | `13.attachmentState.bat` | Select the composer attachment card including filename and remove button; save preliminary selectors and check removal. Positive busy→idle and exercised error evidence are still required before publishing completion capability. |
| 14 | `14.downloadArtifact.bat` | Upload an owned fixture, send once, prove the model read a content marker absent from both filename and prompt; select the generated download control inside the fresh owned answer, download and compare file content. |
| 15 | `15.fileRoundtripIT.bat` | Repeat upload/read/create/download using saved selectors, with no new DOM selection; exact decoded UTF-8 text and final newline must match. |

Every selection panel uses the same three status meanings: red invalid, yellow
preflight pending functional verification, green functional verification passed.
Generated fixtures and downloads stay under `localdata/manual_dom_file_it`, and
evidence stays under `localdata/logs/manual_dom_it`. Tests never upload workspace
files. Download destinations are unique and existing files are never overwritten.

Provider-owned `dom_config.json.files` contains selectors and independent verified
proofs; these fields do not alter frozen dialogue profiles. Changing relevant
selectors invalidates dependent file proofs. Existing1–10 saves preserve file
selectors and invalidate the complete file-roundtrip proof when dialogue changes.
The Agent attachment transaction consumes these same selectors through adapter
methods. The calibrated download runtime requires the current request's bound
user/fresh assistant scope and unchanged page URL, and consumes a successful
download scope once. A new ask, even one failing before submit, retires that scope.

Native file creation/attachment support varies by website/account. A text answer,
code block, missing download, login challenge or quota failure does not pass14/15.
No automatic resubmission is performed. Live results must be reported per site;
controlled browser fixtures are not evidence that a model website supports files.

Current implementation boundary: stage13 saves `attachment_pending`, not
`attachment_verified`, because filename stability and absent generic indicators
are insufficient positive completion/error evidence. A reachable manual capture
flow for those positive signals remains to be implemented; stage14/15 therefore
remain blocked for newly calibrated websites. Downstream controlled fixture tests
explicitly install fixture-only capability proofs and do not establish stage13
website verification. No live11–15 site has passed in this implementation run.
