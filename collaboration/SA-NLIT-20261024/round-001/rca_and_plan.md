# SA-NLIT-20261024 — Round 001: RCA, classification and corrective plan

**Author:** ChatGPT (GitHub source review; no local Runtime execution)  
**Task:** `SA-NLIT-20261024` · **Round:** `001`  
**Report source:** `collaboration/SA-NLIT-20261024/round-001/handoff.json` at `ffd727e7ff4fc1a511a041064240efab29bcba5d`  
**Product baseline:** `309305b2eeeb79aed0925dc4dfee45af5193f8b5`  
**Branch:** `fix/nlit-evidence-20261024-r1` (two report-document commits from baseline, plus the ACK; no product code changes in this delivery)

## 1. Evidence integrity and acceptance boundary

The exact report commit was read through GitHub, not inferred from user text. Its `ack_nonce=NLIT-R1-ACK-8F62D1` and `product_sha=309305b2eeeb79aed0925dc4dfee45af5193f8b5` were checked. The ACK was committed as `62dd9cf5cc5777367d63558eae288a105af9c05b`.

**Current source scope:** 17 submitted requests from IT07–IT23 (IT23-A and IT23-B are distinct; IT19 not submitted). The report lists 12 `TASK_COMPLETED`, 3 `TASK_INTERRUPTED`, 2 `TASK_FAILED`, and **15 distinct Runtime acceptance receipts**. Those are *transport/terminal facts, not 15 test PASS*. Two requests are missing receipts. **0/17 exact next-Planner input captures** were supplied. A receipt with `evidence_verdict=PASS` can mean a terminal decision was admitted; it is not automatically a positive `file.exists` Predicate or successful end-to-end NL-IT. The report also lists one missing Progress timeline. Running-process SHA remains independently unverified.

**Evidence limit:** The handoff contains sanitized excerpts, not all original bytes. Numerous `action_results_raw.txt`, `conditions_raw.json`, `transport_final_raw.txt`, and `progress_timeline.jsonl` entries explicitly say `truncated=true`; they are NOT treated as complete. Model-authored final text, local fixture preparation, a status icon and a terminal receipt do not prove next-Planner delivery. Unavailable details are `NOT_OBSERVED`, not inferred. No absolute local path, conversation URL, CDP endpoint, browser profile or private log is included in this public handoff.

**Spec precedence:** Source `acceptance_spec` Rev3 plus `spec_overrides` supersede historical `d3963f55`: baseline is `309305b2eeeb79aed0925dc4dfee45af5193f8b5`; IT01–IT06 are historical, do not rerun; use LOCAL_TEST Telegram-shaped receiver → real Gateway/queue/supervisor/Planner/Runtime (not external Telegram); IT23-A/B are distinct requests. The later user instruction authorizes ChatGPT triage and fixes but does not relax safety/evidence acceptance.

## 2. Confirmed findings vs hypotheses vs environment

### Confirmed from report excerpts and/or inspected product source

