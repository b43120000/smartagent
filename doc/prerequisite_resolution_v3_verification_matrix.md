# Prerequisite Resolution v3 — Stage-by-Stage UT / IT / Live Verification Matrix

> **PLAN ONLY.** IDs, expectations, commands and gates below are proposed. No tests were run when authoring this document.  
> Parent: [v3 design](prerequisite_resolution_v3_design_plan.md).  
> Future dependency: Tri-One Phase 1 (2A → 5A → 5B) must already have ACCEPTED evidence on an explicitly pinned Source/loaded-Worker SHA.  
> Planning baseline before Tri-One implementation: `main@b95be77876ac522248954f3127d4fb7b1024eed2`.

## 0. General validation and report contract

**Sequence for EVERY stage**: source preflight and candidate pin → UT → IT → security/runtime regression → controlled Live → formal evidence review/acceptance. Tests may be collected in an altered order if required for safety, but formal acceptance must include all mandatory gates. If a case FAILs, complete later **independent and safe** cases, preserve failed evidence and do not prematurely accept or merge the stage.

### Definitions
- **UT**: isolated model, graph, ledger and policy rules with fake clock/FS/tool facts; no live browser, network, actual install or user-data writes.
- **IT**: real SmartAgent parser/Action Loop, scope policy, event/ledger stores and queue/adapter components with deterministic fake Planner; use an **isolated disposable workspace** and bounded offline commands.
- **Live**: fresh natural-language request from real supported originating interface → bound real WebGPT Planner → authorized Runtime action(s) → objective evidence/receipt → actual delivery back to original interface. Only isolated disposable test workspace and explicitly allowed actions.
- **Case verdict** `PASS/FAIL/BLOCKED/NOT_RUN` is the formal test assessment. **Task outcome** `SUCCESS/FAILED/PARTIAL/UNKNOWN` is separate. An intentionally denied privilege or uncertain operation yields formal test PASS when objective non-SUCCESS is correctly preserved.
- **Mandatory**: must pass on candidate SHA for stage acceptance. When a required Live fixture/permission/provider is unavailable, record `BLOCKED`; seek a safely approved equivalent fixture or defer acceptance, never mark it PASS by omission.
- **Platform**: Windows local Codex runner; current official browser binding verified in preflight. No unapproved Edge instances, especially `edge5`; do not reuse old IT19 pending tasks.

### Required per-attempt evidence
`stage`, `test_id`, `attempt_id`, `test_definition_revision`, `source_full_sha`, `manifest_hash_verdict`, `loaded_worker_sha` (Live), original `request_id/task_id/task_epoch/intent_digest`, scope digest/fixture root, exact sanitized request bytes/hash, Planner rounds, tools/actions with IDs, probe receipt and confidence/status, prerequisite ledger revision, expected/actual node states, original Action admission/execution, terminal acceptance + TaskResult, transport delivery receipt, formal verdict, exact raw log paths/size/SHA-256, safety blockers. A sent message or 0 exit code is NOT evidence of the expected user-facing terminal outcome.

For negative tests: record whether an operation was **not dispatched**, not simply whether the model said it did not dispatch.

## 1. Milestone PR-A — typed DAG & durable ledger (no new remedies)

### UT (PR-A-U01..U12, mandatory)
| ID | Test input/condition | Expected |
|---|---|---|
| PR-A-U01 | Valid typed requirements, one original Action, DAG chain | Canonical accepted versioned graph |
| PR-A-U02 | Duplicate node IDs / nonexistent `depends_on` reference | REJECT; nothing persisted as actionable |
| PR-A-U03 | Direct/indirect cycle, excessive depth/width or graph count | REJECT with bounded diagnostic |
| PR-A-U04 | Cross-task/epoch/intent/scope digest or unrelated Action ID | REJECT; cannot attach borrowed evidence/rights |
| PR-A-U05 | Initial UNKNOWN, valid MISSING → REPAIRING → verification candidate | Legal state-machine path only; no premature SATISFIED |
| PR-A-U06 | Invalid status jump or forged SATISFIED/evidence ref | REJECT, cannot unblock Action |
| PR-A-U07 | Atomic write crash before rename, log journal recover | Last committed graph revision intact |
| PR-A-U08 | Repeated identical graph/change IDs after restart | No duplicate events/graph revisions |
| PR-A-U09 | Stale graph revision/concurrent update compare-and-swap | Conflict, no lost update |
| PR-A-U10 | Original task changes scope or snapshot while graph remains | Nodes marked STALE or guarded; no inherited authorization |
| PR-A-U11 | Disabled v3 flag and v2 advisory API | Legacy behavior unchanged |
| PR-A-U12 | Prompt injection in requirement description, long errors, fake command/approval fields | Treated as untrusted data; no tool execution/permission expansion |

