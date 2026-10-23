# WebAgent shared short protocol readiness compatibility

**Scope:** WebAgent bootstrap only. This is a standalone fix branch based on
the verified `main` baseline, separate from Runtime Evidence Foundation.

## Problem

Some Web LLM providers return exactly:

```text
[AGENT_PROTOCOL_READY]
```

instead of the full `AGENT_PROTOCOL_READY` JSON identity block.
Previously this response raised:

```text
RuntimeError: WebGPT 未回傳 matching WEBAGENT protocol readiness
```

before any task action could run.

## New acceptance rule

On a **Runtime-initiated bootstrap**:

- The original full identity block (protocol name, version, hash, session ID)
  remains supported and checked exactly.
- A complete assistant response consisting only of
  `[AGENT_PROTOCOL_READY]` (optional surrounding whitespace) is accepted.
- Bare text `AGENT_PROTOCOL_READY`, quoted markers, code fences, other
  markers and markers embedded in prose remain rejected.
- `[AGENT_SESSION_READY]` still **must** include matching structured
  identity and the fresh handshake nonce; short session attach replies
  never succeed. Failure to attach triggers a fresh bootstrap.
- The shared logic runs in `WebAgent.session.ensure_webagent_session`
  for **ChatGPT, Gemini, Claude, M365 and Other URL**, regardless of the
  provider DOM implementation. The shared `SessionProtocol` parser has
  an explicit `allow_marker_only` argument, which defaults to `False`
  for other callers.

## Assurance / security boundary

`[AGENT_PROTOCOL_READY]` alone does **not** cryptographically attest that
the provider retained the current protocol body, hash or session ID. It
only acknowledges the immediately preceding bootstrap in the current
request-scoped response channel. The Runtime stores:

- `readiness_mode=FULL_IDENTITY` for a matching structured identity.
- `readiness_mode=MARKER_ONLY` for the exact abbreviated reply.

These modes persist in the common conversation registry and are not
interchangeable proof levels. Tools still pass normal per-request protocol,
scope and authorization checks. This compatibility rule must never be
used to bypass session-attach nonce verification, or to interpret a marker
embedded inside ordinary conversation text as readiness.

## Validation

From a checkout of this branch:

```powershell
$env:PYTHONPATH = "source"
python source/WebAgent/tests/validate_shared_marker_only_readiness.py
python source/WebAgent/tests/validate_startup_protocol_flow.py
python source/WebAgent/tests/validate_readiness_missing_closing_marker_switch.py
python -m unittest source/WebAgent/tests/test_claude_readiness_quotes.py
python -c "from agent_core.protocol_manifest import validate_protocol_manifest; print(validate_protocol_manifest('.'))"
```

Expected new test marker: `SHARED_MARKER_ONLY_READINESS_PASS`.
Then smoke-test actual WebAgent bootstrap using one provider that sometimes
returns the short marker, and an Other URL provider when configured.

Do not merge this branch into `main` or the separate Evidence Foundation
branch until the local regressions and one actual bootstrap are verified.
