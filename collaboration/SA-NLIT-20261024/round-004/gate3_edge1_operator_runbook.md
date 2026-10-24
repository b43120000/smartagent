# SA-NLIT-20261024 — round004 Edge1 source/deployment correction

Status: **GITHUB_SOURCE_ONLY; NOT A LOCAL IT PASS**. Source owner ChatGPT; local operator Codex.
Continue existing `fix/nlit-evidence-20261024-r1` branch. Do not merge or change `main` or either product checkout.
Product baseline remains `309305b2eeeb79aed0925dc4dfee45af5193f8b5`.

## Why round004 supersedes Gate3 browser assumptions

User explicitly requires the **first product Edge instance**. Official product
`save_browser_preferences('edge', 1)` selects **Edge1** with CDP
`127.0.0.1:12811`, managed TEST profile `%TEST_APPDATA%/WebLLMScraper/edge_1`.
The collaboration CDP `12941` is separate and NOT a selectable product instance.

Historical round003 result at commit
`c481bb39fa55c60200d4f14a8e1dfef19fe5de3e` remains evidence:
Gate2 V2/StrictMode/auto-hook PASS; Gate3 Inspect PASS; the **prior Edge5**
test-only browser owned/launch PASS; manual login BLOCKED; TEST binding
NOT_RUN; supervisor NOT_STARTED; new case NOT_SUBMITTED; exact post-tool
delivery NOT_OBSERVED. These observations do not transfer into an Edge1 PASS.
Retain the old TEST Edge5 process, its TEST_APPDATA profile, login state,
tabs/drafts and related private backup: **do not close or mutate it**.
Do not rebind its conversations, copy cookies, accept first-run prompts,
automate login or submit any new task without explicit human instruction.

## Exact round004 sources and pin (new, not original five-file manifest)

Place the two changed source files into the **external observer directory only**.
The following are exact UTF-8 without BOM byte counts and SHA-256 hashes,
separately pinned in `.codex/nlit_private_capture/gate3_manifest.json`
(`SA_NLIT_GATE3_ARTIFACT_PIN_V2`):

| File | Source commit | Git blob SHA | Bytes | SHA-256 |
|---|---|---|---:|---|
| `nlit_gate3.py` | `4925f59839dd059fbc70a63548bca1bca057bc3b` | `60381680cc4da530202083b2c67bc0059bc8d120` | 11828 | `bc52f2c61374aab490e4cf0b3115cb4b29715802b2bf735597ed5d21f7f01b4c` |
| `Invoke-NlitGate3.ps1` | `36d24d6e4fef4b7c4a8e1d9f87a22b704647e2ef` | `b3ebf05c42afa8d9d50b5c14abe427c87511bddd` | 11872 | `189a196c7ef2dddb64dc38a1e39215b50b177123ea277bd4d0278afa7dee9a0b` |

The PS helper **fails closed** unless the staged V2 Gate3 manifest contains
both exact pinned file sizes and hashes and browser `edge/1/12811/edge_1`.
Do not reuse the old round003 pin: it predated the source fixes and does
not match either helper. Preserve original five `artifact_manifest.json` and
all five collector files exactly; preserve separate `adapter_manifest.json`
V2 as previously verified.

### Minimal non-executing local hash / browser check

Use the existing restricted local `$nlit` parameter map from the round003
runbook; **do not put its real paths in GitHub**. Before checks, Codex backs
up the TEST-only `config/browser_session.json` and uses **official**
`save_browser_preferences('edge', 1)` within the isolated TEST install/
TEST_APPDATA; production settings must not change. Re-Enable of the test
Python adapter is NOT required; prior V2 Verify was PASS.

```powershell
$expected = @(
 @{ Name = 'nlit_gate3.py'; Bytes = 11828; Sha256 = 'bc52f2c61374aab490e4cf0b3115cb4b29715802b2bf735597ed5d21f7f01b4c' },
 @{ Name = 'Invoke-NlitGate3.ps1'; Bytes = 11872; Sha256 = '189a196c7ef2dddb64dc38a1e39215b50b177123ea277bd4d0278afa7dee9a0b' }
)
foreach ($x in $expected) {
 $p = Join-Path $nlit.ObserverDir $x.Name
 if ((Get-Item -LiteralPath $p).Length -ne $x.Bytes -or
   (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() -ne $x.Sha256) {
  throw "ROUND004_GATE3_HASH_OR_BYTES_MISMATCH: $($x.Name)"
 }
}
$pin = Get-Content (Join-Path $nlit.ObserverDir 'gate3_manifest.json') -Raw | ConvertFrom-Json
if ($pin.schema -ne 'SA_NLIT_GATE3_ARTIFACT_PIN_V2' -or
  $pin.browser.instance -ne 1 -or $pin.browser.port -ne 12811 -or
  $pin.browser.profile -ne 'edge_1') { throw 'ROUND004_GATE3_PIN_MISMATCH' }
# Read only; no connection/attach. Edge1 may still be absent.
Get-NetTCPConnection -State Listen -LocalPort 12811 -ErrorAction SilentlyContinue |
 Select-Object LocalAddress,LocalPort,OwningProcess
# Only after TEST preference saved and staged artifacts pinned:
$Gate3 = Join-Path $nlit.ObserverDir 'Invoke-NlitGate3.ps1'
& $Gate3 @nlit -Mode Inspect
```

An absent listener on 12811 is expected before Edge1 is launched. If a
listener exists, the Gate3 `EdgeOwned` guard requires **matching**
`msedge.exe`, exact TEST `edge_1` profile path, `--remote-debugging-port=12811`
and loopback-only address. Any mismatch blocks; never attach to or kill
unknown listeners. The prior TEST Edge5/12815 may remain active and is
**not** considered evidence that Edge1 is started. The separate collaboration
port 12941 must remain untouched.

## Holding condition / later gates

**Current hard stop: HUMAN_LOGIN_REQUIRED; NO_NEW_TASK_IN_FLIGHT.**
Do not invoke `OpenBrowser`, `Bind`, `CheckBinding`, `StartSupervisor`
or `SendCase` automatically. When explicitly authorized, `OpenBrowser`
uses official TEST `manual_browser.ensure_agent_browser` only after its
port/profile ownership checks, then human must manually set up/login the
Edge1 profile and supply a **new TEST-only** authenticated ChatGPT
conversation URL. Never recycle an Edge5 URL or a production conversation.

The subsequent Gate3 flow remains exactly as documented in round003,
**substituting Edge1 for Edge5** after manual login and fresh URL:
`Bind -> CheckBinding -> StartSupervisor -> one authorized SendCase`.
Original read-only preflight dynamically gets the selected TEST port from
`browser_session.json`. After Edge1 launch, the gate checks AfterStart
without changing the original five-file preflight. Preserve original
IT07-IT23 history and IT19 BLOCKED; no old request replay, no blind retry,
no draft cleanup and no relaxed SUCCESS acceptance. New runtime acceptance
is NOT_OBSERVED until a future, independently authorized real case yields
request-bound Planner input and terminal receipt.

## Source validation vs runtime status

GitHub readback and pin agreement establish **source integrity only**,
not PowerShell parse/runtime or Windows/CDP behavioral acceptance. Codex must
record a **new** round004 local gate report without rewriting round003
observations. In particular, never turn `Edge1 not yet run` into PASS.
