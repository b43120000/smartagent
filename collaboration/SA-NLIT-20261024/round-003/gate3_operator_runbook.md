# SA-NLIT-20261024 — Gate3 Windows deployment / human-login stop

**Source owner:** ChatGPT. **Local executor/evidence custodian:** Codex. **No live execution claimed here.**
Branch: `fix/nlit-evidence-20261024-r1`. Product/test source stays detached at
`309305b2eeeb79aed0925dc4dfee45af5193f8b5`, tracked clean. Do not merge/main.

## Product APIs reviewed at exact product source SHA

- `agent_core.browser_preferences.save_browser_preferences('edge', 5)`: one TEST-install-only
  `config/browser_session.json`; default Edge5 port 12815. Already set through official API.
- `agent_core.paths.browser_profile_root()` is `APPDATA/WebLLMScraper`, **not** install-relative.
  The Gate3 script supplies TEST_APPDATA before interpreter startup.
- `agent_core.web_ui.manual_browser.ensure_agent_browser(playwright, headless=False)`
  launches managed profile and loopback CDP. Gate3 independently verifies the listening PID's
  `msedge.exe` command line includes the intended TEST profile and port before reuse.
- `RemoteAgent.remote_workspace.cmd_set` uses `remote_binding.save` and
  `ConversationRegistry.upsert_binding/configure_remote_conversation`, all under TEST install.
  `agent_core.workspace_access.save_workspace_access_policy` writes TEST-only authorization;
  OS attestation, when required, is not bypassed.
- `host_supervisor --remote-only` calls `remote_binding.activate`; its snapshot takes precedence
  over historical registry/ambient workspace settings.
- `RemoteAgent.local_telegram_sender.send_and_wait(request,workspace,root)` constructs
  `TelegramReceiver -> AgentIngressGateway -> RemoteTaskQueue` at explicit TEST root,
  and waits for TEST supervisor/worker's real result/outbox.
- `RemoteAgent.remote_worker_launcher._spawn` marks workers `SMARTAGENT_REMOTE_WORKER=1`;
  Gate2-proven `sitecustomize` wraps four real methods in the fresh isolated interpreter.

**Important correction to prior helper:** `Invoke-NlitIsolated.ps1 -Mode StartSupervisor`
always invokes the original `preflight.ps1 -Phase BeforeStart`. Once Edge5 has started,
BeforeStart correctly rejects its occupied port. Gate3 therefore calls the official
`launch_remote_agent.bat` **directly** after an independently checked
`preflight.ps1 -Phase AfterStart`; do not run the old Invoke helper for Gate3 supervisor/case.
This does **not** bypass or loosen any original preflight guard.

## Artifacts: stage through GitHub connector, not local git fetch

Put these **exact bytes** in the already ACL-restricted external observer directory, beside
the original five unchanged observer files and retained v1/v2 adapter evidence.
Use `.codex/nlit_private_capture/gate3_manifest.json` to verify the new Python and PowerShell
files: full SHA-256, exact length, Git blob SHA and source commit. Also stage the latest separate
`adapter_manifest.json` (v2); never replace `artifact_manifest.json` (the original five-file manifest).
Do not stage product source changes into the detached test checkout.

## Local PowerShell operator commands

Codex already has the real private installation/workspace roots. Assign actual values in the
restricted local shell only; these names are the complete interface, not source-code edits:

```powershell
$nlit = @{
  TestInstall       = '<DETACHED_309305B2_TEST_INSTALL>'
  ProductionInstall = '<RUNNING_ORIGINAL_INSTALL>'
  TestWorkspace     = '<FRESH_DEDICATED_TEST_WORKSPACE>'
  ProductionWorkspace = '<RUNNING_ORIGINAL_WORKSPACE>'
  ObserverDir       = '<EXTERNAL_OBSERVER_DIRECTORY>'
  PrivateRoot       = '<ACL_RESTRICTED_PRIVATE_ROOT>'
  TestAppData       = '<DEDICATED_TEST_APPDATA_DIRECTORY>'
  ProductionAppData = '<ORIGINAL_WINDOWS_APPDATA>'
  GitExe            = '<ACTUAL_LOCAL_GIT_EXE>'
}
# If TEST_APPDATA does not exist yet, create it only under an explicitly
# authorized, disjoint TEST-private location. Do not use the live APPDATA.
# Verify private-directory ACL locally as before.
$Gate3 = Join-Path $nlit.ObserverDir 'Invoke-NlitGate3.ps1'
& $Gate3 @nlit -Mode Inspect
if (!$?) { throw 'NLIT_GATE3_INSPECT_FAILED' }
```

`Inspect` repeats exact v2 adapter Verify, checks reparse/disjoint roots, then the unchanged
original **five-file** product/observer preflight. It selects BeforeStart when Edge5 is absent,
AfterStart if an owned Edge5 listener exists. It does not start a browser, worker or task.

### Browser gate: permissible while human is unavailable

```powershell
& $Gate3 @nlit -Mode OpenBrowser
if (!$?) { throw 'NLIT_GATE3_BROWSER_START_FAILED' }
```