1. **IT07 — invalid Condition Binding was not turned into a successful observation.** Source report records `WEBAGENT_EXPECTED_FAILURE_REJECTED` and `expected_failure_not_bound_to_success_contract`; original `observe_fact` did not execute. A separate directory listing and an empty project event ledger cannot prove its Predicate. A formal `INTERRUPT/UNKNOWN` receipt exists despite model text saying no receipt had been obtained *at submission time*; these are different time points. Ledger provenance for the rejected admission action is not established in the excerpt.
2. **IT08 — raw observation is not Predicate PASS.** Raw `file.exists=true` was observed with no bound Predicate (`NOT_REQUESTED`, `UNVERIFIED`); Runtime admitted a task-level `COMPLETE/SUCCESS` for reporting the observation. That does not change Predicate verification.
3. **IT09/IT13/IT14/IT16/IT21 — negative outcomes remained negative** in the available formal receipt fields (`COMPLETE/FAILED`, `verification_status=FAIL`). IT09 is the deliberate contradicting file presence test; IT13 includes external untrusted fake PASS text; IT14 has independent A/B conditions; IT16 runs exit code 7; IT21 requests illegal post-hoc predicate revision. Exact action sequence, immutable condition binding and next-Planner input still require untruncated originals.
4. **IT10 and IT23-A — UNKNOWN was not automatically converted to false/PASS.** The provided acceptance receipts say `INTERRUPT/UNKNOWN` and `verification_status=UNKNOWN`. IT07 likewise has an accepted UNKNOWN terminal.
5. **IT11, IT12, IT15, IT20 — accepted task SUCCESS receipts exist**, some with `verification_status=PASS`. IT12 report states Planner independently refused the fabricated evidence-ref request; do not claim a Runtime rejection of that unsubmitted ref without its own raw diagnostic.
6. **IT17 — query success and Predicate success differ.** Formal receipt `COMPLETE/SUCCESS` but `verification_status=UNKNOWN`. Excerpts indicate `query_project` returned project query/snapshot observations, not an independently bound Predicate PASS; full structured output is truncated.
7. **IT18 — requested read-back was only PARTIAL.** Report records `write_file` executed, `read_file` rejected by `WEBAGENT_EVIDENCE_ROUTE_REQUIRED`, replacement `query_project` evidence, rejection of an attempted SUCCESS as `success_contradicts_runtime_verification`, then formal `COMPLETE/PARTIAL`. Receipt `verification_status=PASS` must not override receipt `evidence_verdict=FAIL` or the originally unmet read-back condition. This is a material acceptance shortfall, **not an all-PASS**.
8. **IT19 — verified architectural isolation boundary / safety block.** Source `source/agent_core/tools.py` constructs `ChunkedWriteManager(workspace / ".agents" / "chunked_writes", roots)`; `source/agent_core/chunked_write.py` constructor invokes `cleanup_expired()` and `_recover_staging_boundaries()` over manifests in that store. Under a shared active workspace, transaction cleanup/recovery is not per NLIT batch. Codex correctly **did not submit** IT19 or touch an open transaction. This is a confirmed reason for the isolation block, but not proof of an already-corrupted production transaction.
9. **IT22 — Composer pre-write obstruction.** Report records `TASK_FAILED`, `WEB_COMPOSER_PREWRITE_FAILED`, `composer_pre_write_failed`. `source/agent_core/web_runtime.py` maps pre-write mutation-impossible errors to fail-closed, no-send behavior. This case does **not** prove replay/blocker acceptance or a completed `observe_fact` test.
10. **IT23-B — unconfirmed unrelated draft.** Report records `TASK_FAILED`, `WEB_COMPOSER_UNCONFIRMED`, `composer_helper_failed:composer_unrelated_draft_present`, with no terminal acceptance receipt. Production code deliberately avoids clearing, resubmitting or overwriting an unverified composer. Cause of the unrelated draft and exact operation journal remain unproven.
11. **Planner input evidence gap — established test observability failure, not established Predicate engine failure.** `source/WebAgent/protocol_loop.py` builds each `[WEBAGENT_TOOL_RESULTS]` next-turn prompt and calls `self.planner(self._with_commit(prompt, expected), expected, attachments)`. It emits `planner_round_started` and `planner_response_received`; event log excerpts are not an exact saved copy of the model-visible post-tool prompt and do not prove successful Web UI delivery. `source/WebAgent/browser_client.py` / `source/agent_core/web_runtime.py` add browser transport preflight and composer/send gates. The 17 missing exact captures cannot be reconstructed by assumption.

### Hypotheses needing raw evidence

