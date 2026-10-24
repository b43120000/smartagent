# SmartAgent runtestIT7 — IT06 repair baseline

- Source base: IT6 `d791971b2b1ecf4fc82267872d0d9018c6c18881`.
- Scope of this source change: **IT06 only**. IT6 source and historical reports are immutable.
- Required deterministic check: `PYTHONPATH=source python source/WebAgent/tests/validate_runtime_evidence_it7.py`, then all eleven existing IT6 regressions. These checks have **not** been run by the GitHub edit session.
- IT06 Live acceptance: malformed original `file.exists / contains / "yes"` must be rejected before Tool execution; zero canonical observations, zero substitute reads, no Predicate PASS, and a truthful signed Runtime `INTERRUPT/UNKNOWN` receipt scoped to current Request/Epoch. The rejected action remains a protocol event, not a fabricated Tool result. Preserve any original failure metadata.
- Explicitly keep SUCCESS fail-closed; a Planner assertion alone does not authorize rejection-based interruption. Executed, continued, foreign-epoch, or side-effect-bearing actions cannot use this exception.
- IT7 remaining pool, no changes yet: IT10, IT12, IT13, IT14, IT20 pending after writer quarantine investigation; IT19 remains SAFETY_BLOCKED. IT01–03/04/05/07–09/11/15–18/21–23 retain their evidence-scoped historical/IT6 PASS. No independent copy, merge/main, or original-package deployment in this Git-only change.
- Future Live tests use the authorized `C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1` package and its original `send_and_wait()` path, only after Regression gate and writer safety checks.
