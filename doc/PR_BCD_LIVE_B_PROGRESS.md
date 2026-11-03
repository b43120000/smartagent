# PR-B/C/D Live B — Fixed Progress Ledger

Status: **40% / 100% — B-P0 20% offline, B-P1 20% offline PASS; both Live credits pending**
Last updated: 2026-11-03 (Asia/Taipei). This document is a tracking contract,
not evidence of a Live pass.

## Fixed weights (do not rebaseline between debug turns)

| Phase | Objective | Offline regression | Live acceptance | Total |
|---|---|---:|---:|---:|
| B-P0 | Request-first Project Root; native read Scope; strict request write ceiling | **20% PASS** | 5% PENDING | 25% |
| B-P1 | INSPECT proven observation, stable Evidence and freshness | **20% PASS** | 5% PENDING | 25% |
| B-P2 | GAR Action Blocker classification and safe replanning | 15% PENDING | 5% PENDING | 20% |
| B-P3 | Scoped offline install, independent VERIFY, Resume, environment persistence | 20% PENDING | 10% PENDING | 30% |
| **Total** | | **40% earned** | **0% earned** | **100%** |

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

Historical Live receipt `TASK-890C11161F947FB158CC` showed real successful
root/wheelhouse/deps reads and explicit project-root resolution. The
sanitized RCA did not include `ceiling_valid` or `ceiling_reason`, and the
observed `explicit_path_count=4` included the extra wheelhouse path.
Therefore the 5% B-P0 Live credit remains pending until the Runtime
`request_write_ceiling_bound` event confirms:
`ceiling_active=true`, `ceiling_valid=true`,
`ceiling_reason=REQUEST_WRITE_CEILING_READY`, and
`grants_authorization=false`. The updated collector keeps these safe fields.
Do not repeat destructive probes merely to earn P0 credit.

## B-P1 offline implementation — 20% PASS

Development base: `2f60e622de89cc9d8348e44c56da19284bd38543`
Branch: `feat/live-b-p1-observation-evidence-20261103`.
`main` is unchanged. B-P1 Live credit (5%) is **not yet awarded**.

- New `pr_bcd_observation_evidence.py` consumes a real Runtime tool-result
  envelope and action data. It admits successful, actually executed,
  identity/path-bound native `list_directory`, `inspect_directory`, and
  positively proven read-only `run_command INSPECT`. Rejected, partial,
  mismatched and fabricated payloads are not admitted.
- PR-A records the **observation Action** `SATISFIED` with a unique durable
  evidence reference, without setting the business success condition to PASS
  or granting installer rights.
- Strict INSPECT-only CWD exception uses read authorization. Literal file
  targets are also read-scope verified; unknown commands, mutations and
  compound VERIFY steps retain their previous write-CWD security checks.
- Definitely unstarted/rejected shell commands do not stale existing reads;
  ambiguous actual effects do stale them. Proven read-only INSPECT cannot
  stale prior reads.
- `collect.bat` now preserves sanitized B-P1 proof codes plus B-P0 ceiling
  validity, eliminating the previous evidence-collection blind spot.
- Windows CI (all PASS on the B-P1 code): read-only P1 IT **10 tests PASS**,
  full V9 durable PR-A integration, real Windows PowerShell INSPECT smoke,
  P0 regression, Continuation and broader Evidence/GAR/Hybrid.
  Runs:
  https://github.com/b43120000/smartagent/actions/runs/41526072361
  https://github.com/b43120000/smartagent/actions/runs/41526072561
  https://github.com/b43120000/smartagent/actions/runs/41526072440

B-P1 Live acceptance: see `doc/PR_BCD_LIVE_B_P1_EVIDENCE.md`. Only
read-only native scans and one proven INSPECT; do not install/mutate/verify
the full PR-B/C/D task. Check `pr_b1_observation_admitted`, PR-A
`SATISFIED` action entries, stable `ACTIVE` evidence and no invented
task verification PASS. After real task-scoped RCA confirms all criteria,
credit +5% for B-P1 Live.

## Next phases, still pending

- B-P2: safe GAR reconciliation for completed failed reads versus unknown effects.
- B-P3: separately authorized and verified offline Wheel install into project
  deps, independent VERIFY linked to MUTATE, Resume, project environment
  persistence and re-probe. Never modify shared Python or use the network.

The high-level Live B task cannot yet be deemed SUCCESS. No Live B installation
has been authorized or performed by this implementation task.
