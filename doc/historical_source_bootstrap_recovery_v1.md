# Historical Source Bootstrap / Evidence Recovery V1

Base commit: 3d634a97b63793f1e8f7ca5be1bc547fae30258d
Branch: feat/historical-source-bootstrap-recovery-20261107

## Goal and hard boundary

At request startup, recover provenance-grounded source *location hints* that
may reduce rework in later Goal-Driven plans. A historical path is never a
current filesystem observation, admission grant, file hash proof, or reason
to infer access to a target's parent/siblings.

Historical query is not a new Tool action: it reads only Agent-owned metadata
and already committed journals; no search of E:\ or the user's source tree.

## Implemented P0-P3

P0 - source_goal_identity.py: exact normalized destination plus sorted required
top-level entry names; user labels such as "Case D" are not keys.

P0 - historical_source_resolver.py: index-first boot. On a miss, bounded
historical TaskContinuationStore source records are scanned only when their
owner, Agent instance, workspace and Goal Identity match. Only originally
user-labeled source paths (E1) or properly correlated successful structured
Runtime list_directory receipts proving required top-level names/types (E2)
are recovered. Optional explicitly bound Project Ledger events are weaker
E1 pointers. No general natural-language summary scraping. Ambiguous or
unindexed historical paths stay UNKNOWN.

P1 - source_reference_index.py: bounded Agent runtime metadata (not project
files), keyed by owner, Agent instance, canonical workspace and exact goal
digest. States: INDEX_NOT_FOUND, INDEX_EMPTY, INDEX_INVALID, INDEX_READY,
CONTEXT_UNAVAILABLE. Corrupt indexes fail closed. Stronger E2 provenance is
not replaced by E1 for the same path. All persisted paths remain unverified
in the next request.

P1 - task_continuation.py: public source_history_records reads only bounded
same-owner/instance/workspace archived records and list_directory envelopes,
never executable command bodies, replay actions, or unrelated user tasks.

P2 - source_candidate_ledger.py: include historical origin, evidence_level,
index_status, bootstrap_state, recovered count, and task history count. The
existing interrupt_readiness continues to check explicit read-path scope
before proposing a specific read. The actual SecurityContext/Tool Admission
remains authoritative.

P3 - protocol_loop.py: perform bootstrap once during request initialization,
publish bounded status events, and index a source only after a real same-task
list_directory receipt proves required top-level names/types. Persist in the
Agent installation runtime root, not the currently selected project.
Planner Context receives a historical_source_bootstrap diagnostic, plus the
source candidate and integrity views. On new current-epoch evidence, the
existing Goal-Directed Replan can progress to source content verification
and safe deployment admission.

## Example transitions

Cold start with a prior correlated receipt:
INDEX_NOT_FOUND -> RECOVERED_FROM_HISTORY -> E2 SOURCE_CANDIDATE.
If the source is in this request's authorized read scope:
inspect_directory -> list_directory -> independently validate content.
If source is outside scope:
SOURCE_SCOPE_AUTHORIZATION_REQUIRED.
If old tasks lack any structured source evidence:
NO_MATCHING_STRUCTURED_SOURCE (safe interruption is expected).

This V1 does not grant write permission, create missing sources, install,
copy, deploy, infer file contents, or report success. Independent VERIFY
and actual Windows Case D deployment remain separate live gates.

## Regression

Windows and Linux CI:
- Cold-start source receipts, weaker original labels, index hit, corrupted
  index, exact goal filtering, cross-owner/workspace isolation, no-source
  case, failed receipts, optional contextual Project Ledger pointers.
- Previous source candidate grounding, Case D, Goal Gap, Planner Context,
  Goal Transition, Context Acquisition, Action Loop and SHA256 manifest checks.

Legacy source_history_v1.json files lacking owner/instance binding are not
silently migrated to the new multi-owner index. The migration policy must
not assume that shared historical metadata grants access to another owner.

## Live RCA readout

Look for source_candidate_bootstrap_completed:
  state: INDEX_HIT | RECOVERED_FROM_HISTORY |
         NO_MATCHING_STRUCTURED_SOURCE | CONTEXT_OR_GOAL_UNAVAILABLE
  index_status: INDEX_NOT_FOUND | INDEX_EMPTY | INDEX_INVALID | INDEX_READY
  candidate_count: nonnegative
A nonzero candidate count is only Bootstrap evidence, NOT Case D D4 PASS.
D4 requires a new admitted source observation; D5 write authorization,
D6 deployment and D7 independent VERIFY are still distinct gates.
