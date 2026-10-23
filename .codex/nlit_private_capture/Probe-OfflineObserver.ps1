# SA-NLIT-20261024: mandatory separate-process real-import hook proof.
# No browser, supervisor, Planner, Runtime task, CDP or writer started.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$TestInstall,
 [Parameter(Mandatory=$true)][string]$ObserverDir,
 [Parameter(Mandatory=$true)][string]$PrivateRoot,
 [Parameter(Mandatory=$true)][string]$TestAppData
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
function MustDir([string]$P) {
 if (![IO.Path]::IsPathRooted($P) -or !(Test-Path -LiteralPath $P -PathType Container)) {
  throw 'NLIT_HOOK_PROBE_BLOCKED:INVALID_DIRECTORY'
 }
 return (Resolve-Path -LiteralPath $P).ProviderPath
}
$test=MustDir $TestInstall
$obs=MustDir $ObserverDir
$private=MustDir $PrivateRoot
$appdata=MustDir $TestAppData
$python=Join-Path $test '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw 'NLIT_HOOK_PROBE_BLOCKED:PYTHON_MISSING' }
$envBefore=@{}
$names=@('APPDATA','LOCALAPPDATA','SMARTAGENT_REMOTE_WORKER','SMARTAGENT_NLIT_CAPTURE_ENABLED',
 'SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED','SMARTAGENT_NLIT_CAPTURE_ROOT',
 'SMARTAGENT_NLIT_OBSERVER_ROOT','PYTHONPATH')
foreach ($key in $names) { $envBefore[$key]=[Environment]::GetEnvironmentVariable($key,'Process') }
$env:APPDATA=$appdata
$env:LOCALAPPDATA=$appdata
$env:SMARTAGENT_REMOTE_WORKER='1'
$env:SMARTAGENT_NLIT_CAPTURE_ENABLED='1'
$env:SMARTAGENT_NLIT_PRIVATE_ACL_CONFIRMED='YES'
$env:SMARTAGENT_NLIT_CAPTURE_ROOT=$private
$env:SMARTAGENT_NLIT_OBSERVER_ROOT=$obs
$env:PYTHONPATH='IGNORED_IN_EMBEDDED_ISOLATED_MODE'
$pythonProbe=@'
import json,os,sys
from pathlib import Path
import sitecustomize
from WebAgent.protocol_loop import WebAgentProtocolLoop
from WebAgent.browser_client import WebAgentBrowserClient
from agent_core.web_runtime import WebLLMScraper
import private_capture
from inspect import signature
from agent_core.protocol_manifest import validate_protocol_manifest
real_observer = Path(os.environ["SMARTAGENT_NLIT_OBSERVER_ROOT"]).resolve()
loaded = Path(sitecustomize.__file__).resolve().parent
assert loaded == real_observer, "WRONG_SITECUSTOMIZE_ORIGIN"
assert bool(sys.flags.isolated), "NOT_ISOLATED_PYTHON"
assert bool(sys.flags.ignore_environment), "ENVIRONMENT_IMPORT_RELIANCE"
assert all(hasattr(x,"__wrapped__") for x in (
    WebAgentProtocolLoop._run_impl,
    WebAgentBrowserClient.ask,
    WebLLMScraper._write_prompt_to_composer,
    WebLLMScraper._log_stage,
)), "REAL_HOOKS_NOT_WRAPPED"
assert sorted(k[0] for k in private_capture._PATCHED) == [
    "browser_client","protocol","scraper"
], "HOOK_FAMILY_MISMATCH"
assert all(k in str(signature(WebAgentProtocolLoop._run_impl))
    for k in ("request_id","task_id","task_epoch")), "LOOP_SIGNATURE_MISMATCH"
assert "protocol_expected" in str(signature(WebAgentBrowserClient.ask)), "BROWSER_SIGNATURE"
assert "protocol_expected" in str(signature(WebLLMScraper.ask)), "SCRAPER_SIGNATURE"
verdict=validate_protocol_manifest(".")
assert verdict["hash_verification"] == "PASS", "PRODUCT_MANIFEST_MISMATCH"
print(json.dumps(dict(
    status="PASS",
    loaded_sitecustomize_from_external_observer=True,
    isolated_python=True,
    ignored_pythonpath=True,
    real_classes_wrapped=4,
    hook_families=3,
    product_manifest_pass=True,
    browser_launched=False,
    task_submitted=False),sort_keys=True))
'@
try {
 Push-Location -LiteralPath $test
 try {
  & $python -c $pythonProbe
  if ($LASTEXITCODE -ne 0) { throw 'NLIT_HOOK_PROBE_BLOCKED:PYTHON_IMPORT_OR_ASSERT_FAILED' }
 } finally { Pop-Location }
} finally {
 foreach ($key in $names) {
  [Environment]::SetEnvironmentVariable($key,$envBefore[$key],'Process')
 }
}
Write-Output 'NLIT_REAL_WORKER_HOOK_IMPORT_PASS'