- Whether IT22 was caused by transient browser/DOM/provisioning state, a helper timeout, stale browser handle, or a product regression is **not established**. Preserve the precise helper journal/error code/operation ID and browser stage transitions locally.
- Whether IT23-B's unrelated draft was left by IT23-A, by another actor in a shared browser page, or by a recovered state is **not established**. Do not automatically clear or replace it.
- Whether IT07 rejection should have produced additional immutable Condition/Admission ledger evidence is a **traceability enhancement candidate**; current `EMPTY` project-event ledger alone does not establish an incorrect predicate result.
- Whether IT18 is a correct consequence of strict project-source evidence routing, a permission/scope issue, or a read-back UX defect cannot be decided from truncated source-result excerpts. Never relax `SUCCESS` to accept `PARTIAL`.

### Environment/deployment limitations

- Codex reported tracked-clean detached `309305b2eeeb79aed0925dc4dfee45af5193f8b5` and one preserved untracked fixture; **loaded process SHA = UNVERIFIED** and the live Agent was in use. No local installation update has been performed in this round.
- The GitHub branch contains handoff/ACK documents and is not proof that the currently executing Windows process has loaded branch changes.
- This round performed GitHub source inspection only; no local files, browser page, CDP, running process, terminal command or real NL-IT were accessed by ChatGPT.

## 3. Case triage — provisional, separate raw status from test verdict

| Request | Reported Runtime/transport | Analyst disposition **for now** | Blocking evidence / action |
|---|---|---|---|
| IT07 | INTERRUPT / UNKNOWN, receipt accepted | Safeguard consistent, E2E **UNVERIFIED** | Need admission rejection source and exact Planner post-action input; do not re-observe prohibited action |
| IT08 | COMPLETE / SUCCESS, Predicate UNVERIFIED | Raw-observation distinction consistent; E2E **UNVERIFIED** | Require exact original tool result + post-tool input |
| IT09 | COMPLETE / FAILED, Predicate FAIL | Negative predicate defended; E2E **UNVERIFIED** | Need full rejection/condition history |
| IT10 | INTERRUPT / UNKNOWN | UNKNOWN preserved; E2E **UNVERIFIED** | Need exact raw observation and next input |
| IT11 | COMPLETE / SUCCESS, PASS | Cross-task separation plausible; E2E **UNVERIFIED** | Correlate new action/condition; verify old IDs unused |
| IT12 | COMPLETE / SUCCESS, PASS | Planner avoided invalid ref; E2E **UNVERIFIED** | Was invalid ref formally submitted? Do not claim Runtime rejected it unless witnessed |
| IT13 | COMPLETE / FAILED, FAIL | Untrusted file did not override receipt; E2E **UNVERIFIED** | Need external-content/tool separation in full trace |
| IT14 | COMPLETE / FAILED, FAIL | A/B isolation plausible; E2E **UNVERIFIED** | Independent condition histories A=PASS, B=FAIL, ordered actions |
| IT15 | COMPLETE / SUCCESS, PASS | Receipt present, E2E **UNVERIFIED** | Compare raw terminal event, task/epoch and rendered transport; next-turn input |
| IT16 | COMPLETE / FAILED, FAIL | Nonzero exit handled as failed action; E2E **UNVERIFIED** | Full process exit/status structured observation and next Planner input |
| IT17 | COMPLETE / SUCCESS, verification UNKNOWN | Query task may succeed, **no Predicate PASS**; E2E **UNVERIFIED** | Complete query payload, snapshot and two structured observation values |
| IT18 | COMPLETE / PARTIAL; attempted SUCCESS rejected | **PARTIAL / acceptance unmet**, not PASS | Full read-back rejection route, write→query correlation and original condition history |
| IT19 | Not submitted | **BLOCKED (safety gate correct)** | Isolated transaction storage + independent workspace and zero foreign open transactions |
| IT20 | COMPLETE / SUCCESS, PASS | Task success receipt exists; E2E **UNVERIFIED** | Distinguish RUNNING/QUEUED from terminal; capture exact next input |
| IT21 | COMPLETE / FAILED, FAIL | Post-hoc PASS unsupported; E2E **UNVERIFIED** | Full immutable binding and replay/admission evidence |
| IT22 | TASK_FAILED, composer pre-write | **FAIL (environment/transport execution)** | Safe composer helper diagnostics; no Predicate/replay verdict |
| IT23-A | INTERRUPT / UNKNOWN, receipt accepted | UNKNOWN preserved; E2E **UNVERIFIED** | Input, result and receipt identities within A |
| IT23-B | TASK_FAILED, unconfirmed unrelated draft | **FAIL (environment/transport execution)** | Safe journal, independent B identity; no task Predicate verdict |

