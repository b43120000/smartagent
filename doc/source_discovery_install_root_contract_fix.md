# Source Discovery Installation Root Contract Fix

Base: 284bd70b453574ad13a059ec68c815925c5700ca

## Observed failure

Previous Case D Live RCA showed:
- installed_git_head matched the Source Discovery Bootstrap commit;
- source_discovery_bootstrap_completed occurred;
- artifact candidate count was zero, even though the sealed catalog is in
  config/source_discovery_catalog.json with the exact Case D Goal.
- The Telegram worker passes TaskContinuationStore(root) where
  telegram_webagent_worker.ROOT points to the source directory.

Root-cause analysis: the previous Bootstrap used continuation_store.root/config
as its packaged catalog. That is a Task state root, not the package installation
root. A matching repository artifact therefore could not be loaded.

## Remediation

The packaged, sealed source catalog is read from
  agent_core.paths.install_root() / config / source_discovery_catalog.json

Only owner-specific runtime registrations are read from the explicitly supplied
  agent_core.paths.runtime_root(task_state_root) /
      source_artifact_registrations_v1.json

The active workspace is reserved for user-specified work and never used to infer
the code/package installation root. The API now accepts an optional explicit
installation_root injection for isolated tests. Production Protocol Loop passes
the canonical install_root() and TaskContinuationStore.root separately.

No Tool Scope, download, GitHub authentication, source integrity, or destination
write authority is granted by this change.

## Sanitized Live RCA evidence

source_discovery_bootstrap_completed now emits:
- catalog_path_authority = INSTALLATION_ROOT
- registration_path_authority = TASK_STATE_ROOT
- goal_identity_status
- catalog_status and registration_status
- catalog_records_examined, catalog_matched_count
- catalog_goal_mismatch_count, catalog_invalid_record_count
- registration_records_examined, registration_matched_count
- artifact_candidate_count

collect.bat now preserves those names as safe structured codes or counters
within the sanitized timeline (not raw absolute paths). The collector is added
to Protocol Manifest SHA-256 protection.

## Expected Live Case D milestones

1. source_discovery_bootstrap_completed:
   catalog_path_authority=INSTALLATION_ROOT
   catalog_status=READY
   goal_identity_status=READY
   catalog_records_examined=1
   catalog_matched_count=1
   catalog_goal_mismatch_count=0
   artifact_candidate_count=1
2. Planner receives the registered GitHub archive and chooses
   RESOLVE_REGISTERED_SOURCE_ARTIFACT rather than requesting an unknown local
   source path. Confirm via terminal summary / subsequent evidence.
3. D4 does not pass until the source is actually acquired by an authorized
   mechanism and independently observed / verified. No write/deploy/VERIFY
   is claimed by fixing the Catalog lookup.

## Cross-platform tests

source/WebAgent/tests/test_source_discovery_bootstrap.py includes:
- real packaged catalog resolution from the actual Telegram Worker source/
  Task state root, even without fake catalog state;
- distinct package/state roots and decoy catalog protection;
- diagnostics for goal mismatch and missing catalog;
- the existing 11 first-run Source Discovery tests.

Windows and Ubuntu CI additionally rerun Historical Recovery, Case D goal,
Planner Context, Goal Prerequisite, Action Loop, and Protocol Manifest integrity.
