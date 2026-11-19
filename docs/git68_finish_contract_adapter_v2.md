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
