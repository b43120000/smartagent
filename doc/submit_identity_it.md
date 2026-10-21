# Submit identity / provider quota verification

Implemented in the execution package, 2026-10-13.

- ChatGPT asks receive a unique WEBUI_SUBMIT_ID transport marker before composer insertion and scope capture. Request ID alone is not a per-round identity.
- Nonce-bearing user turns must match uniquely. Missing/ambiguous markers cannot fall back to a historical ordinal or pair with an arbitrary fresh assistant.
- Recycled ChatGPT fallback units require the confirmed nonce-bearing user, the same current conversation unit, and content absent from the pre-submit baseline. Native message IDs retain strict freshness checks.
- Session attach readiness must match a new handshake_id. Old readiness cannot satisfy a new attach.
- At most two pinned-profile mount recovery attempts; no implicit resubmit or selector union. Diagnostics contain structural metadata rather than message bodies.
- Visible ChatGPT quota error panels are recognized separately from quoted user/assistant content. Preflight and wait gates report WEB_PROVIDER_RATE_LIMITED without retrying or changing models.
- Conversation-length error panels report WEB_PROVIDER_CONVERSATION_FULL, with a natural-language instruction to create a new conversation and update its workspace URL. Telegram failed/interrupted messages include this explanation. A full page cannot accept a final natural-language post; Runtime/Telegram report it without another send.
- Quota/full states without a pending submission are not RECOVERY_REQUIRED, so they cannot permanently poison subsequent requests after the panel clears.

## Validation

UT: existing Web UI ownership/contract, provider boundary, session lifecycle, submit-identity regression, and offline real-browser quota DOM fixtures passed.

Live IT used the exact original requests through local Telegram-shaped ingress (not Telegram network delivery):

1. Build Androidviewer: RR-LOCAL_TEST-3E200D1AB5D8EB10.
2. Checkpoint execution package with message 路徑json 文字保護: RR-LOCAL_TEST-F9CBCF11F6B56E3E.

Both correctly stopped before tools or another prompt were sent, with WEB_PROVIDER_RATE_LIMITED. Read-only live DOM confirmed a visible quota panel and retry control. Neither test is a successful build/checkpoint E2E PASS. Re-run both when the provider quota clears; do not bypass the quota or accept historical assistant content.

The initial IT attempt RR-LOCAL_TEST-33C2CAA74E430646 ran before quota classification was added and reproduced WEB_ASSISTANT_START_STALLED. This discriminated the current provider-blocking condition from a hidden valid answer. Historical failures lacked this panel diagnostic, so their exact underlying provider condition is not retroactively proven.

## IT after user changed the conversation URL

Build request RR-LOCAL_TEST-0E89192E3EB902CF completed successfully, verification PASS, APK app/build/outputs/apk/debug/app-debug.apk. The run needed command/contract repair rounds, but user/assistant ownership continued correctly. Do not interpret this single pass as proof of every provider UI variant.

Conversation-full classification and Telegram text were tested using isolated real-browser DOM fixtures, including quoted-content/hidden-panel negative cases. No production conversation was deliberately filled to force its length limit.
