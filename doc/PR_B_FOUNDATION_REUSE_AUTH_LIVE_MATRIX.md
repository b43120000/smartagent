# PR-B Foundation — Environment Reuse & Authorization Integrity

Development base: 6f2603b644f12dcf4271a8afe3f967fbf5d62174. Branch only; NO main merge.
This plan separates safe read-only Live, future PR-C/D mutation Live, and offline hostile tests.
A Tool SUCCEEDED is not automatically a verified business Task SUCCESS.

## Target six-stage lifecycle

1. Read the previously saved project environment index.
2. Independently re-probe current version, path, and actual task usability.
3. Remediate only when missing, expressly requested, and independently authorized.
4. Perform a separate linked VERIFY, then Resume the original blocked operation.
5. Publish project/.agents/project_context/environment.json only with independent write ACL and exact metadata ceiling.
6. Next Request with new task/request/epoch re-probes candidate and executes the goal with zero installer/MUTATE.

A satisfied version constraint proves metadata consistency, NOT guaranteed import or business goal.
The second Request must execute its actual goal with fresh evidence.

## Fixture and source invariants

Project root: E:\SmartAgent_PR_BCD_LIVE
Wheel: wheelhouse\smartagent_pr_bcd_live-1.0-py3-none-any.whl
Constraint: smartagent-pr-bcd-live>=1.0
Authorized future install target: E:\SmartAgent_PR_BCD_LIVE\deps
Authorized future metadata target: E:\SmartAgent_PR_BCD_LIVE\.agents\project_context\environment.json
Never download from network, modify shared SmartAgent Python, or write outside explicitly approved paths.
Actual running Worker SHA must match the tested commit, not merely the git checkout.
Use new Tasks for new Live; never resume a historical PAUSED Task to earn new credits.

## A. Foundation read-only Live (safe on this branch)

| ID | Live case | Required evidence and PASS criteria |
|---|---|---|
| L00 | Worker / source identity | installed Git SHA, source-load attestation, manifest hash PASS; actual worker matches test SHA |
| L01 | Request-first project root | trusted original request path matches project; read allowed; no inferred write grant |
| L02 | Runtime environment probe | package constraint, probe source, current state/version; honest MISSING/SATISFIED/UNKNOWN |
| L03 | Four B-P1 observations | list wheelhouse, inspect root/wheelhouse/deps, proven INSPECT Wheel, list deps; 4/4 SUCCEEDED, executed=true, evidence ACTIVE, admitted proof, PR-A SATISFIED |
| L04 | Explicit read-only / no-install intent | no MUTATE/VERIFY/Resume, no metadata/deps write, no authorization escalation |
| L05 | Existing project candidate (only if present) | candidate not trusted until project-local re-probe, actual state/constraint from Runtime |
| L06 | Reporting vs terminal | Action SATISFIED does not make full PR-C/D SUCCESS; terminal admission/Verification accurately bounded |
| L07 | Baseline collector completeness | separate sanitized RCA per Task with worker SHA, four Action proofs, ledger, freshness and terminal |

Historical B-P1 Task TASK-D89B9D1A5CE7CA86F6F6 is a baseline for L03 only.
It does not prove this new commit or authorized PR-C/D recovery.

### Pasteable L02/L04 read-only prompt

在 E:\SmartAgent_PR_BCD_LIVE 執行 PR-B Foundation Read-only Baseline。
僅判定既有環境是否滿足 smartagent-pr-bcd-live>=1.0。
本次禁止安裝、下載、任何寫入、MUTATE、VERIFY 或 Resume。
根目錄、wheelhouse、deps 僅允許 Runtime 唯讀 Probe/Observation。
請回報 PACKAGE fact、probe source、observed version、PR-A Action State、
observation_proof_admitted/reason、evidence_status/freshness、Action 執行狀態、
Runtime Verification、Terminal Receipt、actual Worker Git SHA。
如果需要補救或授權不成立，停在 UNKNOWN/BLOCKED，禁止使用其他命令繞過。
不得宣稱完整 PR-C/D 成功。

## B. Full six-stage Live (ONLY after B-P2/B-P3 scoped write adapter)

The current request write ceiling intentionally rejects unproven run_command MUTATE/VERIFY.
Do NOT attempt L08-L18 on Foundation only.