This is 18 table rows because the scope is 17 **requests submitted** plus IT19 **not submitted**. IT23-A/B are counted as separate requests under one numbered IT23 case. Final all-PASS count remains **zero verified complete end-to-end tests** from the supplied evidence; that is an *evidence sufficiency verdict*, not a claim that the 15 admitted terminal decisions were wrong.

## 4. Corrective plan with ownership and acceptance

**P0 — Do not restart completed historical batch.** Codex preserves all 17 original per-request directories and timestamps, retains the current live process while in use, and never rewrites raw test records, fixture, receipts or condition histories. IT01–IT06 remain historical. Never promote `truncated=true` public excerpts into full proof.

**P0 — Close original Planner delivery evidence gap, read-only first.** Codex searches existing local request-specific records (real controller message, outgoing model-input payload, invocation trace) for **exact next-turn Planner prompt as actually presented**, with request ID, task ID, epoch, round, result ID, response/submit correlation and source hash. If unavailable, write `PLANNER_DELIVERY_NOT_OBSERVED` for the 17 original submissions. A reconstruction of `_with_commit(prompt, expected)` from ledger is **not proof of delivery**. Never claim retroactive success. If no capture exists, plan a **new separately identified** targeted run with an opt-in, private local-only, request-scoped pre-send + browser-submitted/ack trace. Preserve exact payload solely inside restricted local storage, not GitHub. Log three different phases: `PREPARED_NOT_SENT`, `COMPOSER_VERIFIED_AND_SUBMITTED`, `MODEL_RESPONSE_CORRELATED`; distinguish these from mere prompt construction. No global text logging or credential export.

**P0 — IT19 isolation remains strict.** Do not use workspace-global production transaction store for a test cleanup/abort/recovery. Codex first establishes a separate **isolated workspace and state root**, with no ongoing production writer, through an authorized public launch/binding; verify resolved `state_root` before constructing any `ChunkedWriteManager`. If the formal tool chain cannot guarantee physical/transaction isolation, IT19 stays `BLOCKED`; no unsafe direct ToolContext invocation, test-only bypass, cross-workspace cleanup or mutation to active production state. A separate implementation may scope transaction stores by authenticated request identity, but must preserve crash recovery/resume and ownership checks; requires focused regression before deployment.

**P0 — Composer failures: retain fail-closed.** Collect local-only state/journal entries for IT22/IT23-B, distinguishing `mutation_possible=false` from unconfirmed mutation; identify exact journal preflight state and per-request browser ownership. Never clear another person's draft, blindly retry, reopen in loops, or send despite uncertainty. If the user can safely resolve their own draft, confirm the page is idle/owned and then use a **new uniquely identified** diagnostic request. No product retry-policy change without reproducible root cause.

**P1 — IT07 rejected admission provenance.** Obtain original admission diagnostic, original action envelope and the immutable ledger decision, not merely the empty project-event ledger. If rejection truly lacks a durable admission record, specify and implement one **outside** success Condition Ledger so invalid condition IDs cannot become PASS. A rejection is evidence of non-execution, not Predicate FAIL. Preserve no-retry restriction of historical case.

**P1 — IT18 readback completeness.** Obtain untruncated `write_file` result, denied `read_file` scope diagnostic, `query_project` snapshot-bound read_range result, and the exact matched failure/success conditions. Define allowed readback verification route *before* dispatch, never silently swap source-file evidence policy. An accepted `COMPLETE/PARTIAL` is not accepted `COMPLETE/SUCCESS`.

