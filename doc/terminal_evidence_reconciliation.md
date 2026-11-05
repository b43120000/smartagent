# P0 Terminal Evidence Reconciliation — 2026-11-06

## Observed incident

Live RCA for TASK-519592C38B0CB1DDCE21 / source 77b03a29:

- Round 13: deployment MUTATE PASS.
- Round 14: independent VERIFY PASS.
- Round 15: terminal path gate UNVERIFIED with
  PREEXISTING_ENTRY_CHANGED_REQUIRES_RECONCILIATION.
- Rounds 16–21: repeated inspection / rejected commands;
  eventually PAUSED by Repair elapsed budget exhausted.

Earlier gate compared all direct-root entry metadata, including directory
mtime_ns and st_size. This incorrectly treated harmless directory metadata
drift as proof of changes to preexisting user data.

## Preservation evidence, instead of timestamp equivalence

At request entry, BEFORE any tool action, Runtime performs an authorized
read-only, bounded recursive capture of preexisting paths under the exact
requested target. It remembers each original directory/file identity and
SHA-256 of each original regular file. It does not ingest file content into
Planner context or mutate any source. Bounds: 512 nodes, 64 MiB hashed,
maximum depth 10, fail closed on symlinks/junctions, special files, failed
authorization, unreadable data, concurrent changes, or incomplete snapshot.

At terminal, after independent Action Verification PASS, Runtime checks every
original path still exists with its same kind and filesystem identity and
re-hashes every original file. New direct-root required entries can be added
and new files can be created under preexisting folders; directory mtime/size
changes alone are ignored. Same-size changed bytes are rejected.

- Verified preservation + exact-root required entries + independent action
  PASS + demonstrated new deployment: allow Terminal Goal PASS.
- Existing file removed, replaced, or modified: UNVERIFIED with a precise
  preservation reason, never retroactively authorize rewriting data.
- Pre-mutation snapshot missing, out-of-scope, depth/file/byte budget exceeded,
  or unreadable: UNVERIFIED. New later probes CANNOT fabricate pre-mutation
  evidence.
- Existing source unchanged with no newly deployed payload still needs
  independently committed reuse evidence (unchanged from previous gate).

No auto-move, delete, overwrite, or budget relaxation. RepairStagnation hard
limits stay intact. Avoiding a false terminal rejection is what prevents
the observed 5/5 -> repeated read commands -> PAUSED path.

## Regression

CI runs Windows and Linux / Python 3.13:

python -W error::SyntaxWarning -B source/WebAgent/tests/test_target_path_contract.py
python -W error::SyntaxWarning -B source/WebAgent/tests/test_goal_repair_p0_env.py
python -W error::SyntaxWarning -B source/WebAgent/tests/test_auto_rca_upload.py

Additional tests: innocuous nested directory metadata changes, same-size
preexisting file content change with restored mtime, removal of nested file,
file-to-dir replacement, missing pre-mutation snapshot, bounded failure,
and authorized new nested content.

## Live acceptance

Rerun the existing Case D prompt without deleting existing directories.
Expect Terminal Target Path PASS only if root direct entries are correct,
all original data is verified unchanged, and independent Action Evidence
passes. Confirm collector and Terminal Receipt. Any new preservation violation
must remain UNVERIFIED (safe, no false-green).

## Already-complete destination: safe revalidation without redeployment

On a fresh request the previous interrupted deployment may already have all
required files directly at the requested root. The original strict gate would
otherwise reject an unchanged destination, causing unnecessary copy/retry.

Terminal reuse is available only when ALL of these are true:

1. The original human request explicitly asks to revalidate / recheck.
2. All declared required top-level entries are already at the exact target.
3. The pre-mutation preservation snapshot is READY and rechecked VERIFIED.
4. Ordinary Terminal Action Evidence passes independently.
5. A distinct `run_command(operation=VERIFY)` executed and passed in the
   current Request/Epoch, using the **exact original target** as
   `working_directory`; its Action ID appears in terminal `evidence_refs`.
6. The receipt is still ACTIVE and terminal-eligible.

The resulting Terminal Reason is `EXISTING_ROOT_REVALIDATED`, with
`reuse_verification_ref` and `mutation_applied=False`. This is not evidence
that a *new* deployment ran in the current request, and no old terminal
receipt or unrelated PASS can satisfy the reuse gate. The VERIFY action
must independently validate the user's required content/integrity, not just
report file existence. A generic deploy request without revalidation intent
still cannot succeed based on unrelated preexisting content.

The original 600-second stagnation deadline and all authorization/scope
ceilings remain unchanged; this change prevents spurious terminal rejection
in the already-satisfied state rather than increasing retry budgets.