This calls official TEST `manual_browser.ensure_agent_browser`, then opens
`https://chatgpt.com/` in the dedicated TEST Edge5 profile with a new visible tab. It never
uses the live SmartAgent Chrome, collaboration browser, or a pre-existing foreign CDP port.
If 12815 is occupied by any **nonmatching** process/profile, it blocks before attach.

**Expected result without user present:** `NLIT_GATE3_TEST_EDGE5_OPEN; LOGIN_NOT_ATTESTED`.
The user must manually sign into this newly created TEST browser/profile and start a **fresh**
test-only ChatGPT conversation. Do not copy cookies, credentials or another tab's existing
conversation link. A listening CDP port alone is not login evidence. **Stop the run here** until
the user has authenticated and the new test page is independently confirmed. The detached
Edge5 can be kept open; no test supervisor/task should start yet.

### Binding gate: ONLY after human login and new conversation URL

```powershell
& $Gate3 @nlit -Mode Bind
# This prompts interactively for the fresh TEST https://chatgpt.com/c/... URL.
# The URL is never included in a GitHub record or helper source.
& $Gate3 @nlit -Mode CheckBinding
if (!$?) { throw 'NLIT_GATE3_TEST_WORKSPACE_BINDING_FAILED' }
```

`Bind` uses the real product `remote_workspace.cmd_set` and creates only a TEST-local
`workspace_access_policy.json`, denying the original production workspace.
The Python guard checks `remote_primary_binding.json`, enabled conversation registry,
writable workspace permission and runtime security preflight. It refuses to overwrite
any existing **non-TEST** binding, reuse the live URL, or work with a TEST installation
containing Telegram credentials/pairing state. Do not edit these JSON files manually.
If the TEST OS security profile does not attest TEST_WORKSPACE, **BLOCKED**; do not
disable security checks or substitute the original authorized workspace.

### Supervisor gate: separate dedicated foreground console, after login confirmation

```powershell
# In dedicated foreground PowerShell console A, reconstruct $nlit from local-only saved
# role values. ManualLoginVerified MUST reflect actual human authentication.
& $Gate3 @nlit -Mode StartSupervisor -ManualLoginVerified
```

The script uses the original `launch_remote_agent.bat` (official launcher).
A fresh nonce is saved **only** in `PrivateRoot/gate3_capture_nonce.txt` with the
previously verified private ACL; none is printed or put on the Python command line.
It sets TEST-only APPDATA/LOCALAPPDATA/TEMP and capture opt-in before launching.
The launcher is foreground and blocking; keep console A alive. Do not invoke this
mode if nonce/state already exists, or if the TEST supervisor is running.
It disables Telegram network receiver/pairing. It does **not** create a parallel queue.

Check the actual TEST `remote_supervisor_state.json` status=RUNNING, PID and current
heartbeat, TEST binding snapshot, the new TEST browser's command line/profile and absence
of production overlap. A WAITING_SIGNAL line alone is not proof of the entire chain.

### ONE new read-only case: separate console B, only when all gates are proven

Place a fresh user-authorized **read-only** request in a UTF-8 file under PrivateRoot;
keep request body, local paths, prompt, response and nonce private. Do not use
historical IT07–IT23 identifiers or resubmit ambiguous IT22/IT23B drafts.

```powershell
& $Gate3 @nlit -Mode SendCase -ManualLoginVerified -NewCaseId 'NLIT-NEW-R3-CAPTURE-001' -CaseFile '<PRIVATE_ROOT>\new_capture_case.txt' -TimeoutSec 900
```

Gate3 verifies owned Edge5, the TEST-bound URL/workspace/authorization, original five-file
preflight (AfterStart), test supervisor state/heartbeat and private casefile before calling
`send_and_wait` with explicit TEST root/workspace. The nonce and request body are loaded
inside TEST Python directly from private files, **not** supplied as process command-line text.
The official test sender submits one LOCAL_TEST Telegram-shaped message through the real
Receiver/Gateway/queue/worker and writes results to TEST-local outbox. Nonzero sender exit
means inspect and preserve private evidence, **not** automatic semantic failure/PASS.

Correlate actual composer input, submit click, request-bound user turn, assistant result and
Runtime terminal receipt; historical 17/17 missing Planner payloads remain unchanged.
IT19 stays BLOCKED until test transaction store/writer ownership is independently verified.
No draft cleanup, no blind retries. Never widen SUCCESS acceptance to accommodate missing evidence.

## Local validation and restore

After browser-stage source deployment, record separate statuses for:
source SHA/bytes, original five-file preflight, Gate2 V2 Verify, Edge5 PID/port/profile,
TEST authorization/binding, manual login, supervisor PID/heartbeat, sender ID and correlation.
**No source artifact or command text constitutes an executed Gate3 PASS.**

When dedicated test work is finished, stop and confirm every TEST supervisor/worker/interpreter,
leave live processes running and restore isolated `python313._pth` only via the already pinned
`Enable-NlitIsolatedPython-v2.ps1 -Mode Restore -TestProcessesStopped`. Compare original
SHA-256 `c9b688d4166d3412f4e46fe3ba24c6b56e5a14443699a627168faf82bf125657`;
leave private backup/state for audit. Do not run `Install-OfflineObserverPath.ps1`,
or mix the v1 backup with v2 state.