# Direct Git workflow

The single development and execution checkout is `C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1`.
Its canonical remote is `https://github.com/b43120000/smartagent.git`; use `main` for the integrated runtime.
The VPCS MVP is integrated into `main` through PR #7. After verifying the merged `main` locally, use its commit as the baseline for `feat/runtime-evidence-foundation`.

The former publication checkout `E:\card\release\SmartAgentv2` is archived. Its original files and branches remain available locally, but its push URL is disabled. This workflow supersedes the older two-checkout transfer plans in this directory.

## Daily work

Open PowerShell in the execution checkout:

```powershell
Set-Location -LiteralPath 'C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1'
git status
git switch main
git pull --ff-only
# Make and validate the intended changes, then stage explicit application paths.
git add <reviewed-paths>
git commit -m '<description>'
git push origin main
```

Keep the working tree clean before switching branches or pulling. A non-fast-forward pull stops for review; it does not automatically merge divergent work. Do not use force push or publish private archival refs. The local configuration uses `pull.ff=only`, `push.default=simple` and `push.followTags=false`.

The historical `install_smart_agent/git_release.ps1` transfer command is no longer part of this workflow. Do not use it to copy commits into the archived checkout. Retired update1/sync_back entries are not part of the direct Git workflow either.

## Local state and recovery

Personal DOM calibration (`dom_config.json` and `dom_config.history`), browser state, `config/debug_config.json`, `localdata`, `.agents`, credentials and installed `.venv` contents stay local and ignored. Only immutable allowlisted `.venv/portable-runtime` and `.venv/portable-git` payloads are versioned. Never clean ignored runtime data with `git clean -fdx`.

The consolidation preserved old branch tips in the private local namespace `refs/consolidation/{execution,upload-package}/...`. List them with `git for-each-ref --format="%(refname) %(objectname:short)" refs/consolidation`; create a new branch from a selected full ref if recovery is needed. These refs are outside normal branches/tags and are excluded from ordinary branch/tag pushes. Never use `git push --mirror` or explicitly publish `refs/consolidation/*`; the archived history and backup bundles stay private.

The migration backup and recovery instructions are local at `E:\card\archives\smartagent-consolidation-20261023-035936\README.md`. The archive includes both original all-ref bundles, Git state/configuration and personal-setting snapshots. Git history alone does not restore ignored calibration or installed runtime data.

## Runtime and validation

Root operation entries are ACLstatus.bat, Edit_workspace.bat, force_stop_all_agents.bat, launch_remote_agent.bat and launch_webcopilot.bat. Installer/check/calibration support entries live under `install_smart_agent/launchers`; calibration stages live under `adapterUI`. Main instructions are in `doc/README.md`.

Run `install_smart_agent\launchers\ensure_portable_runtime.bat` when runtime extraction or validation is needed. It validates bundled Python/Git and refreshes `localdata/metadata/environment_ledger.json`; machine Python/Git are not required. Existing extracted runtimes and credentials stay private.

Use the bundled `.venv\git\cmd\git.exe` if Git is not on PATH. Before publishing code changes, verify the protocol manifest and run focused tests. Keep protocol and update manifest hashes consistent with the exact tracked file bytes; a normal Git commit is not a newly packaged release.

Default provider DOM files remain versioned. Other URL calibration is origin-specific. Generated verification evidence belongs in localdata and must not be published with browser state or credentials.

## R014-B: host-local marker isolation (Direct Git only)

Git still owns source code, the verifier `source/agent_core/protocol_manifest.py`, and the **tracked** canonical build inputs `config/protocol_manifest.json` and `config/update_manifest.json`. A `.gitignore` entry does not untrack these files. Do not use `skip-worktree`, `assume-unchanged`, reset or force checkout to hide tracked changes.

Machine-specific records under `localdata/`, `.agents/`, personal DOM/binding/config files and ignored environment data must not be required to match Git or another installation. Local diagnostic hash observations, if written, belong only under ignored `localdata/metadata/` and are **not** source or Worker attestation. The pure comparator in `source/agent_core/git_local_marker_policy.py` classifies entries for **cross-install reporting only**; it neither authorizes a deployment nor overrides Git's tracked-file conflict behavior. An installed dirty tracked canonical manifest remains a deployment conflict requiring review even if its entire hash is exempt from cross-install equality.

A pinned candidate is verified against its **own** strict `CORE_FILES` Manifest with no missing or mismatched hashes. Before any Live or deployment, create its canonical Manifest through the supported `build_protocol_manifest()/write_protocol_manifest()` mechanism, commit exact bytes to the candidate branch and pass native `validate_protocol_manifest()` on the new clean source SHA. Preserve the original failing run evidence. Neither an ACL-OFF software-only bypass nor a locally regenerated observation is a strict PASS.

The release builder intentionally regenerates staged `config/protocol_manifest.json` plus `config/update_manifest.json` and `config/source_build_provenance.json`, and `source_attestation.py` checks their internal binding. Those are build/provenance records, **not** freely interchangeable machine tags. Moving the canonical runtime manifest under `localdata/` would change these interfaces and requires separate design; not part of R014-B. The retired `Update.bat` / update transfer / publisher branch is excluded from this task. Preserve the original dirty installed checkout, protected tasks and Writer; use a separately reviewed reversible deployment procedure.

Focused offline policy regression: `python -B source/WebAgent/tests/validate_r014_git_local_marker_policy.py` with `PYTHONPATH=source`. It proves local records are excluded from cross-install equality, canonical files still report tracked conflicts, and a stale candidate-local manifest remains a hard FAIL.

