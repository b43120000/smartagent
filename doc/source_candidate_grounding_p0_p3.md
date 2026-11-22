# Source Candidate Grounding and Evidence-Guided Source Resolution P0-P3

Base version: 4204b73f3c1ab4ad419c1004deedef878020a7b6.
Generic Goal-Driven prerequisite resolution, not a Case D hardcoded path.

## P0 - Source Candidate Ledger

The bounded source_candidate_ledger reconstructs provenance-linked source
leads from literal source-labeled paths in the user request, successful
current-request and current-epoch list_directory receipts, and explicit
Runtime-owned historical leads. Source candidates are NOT observed content
or read authorization. The requested target is excluded.

The source_history_registry records a previous source path only when the
actual Runtime list_directory receipt proves all required top-level entries
are present with expected types. It saves a location hint in SmartAgent's
own internal runtime metadata, keyed by exact target identity. A future
request must recheck source contents and request-scope authorization.

Old tasks whose source path was never saved cannot be retroactively
reconstructed from this new index.

## P1 - Scoped Read Readiness

The interrupt_readiness module prioritizes grounded source candidates before
generic discovery. Descendants of explicitly declared read paths are
candidates for Tool Admission only; no candidate grants access. Paths
outside declared read scope produce precise authorization questions rather
than speculative reads. Missing target does not grant parent or drive access.

## P2 - Source Integrity Evidence

The source_integrity_resolution module checks top-level names/types against
the Target Path Contract using same-epoch, committed, SUCCEEDED list_directory
receipts. STRUCTURE_VERIFIED does not imply content hashes, semantics,
deployment readiness, authorization or terminal success.
Full content integrity remains unverified until separately established.

## P3 - Planner Context and Interrupt Admission

Planner Context receives source_candidate_ledger and
source_integrity_resolution and a derived goal_prerequisite_resolution.
A grounded candidate within the declared scope should be inspected next
through ordinary Tool Admission. A structural proof points to independent
content verification. Out-of-scope candidates require specific approval.
The Runtime rejects requests for an already-grounded, unexhausted source
path while preserving legitimate permission requests.

## Security and live gate

No broad scan, ACL bypass, source mutation, automatic user-project write,
fabricated success or implicit scope expansion. The prior Case D run knew
only the missing destination and had no proven source within its read scope.
This patch can prevent redundant searching, preserve successful source
provenance and prioritize permitted source candidates. It does not by itself
create permissions or recover paths never captured in past metadata.

Regression tests cover source provenance, incomplete structure, changed
epoch, denied scopes, history persistence, direct creation, Case D,
Planner Context, Action Loop and full Protocol Manifest hash checks on
Windows and Ubuntu.
