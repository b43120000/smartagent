# Source Discovery Bootstrap V1

Base: 3ae920ff7e2ef04423bf5a70b4363424ac1acbd0
Branch: feat/source-discovery-bootstrap-20261107

## Goal

Provide a grounded artifact source reference when the old Task Journal and
Source Reference Index have no usable local source candidates. Do not invent
a local directory, network access, or write authority.

## Implemented

- config/source_discovery_catalog.json: explicit immutable GitHub artifact
  references with an exact Goal Identity; included in Protocol Manifest.
- source_discovery_bootstrap.py: generic indexer, reads only Agent metadata,
  never contacts a remote service or extracts files. Requires full commit
  SHA-1, ZIP path, SHA-256, Git blob SHA-1 and provenance. Every match is an
  E1 reference, not an observation or deployment authorization.
- Same module: register_source_artifact for explicit owner/instance/workspace
  isolated registration, with explicitly_authorized=True. Registration is
  metadata-only and grants no other permission.
- protocol_loop.py: emits source_discovery_bootstrap_completed with state,
  catalog status, artifact candidate count and no implicit authorization.
- planner_context_snapshot.py: passes grounded artifact reference, requiring
  separate remote access authorization, acquisition, hash verification, and
  subsequent source integrity and destination write admission.
- goal_prerequisite_resolution.py: next category
  RESOLVE_REGISTERED_SOURCE_ARTIFACT, not missing-source-path, when a pinned
  reference exists and no usable scoped local source has yet been selected.
- target_path_contract.py: fixes generic Chinese sentence punctuation parsing
  so check_results.py is not omitted from the required root entry list.

## Current Case D artifact

Repository: b43120000/test
Pinned commit: 6b0d680185e40ae654acef03d0eeb9074536fb8e
Archive: goal-driven-replan/v1/SmartAgent_GoalDriven_IT_fixture.zip
Archive root: SmartAgent_GoalDriven_IT
Git blob SHA: a0a2a3e2aa3c0a34a5011f2553c0b232ea657bdb
Declared ZIP SHA-256: c38d634188e23868bd384ad8b0cd12bbe20d778eea8b67338f4ec2bbf747ef62
Top-level expected entries: case_a, case_b, case_c,
fixture_manifest.json, check_results.py.
Content fixture manifest:
goal-driven-replan/v1/SmartAgent_GoalDriven_IT/fixture_manifest.json

The repository is private. An authenticated checkout may be needed. A
source catalog record is not proof the current runtime can reach it.
This patch performs no download, archive verification or deployment.
Network and write permissions remain separate, mandatory gates.

## Live acceptance

Expected after Target is confirmed absent:
  source_candidate_bootstrap_completed candidate_count=0 (may be empty)
  source_discovery_bootstrap_completed state=ARTIFACT_REFERENCE_FOUND
  source_discovery_bootstrap_completed artifact_candidate_count=1
  goal_prerequisite_resolution next_category=RESOLVE_REGISTERED_SOURCE_ARTIFACT

This completes the remote source reference discovery subtask, not D4 itself.
D4 PASS requires a newly admitted source observation and integrity evidence.
D5-D7 require separate authorization, deployment and independent VERIFY.
If remote access is missing, ask for precise GitHub access authorization
or authenticated retrieval capability, not for an unrelated source path.

## Regression

Windows and Ubuntu tests include first-run bootstrap with empty history,
unmatched Goal, corrupt catalog, path traversal, unpinned commit,
cross-owner registry isolation, explicit registration, target contract
Chinese punctuation, prior Case D / Planner / Goal regressions, and SHA256
Protocol Manifest checks.