### IT (PR-A-I01..I07, mandatory)
| ID | Path | Expected |
|---|---|---|
| PR-A-I01 | Fake Planner → V9 Progress → graph create → persistent Prerequisite Ledger | Correct request/action/epoch and DAG revision |
| PR-A-I02 | Blocked action + `PREREQUISITE_REQUIRED` state | No original Action dispatch or invented Tool result |
| PR-A-I03 | Process restart then load graph and active blockers | Exactly same state and evidence references |
| PR-A-I04 | Competing fake Task/epoch or stale graph update | Isolation and conflict rejection |
| PR-A-I05 | Answer-only accepted Phase 1 task | No new graph/probes/tool calls |
| PR-A-I06 | Existing v2 advisory / unchanged command plan path | No drift in normal Action Loop or Terminal admission |
| PR-A-I07 | Queue/Event/Progress adapter mapping | Graph reference exposed via existing contract, no new divergent protocol |

### Live (PR-A-L01..L04, mandatory scoped smoke)
- **PR-A-L01**: harmless action-required request through actual originating Telegram **or** approved WebCopilot in a disposable workspace; new graph/blocker visible, no auto-repair; task may remain blocked/paused — successful test observation is separate from Task SUCCESS.
- **PR-A-L02**: answer-only question through accepted Tri-One Fast Path; no v3 graph/probe, no extra Tool/Planner round beyond the Phase 1 baseline.
- **PR-A-L03**: read-only action whose requirements are pre-satisfied; existing normal action/terminal semantics preserved.
- **PR-A-L04**: capture original-interface update/receipt for the same task; no duplicate message or new Task ID for a prerequisite.

**Gate PR-A**: all UT/IT and required Live smoke PASS; no host mutation introduced; verified graph persistence and source pin.

## 2. Milestone PR-B — scoped probes / detection / freshness

### UT (PR-B-U01..U12, mandatory)
| ID | Case | Expected |
|---|---|---|
| PR-B-U01 | Valid exact-path exists with reachable parent | Typed SATISFIED / correct receipt |
| PR-B-U02 | Confirmed nonexistent required input | MISSING, no creation |
| PR-B-U03 | Parent inaccessible / ACL denied / symlink/reparse ambiguity | DENIED or UNKNOWN, NOT MISSING |
| PR-B-U04 | Interpreter binary/version verified vs manifest-only claims | Only actual scoped probe can SATISFY |
| PR-B-U05 | Python import in declared environment succeeds vs ModuleNotFoundError | SATISFIED vs MISSING; distinguish probe failure |
| PR-B-U06 | Probe timeout, killed process, corrupt stderr, truncated result | UNKNOWN with explicit evidence, no optimistic retry |
| PR-B-U07 | Read-only Git repo check negative | MISSING, no `git init` |
| PR-B-U08 | VPCS pinned snapshot stale / newly changed file same mtime | STALE, no implicit pinned rebind |
| PR-B-U09 | Independent probes coalesced into one bounded batch | Stable request-scoped results; hard action/bytes budget |
| PR-B-U10 | Mixed text-only vs current-data/task requiring Tool | No probe for true answer-only, no false answer-only success |
| PR-B-U11 | Cached environment fact no longer matches binary hash/scope | Invalidated; re-probe required |
| PR-B-U12 | Before-action TOCTOU change / wrong sandbox path | Original Action remains blocked until fresh guard |

