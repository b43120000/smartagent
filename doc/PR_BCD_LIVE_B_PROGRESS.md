# PR-B/C/D Live B — Fixed Progress Ledger

Status: **20% / 100% — B-P0 offline regression PASS, B-P0 Live pending**
Last updated: 2026-11-03 (Asia/Taipei). This document is a tracking contract,
not evidence of a Live pass.

## Fixed weights (do not rebaseline between debug turns)

| Phase | Objective | Offline regression | Live acceptance | Total |
|---|---|---:|---:|---:|
| B-P0 | Request-first Project Root; native read Scope; strict request write ceiling | **20% PASS** | 5% PENDING | 25% |
| B-P1 | INSPECT proven observation, stable Evidence and freshness | 20% PENDING | 5% PENDING | 25% |
| B-P2 | GAR Action Blocker classification and safe replanning | 15% PENDING | 5% PENDING | 20% |
| B-P3 | Scoped offline install, independent VERIFY, Resume, environment persistence | 20% PENDING | 10% PENDING | 30% |
| **Total** | | **20% earned** | **0% earned** | **100%** |

Do not credit unfinished phases for tests that happen to cover neighboring
code. Award the Live portion only after real task-scoped RCA proves the
phase-specific acceptance. A failed Live test does not erase an independently
passed offline regression, but it reopens the affected phase if the regression
itself was incomplete or invalid.

## B-P0 implemented

Base: `3290bac77c50050b7bc900f254de4b9ebf41c836`
Branch: `fix/live-b-p0-scope-20261103`. Main is not modified.

- The literal original request `在 E:\SmartAgent_PR_BCD_LIVE 執行...`
  now has a lexical boundary before `執行`; task prose cannot become a
  fake path. Quoted paths and adjacent paths retain strict parsing.
- Explicit project root is pinned for dependency recovery context and project
  environment hints. The old longest-path heuristic could mistake
  `environment.json` for the project root.
- Request scope diagnostic records root provenance, workspace comparison,
  path count and read != write authority; collect.bat preserves safe codes.
- An explicitly write-restricted PR-B/C/D request produces a Runtime-owned,
  request-local ceiling. It only *reduces* existing write authority.
  Unknown shell effects, direct project file modifications and mutations are
  denied at both scope preview and tool execution. No new grant is minted.
  PR-C/D installation and verification commands remain unimplemented in this
  ceiling, intentionally reserved for B-P3.
- Regressions: `source/WebAgent/tests/validate_pr_bcd_live_b_p0_scope_it.py`
  (6 tests), plus existing PR-A, PR-B/C/D, Evidence, GAR and 5A suites.

## B-P0 Live acceptance — do not run full Live B yet

Run a new Telegram Task restricted to *read-only* Project Scope. The requester
must include the same absolute root, deps, and environment metadata paths with
the explicit no-other-write restriction. Use only native `list_directory`
and `inspect_directory` actions, with canonical absolute paths. Do not
install, mutate, download, use shell commands or change the project.

Check a sanitized RCA bundle for:
1. `request_project_scope_resolved`: source
   `USER_REQUEST_EXPLICIT_DIRECTORY`, 3 explicit paths, and no write grant.
2. Native list/inspection targets root, wheelhouse and deps:
   `scope_allowed=true` and `scope_error_code=SCOPE_ALLOWED`.
3. Invalid/outside/project write probes are rejected before any side effect.
   Offline negative regression covers these. Do not intentionally perform
   destructive Live probes.
4. Any unrelated terminal Verification/GAR error is assessed under B-P1/P2,
   rather than reclassifying successful P0 read Scope as a failure.

If these hold, increase progress **20% -> 25%**. Otherwise remain **20%**
until a source correction and revalidation.

## Next phases, still pending

- B-P1: proven INSPECT, accurate freshness, independent evidence.
- B-P2: safe GAR reconciliation for completed failed reads versus unknown effects.
- B-P3: separately authorized and verified offline Wheel install into project
  deps, independent VERIFY linked to MUTATE, Resume, project environment
  persistence and re-probe. Never modify shared Python or use the network.

The high-level Live B task cannot yet be deemed SUCCESS. No Live B installation
has been authorized or performed by this implementation task.