| ID | Live case | Required evidence and PASS criteria |
|---|---|---|
| L08 | First Request: positive offline installation and missing dependency | MISSING + explicit install intent + independent Scope/Write ACL and valid bounded request ceiling |
| L09 | Install local Wheel into deps | unique MUTATE, executed, SUCCEEDED, no network, no shared venv; REMEDIATED_PENDING_VERIFY |
| L10 | Independent VERIFY | distinct action, verifies_action_id links L09, actual import passes; VERIFIED |
| L11 | Resume blocked original operation | new Action, depends_on links L10, actual goal success; RESUMED |
| L12 | Publish project environment | independent metadata ACL, current package version independently re-probed; pr_d_current_environment_verified |
| L13 | Second Request: independent Task/Request/Epoch | read index, re-probe project candidate; PROJECT_ENVIRONMENT_REPROBED, constraint SATISFIED |
| L14 | Second Request: actual goal execution | import/goal succeeds with fresh evidence, accepted terminal; MUTATE count = 0 |
| L15 | No repeat installation | compare deps manifest and environment.json content hashes before/after second Request; unchanged |
| L16 | Isolated authorization and evidence | distinct Request/Task/Epoch/action/evidence IDs; prior permission never inherited |
| L17 | GAR B-P2 blocked/unknown cases | rejected unstarted differs from partial/unknown effects; no replay of ambiguous mutations |
| L18 | Full PR-B/C/D terminal | resumed goal + linked VERIFY + published index; no false complete from Action SATISFIED alone |

For L08, original request must explicitly authorize local deps installation and metadata persistence.
Runtime must separately prove writable ACL. Second Request explicitly forbids installation/mutation.

## C. Hostile/negative cases — OFFLINE regression only

| ID | Case | Required |
|---|---|---|
| N01 | Bundled version too old, project version satisfies | re-probe and use project version |
| N02 | Bundled version already satisfies | do not needlessly project-probe |
| N03 | Stale/missing/corrupt project record or downgraded version | no stale-cache SATISFIED |
| N04 | Index redirects candidate outside project | never probe external location |
| N05 | Request mentions metadata but writable ACL denies | no index write |
| N06 | Invalid ceiling, wrong metadata path, or missing metadata declaration | no index write |
| N07 | Explicit install plus ban on other project writes | preserve install intent; still no implicit grant |
| N08 | Explicit install prohibition | remediation_requested=false |
| N09 | Independent VERIFY fails or resume fails | no publication or false terminal PASS |
| N10 | Rejected/unstarted vs partially executed command | no stale evidence or blind retry |
| N11 | Next request demands incompatible version | no reuse as SATISFIED |
| N12 | Next request tries to inherit earlier write authority | independent authorization required |

Do NOT run destructive, out-of-scope or adversarial shell tests in the user Live project.

## RCA collection contract (for every Live Task)

- Baseline: running Worker Git SHA, branch, manifest validation, source-load attribution, original request, project root, authorized read paths, request Write Ceiling active/valid/reason/grants_authorization, actual allowed ACL.
- Per Action: request_id, task_id, task_epoch, action_id, tool/operation, canonical resolved paths, scope admission, executed, execution_status, result_status, exit_code (when applicable), Runtime Verification, evidence_ref/status/freshness, PR-A state and observation_proof_admitted/reason.
- PR-B/C/D: pr_b_probe_completed; package ecosystem/constraint/observed_version/source/status; remediation_requested/eligible; transitions MISSING/REMEDIATING/REMEDIATED_PENDING_VERIFY/VERIFIED/RESUMED; remediation_action, verification_action, resumed_action.
- Persistence: pr_d_current_environment_verified or project_environment_index_not_published reason; index record package/version/location/request/action/evidence; sanitized existence/size/SHA256 and deps file names/hashes before and after, without disclosing secrets.
- Terminal: accepted/rejected receipt, decision/outcome, Runtime Verification, completion_kind, full PR-B/C/D verification status.
- Bundle: collect.bat for the exact TASK and REQUEST; retain separate manifest.json, timeline.json, pr_a_requirement_ledger_summary.json, pr_bcd_recovery_summary.json, task_progress_summary.json, and source attestation if supported. A Telegram final message is not authoritative evidence.

Acceptance stages: Foundation CI PASS; Foundation read-only Live L00-L07 PASS; later B-P3 write-authorized full lifecycle L08-L18 PASS.
Do not award B-P2/B-P3 credit early or claim general C++/Android environment discovery from Python Wheel tests.