**P1 — All other case reviews.** For each original request, compare original prompt, explicit predicate/condition binding, tool execution, raw observation, normalized value (including absent/null/false), comparison, condition ledger, next-Planner input, Planner decision, accepted terminal event and receipt. Record separate columns for `terminal_admission`, `predicate_verification`, `transport_execution`, `semantic_case_verdict`, `e2e_evidence_complete`. Never collapse them.

**P1 — Product fixes only after a reproducible fault is isolated.** Source review established unsafe potential effects of a *non-isolated test*, not a defect deserving an untested writer rewrite while the live Agent is in use. IT22/IT23-B currently establish a safe blocked composer, not authorization to weaken it. **This round makes NO product-source edit.** If future evidence establishes product defects, ChatGPT will propose bounded fixes on this same branch after the current docs HEAD; Codex will validate in isolated local environment. No main change/merge, no old test branch.

**P2 — Regression/deployment gate for any later product code change.** Stop affected live processes gracefully only once the existing workload is idle; preserve untracked fixture and tracked clean baseline. Switch/update only the isolated test installation, not a running instance. Generate manifests **via official generator** from the exact complete working tree, not manual hash replacement:
```powershell
$env:PYTHONPATH='source'
python -c "from agent_core.protocol_manifest import write_protocol_manifest; print(write_protocol_manifest('.'))"
python install_smart_agent/build_update_manifest.py --root . --release-id nlit-20261024-r1
python -c "from agent_core.protocol_manifest import validate_protocol_manifest; print(validate_protocol_manifest('.'))"
```
Require `hash_verification=PASS`, no mismatches, and inspect generated diff for unrelated files. Then run existing focused scripts separately and retain stdout/stderr/exit codes:
```text
source/WebAgent/tests/validate_evidence_terminal_boundaries.py
source/WebAgent/tests/validate_runtime_evidence_foundation.py
source/WebAgent/tests/validate_vpcs_expected_outcome.py
source/WebAgent/tests/validate_startup_protocol_flow.py
```
Also run new request-isolation, composer non-destructive, exact Planner input transmission and negative-predicate regression tests before deploying. The current Evidence Foundation workflow only auto-runs on the earlier fix branch; **do not infer a CI run on this NLIT branch**. Codex must explicitly run or dispatch applicable validations and report actual results.

## 5. Explicit Codex evidence request for next collaboration round

Return a new **sanitized GitHub handoff** linked to this round (do not overwrite original) with: (a) raw source file hashes and full local availability flag per case; (b) exact planner-turn input availability with per-request `CAPTURED` or `PLANNER_DELIVERY_NOT_OBSERVED` and delivery correlation, excluding text/private URLs from GitHub; (c) untruncated *sanitized* IT07 rejection, IT12 invalid-ref handling, IT17 structured query results, IT18 full readback, IT21 post-hoc rejection; (d) IT22/IT23-B composer helper reason, whether prewrite mutation was possible, stage/journal identity and ownership, **without** CDP or conversation identifiers; (e) IT19 isolated-root feasibility based on public entrypoint (or explicit BLOCKED); (f) loaded process provenance, local baseline vs new test deployment SHA, separate test command exit codes. Keep all original full evidence and local-private identifiers **only locally**. Do not rerun completed historical cases simply to fill absent evidence.

### Acceptance criteria for closure