### IT (PR-B-I01..I09, mandatory)
| ID | Connected components | Expected |
|---|---|---|
| PR-B-I01 | Real Python/Git probe with fake bundled executables | Trusted fact/sha/version/exit receipt; no manifest shortcut |
| PR-B-I02 | Isolated known-existing vs missing file | SATISFIED/MISSING only on conclusive observations |
| PR-B-I03 | Denied path, missing parent, timeout fixture | DENIED/UNKNOWN; no parent creation |
| PR-B-I04 | `query_project` pinned stale in disposable VPCS fixture | STALE, preserve `project_snapshot_stale` |
| PR-B-I05 | Graph requirement satisfied then underlying file changed | Invalidate and prevent Action |
| PR-B-I06 | Task-specific cwd binding and command scope | No parent-directory authorization |
| PR-B-I07 | Probe batch partial failure | Independent facts retained, unresolved nodes stay blocked |
| PR-B-I08 | Fast Path smoke through real Planner State adapter + fake Planner | 0 prerequisite operational actions |
| PR-B-I09 | Probe ledger/event persistence and restart | No stale proof promoted to SUCCESS |

### Live (PR-B-L01..L05, mandatory)
- **PR-B-L01**: fresh harmless original-interface request checking exact authorized existing file; appropriate SATISFIED and truthful reply with scoped evidence.
- **PR-B-L02**: fresh read-only check for deliberately absent file under existing authorized parent; expected MISSING, no file created.
- **PR-B-L03**: inspect Git initialized/not initialized *in disposable fixture* without mutation; expected factual negative, no `git init`.
- **PR-B-L04**: one answer-only question; no probe or hidden workspace scan.
- **PR-B-L05**: requested path outside authorization via controlled safe text; DENIED/BLOCKED, no exploratory parent read and no fake success.

**Gate PR-B**: conclusive facts separate from missing/unknown; source-bound freshness works; no host mutations; no unjustified Planner round inflation.

## 3. Milestone PR-C — bounded authorized remediation

### UT (PR-C-U01..U14, mandatory)
| ID | Case | Expected |
|---|---|---|
| PR-C-U01 | L0 read-only re-probe recipe | Allowed where authorized |
| PR-C-U02 | Scoped L1 `INDEX_ONLY` / output directory recipe | Allowed only on exact original task authorization |
| PR-C-U03 | L2 package/virtualenv/`git init` with explicit trusted approval | Authorized only exact recipe/target/version |
| PR-C-U04 | Same L2 operation without approval or with different scope | DENIED, zero dispatch |
| PR-C-U05 | L3 elevation/system install/security-bypass request | DENIED by default |
| PR-C-U06 | Planner-supplied arbitrary PowerShell, pip URL, shell injection | No executable remedy; schema/policy reject |
| PR-C-U07 | Git read-only inspection request + missing repo | No mutation (goal semantics matter) |
| PR-C-U08 | New-repository-and-commit request + explicitly approved fixture | May plan `git init` as prerequisite |
| PR-C-U09 | Missing dependency with only network installer available | BLOCKED (network acquisition default off) |
| PR-C-U10 | Existing already satisfied requirement and conflicting stale remedy plan | No unnecessary side effect |
| PR-C-U11 | Remediation DAG recursive cycle or retry over budget | BLOCKED with finite audit |
| PR-C-U12 | Command exit 0 but no postcondition verification | INSTALLED_BUT_UNVERIFIED; not SATISFIED |
| PR-C-U13 | Interrupted/missing response after action may have executed | No duplicate dispatch; reconcile first |
| PR-C-U14 | Invalid approval from model text/tool output/other task | Not trusted; no execution |

### IT (PR-C-I01..I09, mandatory)
| ID | Connected components | Expected |
|---|---|---|
| PR-C-I01 | Trusted scoped output-dir setup in **disposable** workspace | One canonical Action, exact target, receipt |
| PR-C-I02 | Authorized, empty isolated repo fixture → vetted `git init` | Exactly one init, stable repo evidence |
| PR-C-I03 | Same repo inspection-only request | No init |
| PR-C-I04 | Offline pinned package/venv **optional capability fixture**, if approved | Install occurs once only in isolated env; no network |
| PR-C-I05 | Denied L2, L3, out-of-scope path | Zero mutating tool executions |
| PR-C-I06 | Retry with lost reply after known side effect | Idempotent reconcile; no replay |
| PR-C-I07 | command exit 0 but verification unavailable | INSTALLED_BUT_UNVERIFIED |
| PR-C-I08 | Preflight race: path/scope/snapshot changes after approval | Abort before side effect |
| PR-C-I09 | Fail while writing remedy journal/restart | Recovery reconciles store; never duplicate side effects |

