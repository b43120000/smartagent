# Git43 — Turbo Mode, Project Sync and Software Registry

Base: Git42 `1582ea9dee5ec9ba8df2066243f8bd504be070d3`
Branch: `git43/turbo-mode-environment-sync-20261109`

## Operator controls

**Turbo is OFF by default.** The user explicitly configures one or more strict descendants of an existing authorized writable workspace. The descendant may be a *future* directory; configuration never creates it. Reparse paths, denied paths and read-only overlap are rejected.

- Telegram: send exactly `turbomode=E:\\SmartAgent_GoalDriven_IT_GH_v1` to the authenticated bot. The receiver applies the setting locally, *before* submitting anything to WebGPT or creating an Agent task.
- WebGPT/WebCopilot: send the same standalone text as the user request, not as a Planner-generated tool call.
- Telegram or WebGPT: `turbomode=off` disables Turbo roots under the current Workspace.
- `Edit_workspace.bat` -> menu [1] Configure Writable/Read-only paths -> [1] Add writable, [2] Remove writable, [3] Add read-only, [4] Remove read-only, [5] Add turbo mode, [6] Remove turbo mode, [7] Apply, [8] Back.
- The global shared location is `localdata/secure/windows_security/workspace_access_policy.json` (same `SMARTAGENT_WORKSPACE_ACCESS_POLICY_V1` policy as writable/read-only roots), under `turbo_roots`. The Manager reloads newer policy revisions and refuses to overwrite a concurrently changed external policy while holding local draft edits.

Turbo is **not** an ACL, system privilege, permission to traverse a denied path, or permission to overwrite/delete. It bypasses only the extra UNKNOWN-command human confirmation for precisely recognized, in-scope, non-destructive *single* commands **after** Action Admission. A `TURBO_AUTO_ADMITTED` Blackboard event records the policy revision, root and action hash. Ordinary ToolResult/evidence remain authoritative.

The user can add and write files inside an already authorized writable Workspace through normal typed tools. Turbo is not necessary for those operations. Arbitrary Python scripts, `git reset --hard`, any compound PowerShell expression, nested invocations, reparse paths and unknown shell grammar remain gated. Missing the text `delete` is never enough for automatic approval.

An exact literal PowerShell `(Get-FileHash -LiteralPath 'PATH' -Algorithm SHA256).Hash -eq '64-HEX'` now satisfies the proven-read grammar; no human confirmation is needed even in Standard Mode. Side-effecting compound commands remain UNKNOWN or rejected.

## Project Sync for PokeClaw Executor

- `INDEX_ONLY` is retained in the Planner tool catalog when `browser_upload=false`. It creates a **local** index/snapshot and supports subsequent `query_project` calls with Runtime-owned references.
- `FULL_BUNDLE` and `DELTA` are only listed when browser upload is available. They still require verified transport binding/ACK; no fabricated transfer success.
- An Environment Inventory is now constructed once per Executor task, and included in the Planner decision context. Previous Goal/Prerequisite components remain disabled.

## Software Environment Registry

The Environment Inventory merges:
1. Runtime-probed bundled `.venv` Python/Git (CURRENT_AVAILABLE only after a real probe).
2. `<ProjectRoot>/.agents/project_context/environment.json` — verified project dependency and SDK/tool records, re-probe required.
3. `localdata/metadata/software_registry.json` — additional executable candidates, created only from successful, executed, evidence-backed absolute executable `--version` probes.

A successful installation command is **not enough** to add an available tool. Executor must use an admitted version probe that returns `status=SUCCEEDED`, `executed=true`, `process_state=EXITED`, `exit_code=0`, `timed_out=false`; the registry records path and actual Evidence Ref. On subsequent tasks registered executables remain `RECORDED_REPROBE_REQUIRED` and require a current scoped re-probe before use.

Registry presence is never authorization to launch software, reach the network, install dependencies or mutate the shared `.venv`.

## Test and Live acceptance

Git43 CI: targeted Turbo controls, security boundaries, shared policy, hash grammar, no-upload Project Sync, evidenced external tool registry, ACL OFF compatibility and Git33–42 regression on Windows and Ubuntu.

Live checks still required:
- Set Turbo path via Telegram, confirm Workspace Manager [1] lists the identical root and revision; then remove it in Manager, confirm Telegram control sees the new revision.
- Enable Turbo for a future Case D deployment child and verify in-scope allowed ToolCalls execute without repeated UNKNOWN prompts while out-of-scope/deletion commands remain gated.
- Run `project_sync INDEX_ONLY` under WebGPT text-only/no-browser-upload.
- Perform one real executable version probe and verify Global Software Registry updated with matching `MIN-EV-...`.
- Run Case D end-to-end; independent CSV SHA256 and Finish Comparator determine PASS/FAIL.

No claim of Live testing is made from CI alone.
