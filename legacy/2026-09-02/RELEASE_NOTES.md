# SmartAgent release 2026.09.02

This release stabilizes sequential RemoteAgent execution on Windows.

## Highlights

- Reuses one canonical ChatGPT conversation page and collapses duplicate tabs.
- Replaces request-worker `page.goto()` navigation with conversation-ID and
  composer-readiness checks.
- Serializes Agent 0 observation and Agent 1 execution through an execution-page
  lease.
- Refreshes the durable task queue across processes before routing and dispatch.
- Uses retrying atomic JSON state writes for shared Windows state files.
- Includes the local Telegram simulation launcher for end-to-end verification.

## Verified acceptance

- Release boundary and Python syntax: PASS.
- WebAgent startup protocol, startup input queue, and standalone loop: PASS.
- RemoteAgent page reuse and hand-in-hand protocol regressions: PASS.
- Two sequential local Telegram simulations without restarting Agent 0: PASS.
  Both requests reached `COMPLETED`, returned an explicit `已完成`, and created
  the requested `text1.txt` and `text2.txt` files.

Live ChatGPT, Telegram, and account rate limits remain external dependencies.