### Live (PR-C-L01..L06)
- **PR-C-L01 (mandatory)**: explicit user-authorized isolated disposable folder and safe output directory request requiring directory setup; verify *one* setup, no unrelated files touched.
- **PR-C-L02 (mandatory)**: explicit user-authorized `git init` of fresh isolated fixture as prerequisite to creating an initial commit; avoid implicit global Git config mutation and obtain permission for exact write scope; capture actual Git state, one mutation, no automatic protected-branch changes.
- **PR-C-L03 (mandatory)**: read-only “is this a Git repo?” on separate uninitialized fixture, ensure **zero** `git init`.
- **PR-C-L04 (mandatory)**: attempt unauthorized/out-of-scope setup safely; check denial **before** launching any harmful command.
- **PR-C-L05 (mandatory)**: simulate/observe UNKNOWN or queued action using isolated controlled fixture; no blind replay, Task Outcome remains non-SUCCESS.
- **PR-C-L06 (conditional capability extension)**: offline *pinned* wheel/venv installation in isolated fixture only with explicit authority and available trusted artifact; if unavailable, report NOT_APPLICABLE to optional extension, do not make blocking network calls.

**Gate PR-C**: scoped approved remedy success + objective non-replay, all mandatory negatives fail-closed, no automatic L2/L3 escalation, completion remains unverified until PR-D proof.

## 4. Milestone PR-D — postcondition proof / resume / TaskResult

### UT (PR-D-U01..U14, mandatory)
| ID | Case | Expected |
|---|---|---|
| PR-D-U01 | Actual remedy receipt + independent postcondition PASS | Requirement SATISFIED |
| PR-D-U02 | Successful command + failed independent probe | Not SATISFIED |
| PR-D-U03 | Requirement SATISFIED but another DAG parent MISSING | Original Action remains blocked |
| PR-D-U04 | Graph revision/source/scope change between verify and admission | Invalidate affected facts and re-probe/re-plan |
| PR-D-U05 | Resume original action only after all scoped predicates satisfied | Exactly one original Action allowed |
| PR-D-U06 | Previously completed original Action / OPEN Writer / UNKNOWN action | No replay/commit/abort, safe block |
| PR-D-U07 | No-effect remediation repeated without new facts | Finite recovery budget, PAUSED/BLOCKED |
| PR-D-U08 | Original task failed after successful repair | TaskOutcome FAILED/UNKNOWN, not SUCCESS |
| PR-D-U09 | TaskResult/Progress/Terminal receipt reconciliation | No invented terminal receipt |
| PR-D-U10 | Delivery PENDING/QUEUED vs DELIVERED | Delivery never changes task verification |
| PR-D-U11 | Restart from REPAIRING vs INSTALLED_BUT_UNVERIFIED | Reconcile actual state before continuing |
| PR-D-U12 | Request ownership/epoch mismatch during resume | Reject and keep old task state |
| PR-D-U13 | Stale VPCS read after repair | No silent snapshot rebound, revalidate |
| PR-D-U14 | Fast Path/text-only after integrating Resolver | No extra probe, turns or action |

### IT (PR-D-I01..I10, mandatory)
| ID | Connected components | Expected |
|---|---|---|
| PR-D-I01 | End-to-end fixture: missing output dir → scoped setup → verify dir → original action | Correct one-time resume, original goal verified |
| PR-D-I02 | Git fixture: absent repo → authorized init → verify repo → original safe git step | Accurate ordered events, no duplicate init |
| PR-D-I03 | Local dependency remediation from pinned offline artifact when available | Verify actual import, then unblock task; optional extension |
| PR-D-I04 | Failed postcondition | Original action not executed |
| PR-D-I05 | Multiple prerequisites, one still UNKNOWN | Remains blocked, no success |
| PR-D-I06 | Crash after remedy before verification, then restart | Re-probe/reconcile, no duplicate remedy |
| PR-D-I07 | Crash after original task action before terminal reply | Check original Action Ledger; no side-effect replay |
| PR-D-I08 | VPCS stale during recovery | Pinned stale preserved, new context requires explicit replan |
| PR-D-I09 | Tri-One TaskResult and Telegram/WebCopilot mocked adapters | Same task ID, separate terminal and delivery receipts |
| PR-D-I10 | No-progress/denial/exhausted deadline | Truthful PAUSED/BLOCKED and bounded turns |

