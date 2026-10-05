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
