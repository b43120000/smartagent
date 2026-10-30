# Tri-One Shared Runtime: Answer-only Completion Contract (2026-10-31)

Baseline: `4c706e48bf3ecdc037e7b7f3fb5e5fb17b416204`.

## One shared V9 Runtime, distinct ingress and reply routes

`Telegram / WebCopilot / Web Direct / Local Test` all use the existing shared V9 Runtime and its `report_progress + final_response + turn_commit` transport. The original request source and destination/TaskResult routing are unchanged. This work **does not** introduce a GAR-only or Provider-specific terminal format.

## Conditional Action Evidence semantics

- Ordinary Progress and every action-bearing/nonterminal request still requires the canonical `evidence_refs` field, and real Actions must cite verified request-scoped Action Result evidence.
- When trusted Runtime policy confirms an eligible **text-only `COMPLETE/SUCCESS` with a single exclusive `final_response`**, with no tool execution/history, unverified pending operations, blockers, attachments, externally dependent tasks, or continuation, the model may **omit** `evidence_refs` entirely. The existing shared Progress materializer turns only that authorized omission into `[]` internally.
- An explicit `evidence_refs: []` stays supported, as before.
- An explicit **nonempty** `evidence_refs` array is **never erased**. If it contains only `REQUEST_ACCEPTED` or `RUNTIME_STATUS` for an otherwise eligible Answer-only terminal, Runtime rejects it with a specific repair diagnostic rather than incorrectly requesting Action Verification or accepting an unverified claim.
- These status markers may be listed as available references but do not establish Tool execution, verification PASS, or Answer-only success.
- A text-only receipt's internal `evidence_refs: []` is metadata, *not* evidence. All existing scoped V9 admission, Progress validation, Action/Result ledgers, terminal receipt, safe-pause, outbox and delivery controls remain in force.

GAR L4 remains advisory and does not manufacture Action Result evidence. It resumes the original Planner under the same shared completion rules.

## Regression checks

Offline from repo root (`PYTHONPATH=source`):
- `python -B source/WebAgent/tests/validate_progress_reply_contract.py`
- `python -B source/WebAgent/tests/validate_tri_one_5a_answer_only.py`
- `python -B source/WebAgent/tests/validate_tri_one_5a_protocol_it.py`
- `python -B source/WebAgent/tests/validate_tri_one_5b_shared_worker.py`
- `run_gar_regression.bat`
- `run_gar_debug_regression.bat` (strict 204-file manifest)

Coverage includes omission across Telegram, WebCopilot, Web Direct and Local Test; explicit empty/nonempty refs; a status-marker rejection followed by correctly omitted evidence; feature OFF; real Action/Pending blockers; and external-information tasks not routed through Answer-only.

**Do not claim GAR D1 Live PASS** until a separate same-request L4 -> WebGPT2 -> WebGPT1 -> terminal SUCCESS run is collected and verified. No main merge, Update.bat, publisher, or existing Controller changes were authorized.