### Live (PR-D-L01..L08, mandatory where supported)
- **PR-D-L01**: through **real Telegram** original ingress, run safe isolated directory prerequisite→verify→original file action→verify→real Telegram receipt; bound single task/epoch.
- **PR-D-L02**: through **real WebCopilot** original ingress, same *equivalent* authorized isolated fixture, source SHA and terminal/delivery semantics; do not infer availability from Telegram success.
- **PR-D-L03**: safe original goal “create isolated Git repo and initial commit” with explicitly approved scope; verify init prerequisite and final commit are separate evidenced actions.
- **PR-D-L04**: original goal “only check Git repo state”; negative finding returned, no automatic init.
- **PR-D-L05**: controlled negative: missing input path cannot be auto-created to falsely satisfy original request.
- **PR-D-L06**: intentionally absent approval/out-of-scope target; exact blocked receipt and no mutating action.
- **PR-D-L07**: naturally observed or isolated injected missing/unknown response (not a destructive live test), no blind replay; truthful PAUSED/UNKNOWN.
- **PR-D-L08**: subsequent answer-only task; no prerequisite checks or Tool cost regression.

**Gate PR-D**: verified prerequisite pass + original task result + original-interface delivery receipt all present and different; safe no-replay; no side-effect overlap or false SUCCESS.

## 5. Cross-stage regression freeze (all affected stages)

Before each candidate's acceptance, run applicable named regressions **on that exact source SHA**. Existing script name is an inventory, **not a claim it is safe or executable on every host**. Codex must inspect their CLI, side effects and Windows environment first, using an isolated checkout; mark unsupported as BLOCKED/NOT_RUN, never PASS.

- `source/WebAgent/tests/validate_round3_prereq_repair.py`
- `source/WebAgent/tests/validate_runtime_environment.py`
- `source/WebAgent/tests/validate_action_loop_runtime_state.py`
- `source/WebAgent/tests/validate_runtime_dependency_loop.py`
- `source/WebAgent/tests/validate_progress_terminal_lifecycle.py`
- `source/WebAgent/tests/validate_evidence_backed_completion.py`
- `source/WebAgent/tests/validate_runtime_evidence_foundation.py`
- `source/WebAgent/tests/validate_evidence_terminal_boundaries.py`
- `source/WebAgent/tests/validate_tool_scope_hardening.py`
- `source/WebAgent/tests/validate_authorized_path_extraction.py`
- `source/WebAgent/tests/validate_typed_paths_request_context.py`
- `source/WebAgent/tests/validate_project_access_indexed_query.py`
- `source/WebAgent/tests/validate_vpcs_context_reference.py`
- `source/WebAgent/tests/validate_vpcs_runtime_pinned_snapshot.py`
- `source/WebAgent/tests/validate_plan_only_telegram_delivery.py`
- `source/WebAgent/tests/validate_single_fence_transport_contract.py`
- `source/WebAgent/tests/validate_it19_open_write_isolation_offline.py`
- `source/WebAgent/tests/validate_it19_task_scoped_runtime_writer.py`
- `source/WebAgent/tests/validate_it20_command_workspace_scope.py`
- `source/WebAgent/tests/validate_it20_bound_workspace_planner_repair.py`
- Accepted Tri-One Phase 1 contract/Fast Path and original-interface delivery regression suite on its final accepted SHA, re-run or ported as appropriate to candidate.
- Other affected official source Manifest and action-admission cases identified by stage Source diff.

Protected IT19/IT20 historical evidence is **READ ONLY**. IT19 CASE formal PASS does not turn the paused Runtime Task into completed success. Never import old active OPEN write, sender timeout, or previous action IDs as a new test's executed receipt.

