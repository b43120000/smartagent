# GAR-F828C8E-VALIDATION-20261029-01 — targeted source correction and retest

Original tested candidate: `f828c8e67e9a900246a9093676bf9cf799783e29`.
This branch is a **new candidate**, not a retroactive PASS for the original SHA.

## Ground truth from Codex validation

- Shared Runtime UT: 22/22 PASS (original candidate only).
- Offline V9 IT: 6 PASS, 1 FAIL, 1 ERROR (original candidate only).
  - FAIL: `test_unverified_terminal_precondition_is_not_unknown_side_effect`: the test expected a retained `pending_verification_requirement`, got `{}`. Do not fabricate verification or relax the V9 terminal gate to satisfy the assertion; the failing diagnostic must be captured.
  - ERROR: `test_l4_fresh_planner_can_complete_text_only_goal`: `GAR_FRESH_CONTEXT_UNAVAILABLE`. The new Planner was not shown to produce an accepted V9 terminal.
- Strict protocol Manifest passed (203 core files, original candidate).
- One true-browser WebCopilot LOCAL_TEST ingress (not manual user GUI) reached a safe L5 at round 1: `GAR_PENDING_OR_BLOCKED_OPERATION`, single UNKNOWN pending key in redacted checkpoint, no blockers, zero accepted/rejected GAR turns. No original request was repeated.
- The report is evidence of **safe pause**, not success, and is insufficient to identify the exact pending field or actual loaded Worker Source.

## Source changes proposed for next candidate

1. **GAR terminal prerequisite classification.** Permit only a clearly identified non-executed `UNVERIFIED` VERIFY prerequisite without any action/result/condition history, with an exact verification contract and no evidence references. Some Runtime diagnostics are emitted before `next_actions` is materialized; this case is *advisory*, not an in-flight mutation. All other pending operations, side effects, OPEN/UNKNOWN results, blocking actions, and any missing provenance remain HARD safety blockers. Original terminal acceptance is unchanged.
2. **L4 fresh reply repair.** A fresh, identity-verified L4 Planner can itself emit an invalid protocol reply. It may receive at most **two in-place schema repairs** from the same new conversation, with one common rejected-turn budget, and **no second fresh conversation/session**. On further rejection or exhausted global budget, stop at L5. This repairs premature `GAR_FRESH_CONTEXT_UNAVAILABLE` escalation without granting tool permissions or success.
3. **Redacted classification diagnostics.** Emit `gar_pending_classification` with finite internal pending-state names, true/false advisory classification and ledger counts; also emit `gar_fresh_context_protocol_repair`. Do not emit raw user prompts, paths, tokens, action bodies, or DOM data.
4. **Regression coverage.** Add positive and negative shared Runtime UT for unmaterialized but canonical prerequisite vs hard blockers, and for bounded fresh L4 reply repair. The V9 IT tests for the two reported failures retain their original acceptance gates; L4 negative IT now explicitly checks exactly two in-place repairs and one fresh session.

## Mandatory acceptance sequence

A. At exact candidate SHA: `require_protocol_manifest('.')`, the entire shared Runtime UT, full V9 IT (all cases), L4 Browser Adapter tests, stale readback regression, and Goal1 2A/5A/5B regressions. Record actual exit codes and reason codes for every failed assertion. Any Offline IT failure **blocks Live**.

B. Only if A truly passes: one **new** no-side-effect user-intent prompt via the official WebCopilot LOCAL_TEST → real Browser Planner → V9 Runtime → original-interface final render, using the user's existing prepared closed Windows installation. Explicitly distinguish LOCAL_TEST from manual GUI. Do not replay the historical paused task or invent success.

C. L4-specific validation requires a separate, pre-opened, independently identified fresh-context tab in the *same* BrowserContext and an actually observed `gar_fresh_planner_activated`; a normal one-round text-only success may validate normal end-to-end delivery but does NOT prove L4. Only one bounded fresh L4 test after offline gates, without side effects.

D. Report a short result package: exact Source SHA, clean/dirty state, how the target checkout was preserved, UT/IT counts, Manifest, first-offending diagnostic reason, pending-state kinds, true-browser ingress/outcome, L4 reached/activated or not, loaded Worker Source attested or not. Evidence stays local; do not paste raw secrets/paths/browser logs.

Scope boundaries: no main merge; no forced Worktree; no destructive checkout or lost stash; no auto replay of old task; no silent safety-threshold increases; no unrelated GAR redesign. All tests required to support claims of PASS. If still failing, return exact evidence and stop.
