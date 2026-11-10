# Git48 — ACL OFF always disables Turbo execution

Base: Git47 `a3cb89065a54d4f76058512a4925c505f4cc4bad`.

## Decision matrix

| ACL | Persisted Turbo request | Effective Turbo | Ordinary execution |
|---|---|---|---|
| OFF | OFF | OFF | Allowed subject to normal software guards and approvals |
| OFF | ON (stale) | OFF | Allowed subject to normal software guards and approvals |
| ON | OFF | OFF | Normal ACL-restricted execution |
| ON | ON | OFF / BLOCKED | No production Windows OS sandbox broker in Git48 |

The raw persisted `turbo_requested` remains visible for diagnosis: it is **not**
an authorization. The action boundary always evaluates `acl_mode` and the
OS-sandbox state. `ACL OFF + stale ON` reports
`reason_code=TURBO_SUPPRESSED_ACL_OFF` and
`turbo_effective=OFF`; it must not block normal software-guard actions.

When `ACLstatus.bat off` succeeds, its existing transactional action clears
Turbo roots and requested status. The runtime suppression above handles
older persisted settings or state that survived an interrupted transition.
Enabling a new Turbo root while ACL is OFF is still rejected. Turning ACL
back ON does not automatically re-enable cleared Turbo preferences.

An unreadable/corrupt shared policy, unverified ACL mode or inconsistent
state is a failure, NOT an implicit ACL OFF. ACL ON + requested Turbo ON
remains blocked pending a real production OS broker; no fail-open fallback.

## Validation

Git48 workflow covers Windows and Ubuntu, the four ACL/request combinations,
stale ON vs new OFF, unknown ACL/corrupt policy, CLI/planner/decision reports,
executor-client/service gates, a **real local subprocess** executed through
`run_bounded_process` in software-only mode, and existing ACL OFF software
guard and Git47 WebCopilot/Tri-One regressions.

These tests do **not** establish production OS sandbox readiness. Local Windows
ACL transitions and real WebCopilot live actions require separate user Live tests.

### Local read-only status

```bat
cd /d C:\Users\user\Desktop\remoteagent\smartagent
ACLstatus.bat status
```

Expected after ACL OFF: `ACL Mode: off`, `Turbo Effective: OFF`.
If the persisted request was stale ON, its display may remain ON until a
successful `ACLstatus.bat off` clears the shared policy.
