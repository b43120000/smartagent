# Git68 — Runtime-owned Finish Contract V2

## Scope
Git65 Case D Reset Deployment executed 91 ToolResults and then failed to
terminalize: the legacy Finish required one `expected_runtime` claim per
action, while its parser/comparator enforced a maximum of 64. A long
`summary` can also trip the 3,000-character Decision limit.

## Wire format
For tool tasks the Planner should prefer:

```json
{
  "type": "finish",
  "contract_version": 2,
  "summary": "P0-P3 independently verified; request runtime acceptance.",
  "goal_checks": [
    {
      "criterion": "Eight fixture file hashes match source",
      "evidence_ref": "MIN-EV-<real 24 lowercase hex>",
      "field": "data.<actual scalar path>",
      "operator": "eq",
      "expected": true
    }
  ]
}
```

V2 does **not** accept `expected_runtime` claims. Existing V1 remains
supported for backward compatibility.

## Trust boundary
1. Planner proposes a Finish and explicit typed goal predicates, not a
   rewritten history.
2. Runtime Adapter binds Task / Request / Epoch and Frozen Goal Contract.
3. Comparator independently reads the full, request-bound Executor Blackboard
   and immutable ActionResult ledger, verifying action identity, digests,
   status and side-effect uncertainty before evaluating the goal.
4. A COMPILED goal is verified against the frozen deterministic contract.
   An UNCOMPILED goal needs trustworthy Runtime-confirmed predicates and
   continues to report `MODEL_DECLARED_RUNTIME_EVIDENCE_MATCH`: this is
   not universal semantic proof.
5. Runtime alone creates the terminal verdict and receipt.

## Negative requirements
- No automatic repair or replay of mutating actions.
- No conversion of denied proposals into successful evidence.
- No guessed Evidence IDs or unchecked claims.
- No model-controlled Task/Request/Epoch or frozen goal digest.

## Offline / CI validation
Run `PYTHONPATH=source python -m pytest -q
source/WebAgent/tests/test_git68_finish_contract_adapter_v2.py`.
The Git68 workflow runs the new and inherited Finish regression tests on
Ubuntu and Windows. Production Windows Live E2E requires the SmartAgent
installation and its own E:\\AIspace test fixtures; CI is not a substitute.

## Git68 hardening after rebase onto Git67 (2026-11-20)

- **Tool-task Finish:** Executor-only tasks with any tool activity, or an
  evidence-required goal, must submit the explicit V2 Finish proposal. The
  Runtime rejects an omitted/legacy V1 version and requests a fresh V2
  proposal in a non-executing Finish-repair turn. It never silently strips or
  reinterprets V1 claims. Pure answer-only legacy compatibility is unchanged.
  The V1 comparator remains available for historical direct compatibility
  tests.
- **Original stdout authority:** V2 uses the complete Runtime-owned ledger.
  The Planner need not copy multi-line/long stdout or preserve its tabs. An
  intentionally submitted goal predicate is still independently checked; V2
  never promotes an unchecked Planner summary into PASS.
- **Blocked P0:** An executed failing ToolResult is retained unchanged.
  A model blocked response records a bounded, typed Runtime blocker
  reference and exit code (when known), returns BLOCKED not SUCCESS,
  and pauses without replaying any side-effecting action. Evidence absence
  stays NO_RUNTIME_BLOCKER rather than guessed as a real failure.
- **GitHub source preflight:** github_source_stage_evidence adds
  non-authorizing, per-action diagnostics to the Executor Blackboard and
  Planner preview. Stages: REMOTE_REFS, REMOTE_FETCH, LOCAL_COMMIT_OBJECT,
  PINNED_TREE_BLOB, LOCAL_BLOB_HASH, LOCAL_ARCHIVE_SHA256. DNS resolution
  failure is distinct from repository access failure; remote ref read only
  confirms repository read access, not credential identity or the pinned
  commit. Local commit and blob observations are not proof of remote source
  identity until explicitly bound and compared. A failed required P0 must
  stop further mutation.
- **Git67 inheritance:** Git68 was rebased onto
  2cc3eadfdb8aa851c04096ccf15fc9b0033b24de, preserving Git67
  path_integrity_trace, strict parser checks, and associated sidecar tests.

### Verification gates

The Git68 workflow includes the V2 comparator tests, new hardening tests,
Git67 parser/path localization regressions, and inherited Finish tests on
Windows and Ubuntu. Passing offline/CI tests does NOT constitute a
Windows SmartAgent normal-Runtime Case D P0-P3 deployment PASS.
Run a separate Live E2E with original Fixture safety constraints and GAR
Evidence; do not clear files if GitHub source verification is incomplete.