- Source integrity: `main` unchanged; every product fix on `fix/nlit-evidence-20261024-r1` descendant; manifests generated and verified if files changed.
- Each of 17 submitted original requests: one authoritative per-request record and honest exact-Planner-delivery availability classification. Any missing input remains explicitly NOT_OBSERVED; no retroactive claim.
- Negative controls: IT07 invalid binding, IT09 contradiction, IT10/IT23-A UNKNOWN, IT12 forged ref, IT13 untrusted text, IT14 mixed predicates, IT21 post-hoc change all retain fail-closed rules.
- IT18 cannot be marked full PASS without authorized independent full readback and accepted terminal success, or a documented case-specific criterion that legitimately permits PARTIAL.
- IT19 only run when independently isolated; otherwise formally BLOCKED (not a false PASS).
- IT22/IT23-B require safe browser state, real model-input delivery and actual Runtime results; no coerced green transport events.
- Full acceptance only after per-case source/Planner/Runtime/terminal/transport join, with explicit provenance and no missing mandatory evidence. Until then overall status: **NOT_ACCEPTED / MORE_EVIDENCE_REQUIRED**.

## 6. Supplement: Runtime producer event review (Codex concurrent commit)

**Independent follow-up source:** `collaboration/SA-NLIT-20261024/round-001/producer_events.json`, read at exact Commit `a8a5fba86e8cc1e179488a0e461de547ffe97625` (GitHub). Its writer is Codex, product SHA `309305b2eeeb79aed0925dc4dfee45af5193f8b5`, `evidence_kind=SELECTED_PRODUCER_LOG_RECORDS_NOT_EXACT_PLANNER_INPUT`. Codex also verified the ACK at `62dd9cf5cc5777367d63558eae288a105af9c05b` against nonce/product/report. The producer file was appended *after* this RCA's initial commit; its Commit is retained, not rewritten or treated as a new test run.

**Validated envelope:** 17 distinct submitted request records containing **207 selected producer entries**, with stage counts: `tool_started=36`, `tool_completed=36`, `action_evidence_recorded=36`, `action_loop_state_updated=35`, `protocol_rejected=14`, `context_acquisition_required=1`, `request_context_input_recorded=23`, `runtime_terminal_accepted=15`, `EXECUTION_FAILED=5`, and `HEARTBEAT=6`. These counts describe selected source events, not 36 successful predicates or 15 E2E PASS. The file preserves selected producer event bodies in sanitized form but is not asserted to include every event from every original runtime log. The original `action_results_raw.txt` for some requests is a persisted task snapshot rather than the actual result presented to Planner; neither that snapshot nor producer events prove actual next-Planner delivery.

**Changes to source-grounded case interpretation:**

