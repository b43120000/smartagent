# Python / Git portable package and workspace URL plan

Date: 2026-10-22 Asia/Taipei. Status: implemented; fresh offline UT/IT and Runtime dependency-loop IT-R pass. Live composer readiness passes ChatGPT/Gemini/Claude; M365 redirects to Microsoft sign-in and remains BLOCKED_LOGIN. Remote publication and clean remote-download validation follow final tree checks.

## Acceptance and execution order

1. Preserve both existing dirty working trees. Runtime development root is `C:\Users\user\Desktop\remoteagent\release\SmartAgentv2_1`; publication root is `E:\card\release\SmartAgentv2`. Record starting Git revisions and changed files before migration. Do not use update1/sync_back, reset, force push, or overwrite machine credentials.
2. Bundle Git alongside the existing Python portable payload, with version/source/hash/license metadata and a relocatable resolver. Seed the environment ledger from actual validated binaries. Planner must discover local tools first and plan installation of missing libraries/SDKs before retrying the original operation; do not invent a successful installation.
3. Fix RemoteAgent/Telegram WebGPT workspace binding to share the existing four-provider and origin-isolated Other URL validation used by Edit_workspace. Trace save -> registry -> task routing -> adapter; retain URL query/path and origin identity. Reject invalid schemes/credential URLs.
4. Replace old code-copy workflows with bundled-Git commit/fetch/cherry-pick, preserving machine state. Only five public root operation files: ACLstatus.bat, Edit_workspace.bat, force_stop_all_agents.bat, launch_remote_agent.bat, launch_webcopilot.bat. Keep source, installer, docs, default DOM and immutable runtime payload in subdirectories; exclude logs, credentials, browser state, caches, temporary test projects and local calibration overrides from version control. Test/support code belongs under subdirectories.
5. Validate runtime package first, then migrate reviewed application changes to publication root using Git. Validate clean export in a fresh path. Do not publish remotely until checks complete.

## Verification matrix

| Area | UT | IT | Web IT |
|---|---|---|---|
| Portable Python/Git | resolver precedence, missing/corrupt payload, relocatable paths, ledger provenance | fresh extraction without PATH Python/Git; imports; git init/commit/fetch/cherry-pick in temporary repos; second launch idempotence | planner request reports both tools and executes a harmless dependency discovery |
| Missing SDK/lib | discovery success, absent dependency produces prerequisite plan, failed install remains failure | local executable fallback; isolated missing package installation/retry; denied/network failure retains diagnostic | issue a runtime task requiring discovery and verify request/task/evidence and user-visible result |
| URLs | ChatGPT/Gemini/Claude/M365/Other; malformed/credentials rejected; Other origin separation | remote binding save/load + registry + Telegram task execution URL; restart persistence | workspace manager/Telegram choose each provider and Other; verify correct adapter/URL and runtime result; manual calibration for unconfigured Other |
| Release | root allowlist; tracked forbidden state; renamed launcher references | clean checkout/extract at path with spaces, all five launcher self-tests; portable Git transfer, dirty destination/conflict handling | launch clean package, open saved provider and verify DOM readiness |

Web IT requires actual browser/runtime evidence. Mocked adapters are IT only. A blocked live check must be recorded as BLOCKED, never PASS.

## Quota and handoff

Query five-hour usage at milestone boundaries. At <=5% remaining: stop starting new milestones, finish/revert incomplete atomic edits, run syntax/launcher checks, preserve runnable binaries/config, write `doc/portable_git_handoff.md` with completed changes, exact tests, failures, pending work and current Git state. Read the latest reset timestamp and schedule a one-time continuation on this chat after reset. Continuation rechecks quota and state before executing; no duplicate side effects or unrelated agent termination. Do not schedule a continuation if all work completes before the threshold.

Initial quota: 99% remaining. Initial publication HEAD: 4b9ad7e; runtime HEAD: db9ed4a. Both trees already contain user changes.