## 6. Stage gating and safe fallback

| Stage | UT | IT | Live | Formal entry to next stage |
|---|---|---|---|---|
| PR-A | PR-A-U01..U12 | PR-A-I01..I07 | PR-A-L01..L04 | Signed `PR-A_ACCEPTED`, no new side effects |
| PR-B | PR-B-U01..U12 | PR-B-I01..I09 | PR-B-L01..L05 | Signed `PR-B_ACCEPTED`, fresh facts only |
| PR-C | PR-C-U01..U14 | PR-C-I01..I09 | PR-C-L01..L05; L06 optional | Signed `PR-C_ACCEPTED`, authorized exactly-once remedies |
| PR-D | PR-D-U01..U14 | PR-D-I01..I10 | PR-D-L01..L08 as supported | Signed `PR-D_ACCEPTED`, original task resumed/proved |
| Final v3 | ALL applicable | ALL applicable | Cross-interface representative + negatives | One final pinned SHA + user signoff |

Before PR-A Source modification: confirm Tri-One Phase 1 has actually been **merged/accepted**; otherwise `DEPENDENCY_NOT_ACCEPTED` and remain design-only.

### Fault injection boundary
Allowed in UT/IT: deterministic fake filesystem/process/provider, controlled transport lost receipt, fake stderr, changing file in temp dir, fake approval denial.  
Live: only read-only or explicitly authorized disposable fixtures. DO NOT deliberately cause unsafe real network install, force crash active service, replay already executed action, modify protected workspace or collide with an active browser owner.

## 7. Performance and acceptance metrics

Track every attempt, positive **and** negative:
| Metric | Meaning / success expectation |
|---|---|
| `planner_turns` | Report baseline vs new (medians/P95), never assert reduction without comparable requests |
| `probe_calls`, `probe_batches`, `probe_bytes` | Quantify JIT benefit and batch overhead |
| `requirements_satisfied/missing/unknown/denied/stale` | Full state distinction, no unobserved PASS |
| `remediation_attempts`, `duplicate_remediation_dispatches` | Target 0 duplicate side effects |
| `original_action_resume_count` | Exactly 1 for eligible safe fixture |
| `postcondition_verification_refs` | Must exist before SATISFIED and resume |
| `false_success_count` | Target 0 for blocked/unknown/denied and task failure |
| `TEXT_ONLY extra operational actions` | Target 0 relative to accepted Tri-One |
| `terminal_receipt` vs `delivery_receipt` | Both observed independently for user-facing complete |
| `source_sha` vs `loaded_worker_sha` | Must match approved candidate, else BLOCKED |
| `sender_timeout` vs Task outcome | Timeouts alone cannot be verdicts |

Keep a structured `reason_code` for every blocked/unknown result and every excluded/missing test. Where a mandatory case cannot be executed safely, mark `BLOCKED` with cause and hold stage acceptance until an approved equivalent setup exists.

## 8. Example result record (schema proposal)

```json
{
  "test_id": "PR-D-I01",
  "stage": "PR-D",
  "level": "IT",
  "attempt": 1,
  "verdict": "PASS",
  "task_outcome": "SUCCESS",
  "expected": "PREREQ_VERIFIED_THEN_ORIGINAL_ACTION",
  "observed": "<replace with real trace summary>",
  "source_sha": "<exact candidate commit SHA>",
  "loaded_worker_sha": "OFFLINE_FIXTURE",
  "request_id": "<observed>",
  "task_id": "<observed>",
  "task_epoch": "<observed>",
  "prerequisite_ledger_revision": "<observed>",
  "probe_evidence_refs": ["<observed>"],
  "remedy_action_ids": ["<observed>"],
  "original_action_ids": ["<observed>"],
  "planner_turns": null,
  "operational_action_count": null,
  "terminal_receipt": "<observed or not applicable>",
  "delivery_receipt": "<observed or not applicable>",
  "raw_log_sha256": "<sha256 of actual file>",
  "notes": "Use observed values, not these illustrative placeholders"
}
```

All illustrative PASS/outcome values above are *contract illustration*, not actual test evidence.
