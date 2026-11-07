# Shared Blackboard + Capability-Aware Planning (V1)

## Implementation base

- Base commit: `3a2f97cdf8dc16714578687845549664a5b29c0d`
- Implementation branch: `feat/shared-blackboard-capability-strategy-20261107`
- Protocol schemas remain backward-compatible except for one optional operational-action correlation field: `strategy_id`.

## Architecture

V1 introduces one request/Task-Epoch-bound canonical read model:

- `SMARTAGENT_SHARED_BLACKBOARD_V1`
- `SMARTAGENT_RUNTIME_CAPABILITY_MANIFEST_V1`
- `SMARTAGENT_STRATEGY_CANDIDATE_REGISTRY_V1`
- `SMARTAGENT_STRATEGY_TERMINAL_READINESS_V1`

The Blackboard is assembled from existing owner ledgers and Runtime-owned preflight results. It is the single cross-module snapshot consumed by Planner and Runtime terminal admission. The underlying TaskProgress, Action, ActionResult, Condition, Security and Runtime Fact ledgers remain their respective authoritative write stores in V1; the Blackboard does not create a second writable copy of those facts.

## Blackboard sections and write ownership

| Section | Authoritative owner | Meaning |
|---|---|---|
| `goal` | TaskProgress + GoalGap | Original Goal and unresolved, evidence-bound gaps |
| `environment` | Runtime Fact / Action Provenance | Actual observations and software inventory |
| `capabilities` | Tool schemas + interface capability policy | Exposed tools, schema fields and side-effect classes |
| `authorization` | SecurityContext + remote-access receipt | Runtime-owned scope and authorization decisions |
| `actions` | Action Ledger + Action Resolution Trace | Proposed, admitted, rewritten, rejected and dispatched Actions |
| `evidence` | ActionResult + ConditionResult ledgers | Typed execution and verification evidence |
| `strategy` | Attested Progress strategy history + Runtime preflight | Goal-relevant candidate strategies |
| `progress` | TaskProgress + ActionLoop state | Accepted progress and required transition |
| `terminal` | Runtime terminal readiness gate | Whether an untried strategy or unresolved effect blocks interruption |

Every section carries an owner, source-of-truth identifier, authority label, bounded evidence references and a digest. The event projection is derived from current owner ledgers; it is not a replacement event store.

## Runtime Capability Manifest

The manifest describes tools exposed by the active interface, canonical required/optional fields, tool category and side-effect class. `run_command` is marked operation-dependent. The manifest never describes a candidate as authorized and never grants Scope, network or execution permission.

## Strategy candidate lifecycle

Runtime persists only allowlisted, Runtime-attested route metadata from a preexecution strategy receipt. It then derives task-specific candidates from:

1. the immutable original Goal and source requirement;
2. current-epoch target-absence Evidence;
3. registered artifact references;
4. Runtime Git capability and remote-access state;
5. a non-executing CWD/Scope preview; and
6. the exact route recorded in the attested strategy revision.

Candidate states include:

- `UNTRIED`: grounded candidate can be evaluated under normal Admission;
- `NEEDS_ACTION_REPAIR`: candidate remains viable but its canonical Action envelope needs repair;
- `RECONCILIATION_REQUIRED`: an Action outcome or Runtime preflight is unresolved;
- `NEEDS_USER_AUTHORIZATION`: current Scope/authorization does not admit the candidate;
- `NEEDS_REMOTE_ACCESS_EVIDENCE`, `NEEDS_SOURCE_INFORMATION`, `NEEDS_TARGET_OBSERVATION`;
- `ATTEMPTED_REJECTED`, `ATTEMPTED_FAILED`, `SATISFIED`, or `CAPABILITY_UNAVAILABLE`.

A candidate contains an exact `strategy_id`, tool/operation/CWD/target constraints and required Action fields. It is not an executable Action and grants no permission.

## Action correlation

Operational Action envelopes may carry a Runtime-issued `strategy_id`. When an Action matches a currently selectable candidate's tool/operation/CWD/target shape, Runtime requires that exact `strategy_id`; Planner cannot silently use a candidate without its correlation reference. Runtime validates that the ID exists in the latest request/epoch candidate registry and that the submitted tool, operation, CWD and target remain within the candidate contract. The field is correlation metadata and is excluded from semantic Action identity.

The ID is retained in public Action provenance, rejection traces and failure receipts so a strategy attempt can be distinguished from an unrelated Action.

## Dynamic terminal readiness

Before accepting Planner `INTERRUPT`, Runtime evaluates the same candidate registry that was included in the immediately preceding Planner Context:

- untried, goal-relevant candidate -> reject premature interrupt and request `CONTINUE` with that candidate's `strategy_id`;
- schema-repairable candidate -> require canonical Action repair;
- unresolved execution/preflight -> require reconciliation;
- candidates blocked by genuine user authorization or missing prerequisite Evidence do not masquerade as runnable strategies;
- no grounded candidates or exhausted candidates leave the existing context/permission/terminal Evidence gates in control.

A terminal block is a Runtime constraint, not a Planner strategy choice. It does not select or execute an Action. All normal Tool, Scope, Network, Approval and independent Verification gates remain mandatory.

## Safety invariants

- Original Goal and request/epoch identity are unchanged.
- Candidate generation and CWD previews are read-only.
- Candidate presence is never authorization.
- Planner proposals never become Runtime facts or Evidence.
- Unknown is not false; unobserved target absence cannot produce an untried deployment candidate.
- An Action with a strategy ID must preserve the candidate's tool/operation/CWD/target constraints.
- A candidate attempt with unknown effects requires reconciliation before terminal interruption.
- Existing owner ledgers remain authoritative; the Blackboard is a canonical, bounded cross-module read model.

## V1 boundary / follow-up

V1 unifies the Planner-facing state contract and makes candidate strategy use traceable. It does not yet migrate all persistent ledgers into one append-only physical event store, nor claim that the Runtime can enumerate every conceivable strategy. The candidate registry is explicitly a bounded set of Runtime-grounded alternatives. A later migration can move owner-ledger persistence behind the Blackboard event API without changing the section and identity contracts.
