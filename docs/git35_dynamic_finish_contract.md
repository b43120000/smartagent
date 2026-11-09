# Git35 — Task-Generated Finish Schema (not tool-specific templates)

Base commit: be5923cf70f91956507ef7e75ff51533c6fdcc38 (Git34).

## Principle

The Model is responsible for generating its own Finish declaration based
on the original user task and observed Runtime ToolResults. Runtime is not a
semantic planner, and it does not populate expected field values for the model.

Flow:
Original request → PokeClaw Agent Loop/ToolResult → persistent Blackboard
→ Model-generated expected_runtime and goal_checks → Runtime Evidence Readback
→ typed field comparison → bounded original-goal proof / verified model-defined
criteria → terminal PASS or FAIL → common Tri-One TaskResult and transports.

Runtime provides the model a dynamic field catalogue extracted from its
actual ToolResult receipts. Field names come from live data, not hardcoded
lists of required inspect_directory, create_directory or run_command fields.

A model-generated Finish can contain:

~~~json
{
  "type": "finish",
  "summary": "Observed result and task-specific conclusion",
  "expected_runtime": [
    {
      "evidence_ref": "MIN-EV-... from the Runtime",
      "fields": {
        "status": "SUCCEEDED",
        "data.task_specific_scalar_field": true
      }
    }
  ],
  "goal_checks": [
    {
      "criterion": "A completion condition from the original request",
      "evidence_ref": "MIN-EV-... from the Runtime",
      "field": "data.task_specific_scalar_field",
      "operator": "eq",
      "expected": true
    }
  ]
}
~~~

goal_checks is required when no independent Runtime goal contract exists.
For supported bounded tasks, an independent Goal proof is preferred and
goal_checks is optional. These claims are not authorization.

Runtime checks all executed actions, request/epoch, SHA256 receipt identities,
complete evidence_refs, strict typed path/value equality, and the submitted
predicates against actual ToolResult. A submitted Finish with no task-relevant
facts, or no coverage basis, is FAIL. No matching UNKNOWN can yield PASS.

### Finisher-only repair

Incomplete model-declared schema may be returned to the model, no more
than twice, while the Agent Loop remains running. This only asks for a
revised Finish using the same observed ToolResults. It does not resend any
ToolCall, rerun a mutation or alter the Runtime evidence. Semantic mismatch,
unsafe execution or invalid actual evidence is not repairable by declaration.

### The semantic boundary

Runtime can deterministically verify a model-generated criterion against
real observed values. Runtime cannot independently prove that model-created
criteria cover every clause of arbitrary natural-language tasks. For
finite supported contracts, the receipt says
coverage_basis=RUNTIME_INDEPENDENT_GOAL_PROOF. For general tasks,
coverage_basis=MODEL_DECLARED_RUNTIME_EVIDENCE_MATCH. This provenance is
preserved in the Blackboard, terminal receipt and Tri-One TaskResult; it must
not be misrepresented as an independent semantic Goal proof.

The exported terminal verdict remains PASS or FAIL; raw Tool Execution
FAILED can coexist with Goal PASS for an expected negative result.

No reactivation of Goal Driver or Prerequisite. No weakening of tool admission,
workspace scope or write ceiling.

## CI

Git35 test suite validates task-generated Finish fields, missing-field
repair without re-execution, arbitrary command-oriented Goal predicates,
negative Exit Code (Tool FAILED vs Goal PASS), conflicting goal states and
Tri-One provenance; also runs Git34/33/31 and shared-interface regressions.