- **IT07:** Producer event `protocol_rejected` round 1 has the exact `[WEBAGENT_EXPECTED_FAILURE_REJECTED]` marker and `expected_failure_not_bound_to_success_contract` for the invalid Condition ID. No `tool_started` for rejected `it07-observe-invalid-condition-01`; only two subsequent read tools (`list_directory` and `inspect_project_ledger`) appear. Later attempted PARTIAL and FAILED progress are also rejected by the Runtime; the accepted outcome is `INTERRUPT/UNKNOWN` round 7. Thus rejection of the invalid binding is **confirmed producer-side**, while Predicate truth remains unobserved.
- **IT08:** One `observe_fact` tool event, `SUCCEEDED/UNVERIFIED`, followed by `COMPLETE/SUCCESS` terminal. The producer record independently preserves the distinction between objective fact observation and bound-Predicate PASS.
- **IT09:** `observe_fact` returns `verification_status=FAIL` and `CONTRADICTED/REPLAN_REQUIRED`; Runtime later accepts `COMPLETE/FAILED` at round 6. Later source-read routing via `query_project` did not erase the negative original condition. Producer diagnostics additionally include `WEBAGENT_EVIDENCE_ROUTE_REQUIRED` and `action_replacement_not_deferred`, not acceptance of a false-positive outcome.
- **IT10 / IT23-A:** Both producer `observe_fact` events show `UNKNOWN`, `OBSERVATION_UNKNOWN`, `REPLAN_REQUIRED` and a later `INTERRUPT/UNKNOWN` accepted terminal. IT23-A required a Progress correction because `INTERRUPTED` is invalid as the `outcome` field; the validator enforced this without turning UNKNOWN into PASS.
- **IT11 / IT12 / IT15:** Positive bound-Observation action evidence and accepted SUCCESS terminals are independently present in producer events. IT12's selected producer events include no invalid-ref Runtime-rejection entry; the evidence supports **Planner avoiding the forged reference**, not claiming a Runtime rejection of an unsubmitted reference.
- **IT13 / IT14 / IT21:** Producer terminal evidence remains `COMPLETE/FAILED` with `verification_status=FAIL`. IT21 specifically shows the original contradiction `observe_fact` at `FAIL` and a later `invalid_progress_payload` rejection; no source producer event proves illegal post-hoc rebinding was accepted.
- **IT16:** Two `run_command` tool events have `execution_status=FAILED` and `verification_status=FAIL`; a later `inspect_directory` succeeds, but the terminal remains `COMPLETE/FAILED`. The original user request expected one short process check; producer records show two process attempts, so exact requested-action multiplicity requires case-specific review even though negative exit evidence was not promoted to PASS.
- **IT17:** Single `query_project` tool event `SUCCEEDED`, with `verification_status=UNKNOWN`, and accepted `COMPLETE/SUCCESS`. Query-task success does **not** supply a distinct Predicate PASS.
- **IT18:** Explicit producer chain: `write_file` `SUCCEEDED/PASS`; `read_file` `FAILED/UNKNOWN` with `status=REJECTED` and `EVIDENCE_ROUTE_REQUIRED`; replacement `query_project` `SUCCEEDED/UNKNOWN`; `protocol_rejected` round 3 with `success_contradicts_runtime_verification`; finally accepted `COMPLETE/PARTIAL` round 4. No greenwash of original full read-back criterion.
- **IT20:** Initial `run_command` action `SUCCEEDED/UNVERIFIED` yielded `VERIFY_REQUIRED`. The verification action initially lacked formal `verify/success_criteria` (`WEBAGENT_VERIFICATION_REQUIRED`), then the subsequent verification tool executed `SUCCEEDED/PASS` and formal `COMPLETE/SUCCESS` was accepted. Transport event alone would not establish this sequence.
- **IT22 — CORRECTED:** Contrary to a possible inference from its final `TASK_FAILED`, the selected producer events **do prove two Runtime tools executed before the Composer obstruction**. Round 1 `inspect_project_ledger` succeeded; round 2 `it22-observe-probe-01` executed and yielded `UNKNOWN`, `OBSERVATION_UNKNOWN`, `REPLAN_REQUIRED`, `terminal_eligible=false`. A round-3 `invalid_progress_payload` diagnostic rejected outcome `INTERRUPTED`, followed by `WEB_COMPOSER_PREWRITE_FAILED`. No accepted terminal receipt or proven action-replay result. Therefore report as **partial execution with a subsequent transport failure**, not `no Runtime action` and not an accepted UNKNOWN terminal.
- **IT23-B:** Only an `EXECUTION_FAILED` event is selected, recording `composer_unrelated_draft_present`. No `tool_started`/terminal producer event is included. Do not infer any runtime action or receipt, and do not clear the unrelated draft.
- **IT19:** Not dispatched, so correctly absent from the 17 producer-case arrays. Its workspace-global transaction isolation risk and Codex safety BLOCKED disposition are unchanged.

**Evidence gate (unchanged):** `planner_round_started`, model response logs, source `[WEBAGENT_TOOL_RESULTS]` prompt-construction code, persisted action ledgers and producer `tool_completed` events are not equivalent to an **exact, post-tool, request-correlated Planner input known to have been sent**. On the supplied handoffs, `0/17` exact inputs are established. No backfilled model-delivery claim, no retroactive PASS. The next Codex handoff must separately record actual captured payload source, model-input correlation, retention/redaction controls, and any `PLANNER_DELIVERY_NOT_OBSERVED` statuses. Avoid posting entire local raw payloads, browser state, conversation URL or CDP details to GitHub.

