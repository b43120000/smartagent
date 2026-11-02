# PR-B/C/D Live B — B-P1 Runtime Observation Evidence

Implementation branch: `feat/live-b-p1-observation-evidence-20261103`.
Base: `2f60e622de89cc9d8348e44c56da19284bd38543`.
Do not merge `main`; do not install or mutate the user fixture during B-P1.

## Problem demonstrated by RCA

- Previous B-P0: native `list_directory` and `inspect_directory` returned
  `SUCCEEDED` but every PR-A Action stayed `UNKNOWN` because
  `effective_verification_status` was not `PASS`.
- Previous full Live B: a later `run_command INSPECT` with unproven command
  effects became FAILED and staled earlier read evidence. This must not happen
  to a **definitively unstarted/rejected** process, yet actual ambiguous
  effects must still invalidate affected observations.
- A successful read operation is NOT equivalent to a verified remediation or
  user task. The verifier's condition remains `UNKNOWN` or `UNVERIFIED`.

## B-P1 implementation

- Runtime-owned `observation_receipt`: native `list_directory` and
  `inspect_directory` are admitted only when the authoritative tool result,
  execution flags, resolved paths, payload, and observations match. A proven
  read-only `run_command INSPECT` additionally requires Runtime's
  `observation_confirmed=true`, exit code 0 and completed process.
- A successful receipt makes **its PR-A Action** `SATISFIED`, with
  `observation_proof_admitted=true`, while leaving the independent task
  condition unverified. No response text or untrusted operation label can
  fabricate postcondition PASS.
- Proven INSPECT may use **read-authorized** project CWD only when both
  `working_directory` and literal read target are in the authorized read
  boundary. Unproven INSPECT and any `VERIFY`/MUTATE retain the stricter
  write-CWD rule. Backward-compatible relative literals are resolved against
  their already-authorized CWD before being checked.
- Definite non-execution (`REJECTED`, `executed=false`) cannot stale past
  reads. Ambiguous execution (`UNKNOWN` or `PARTIAL`) remains conservatively
  stale, even if process-start status is unknown. Positively proven read-only
  commands never mutate resource evidence.
- B-P1 does not add PR-C installer permission, PR-D verification, persistence,
  or override GAR pause logic.

## Windows offline regression

```powershell
$env:PYTHONPATH="source"
& .\.venv\Scripts\python.exe -B source/WebAgent/tests/validate_pr_bcd_live_b_p1_evidence_it.py
& .\.venv\Scripts\python.exe -B source/WebAgent/tests/validate_pr_bcd_live_b_p0_scope_it.py
& .\.venv\Scripts\python.exe -B source/WebAgent/tests/validate_task_continuation.py
```

The P1 test covers native receipt, partial/errors, spoofed identities, shell
scope read vs mutation, foreign literal path, process exit proof, dependency
satisfaction, and evidence freshness. Existing continuation checks must also
remain green. The CI workflow runs the broader PR-A/B/C/D, Evidence, GAR and
Hybrid regression suites.

## B-P1 Live Telegram Prompt (read-only only)

```text
在 E:\SmartAgent_PR_BCD_LIVE 進行 PR-B/C/D Live B-P1 Evidence 唯讀驗證。

本輪只驗證 Runtime Observation、PR-A Action Evidence 與 Freshness。
禁止任何套件安裝、MUTATE、寫入、網路下載、PR-C/D、VERIFY、Resume，
不得修改 SmartAgent 共用 Python 環境。

原始依賴：smartagent-pr-bcd-live>=1.0
離線 Wheel：wheelhouse\smartagent_pr_bcd_live-1.0-py3-none-any.whl

允許套件安裝目標：E:\SmartAgent_PR_BCD_LIVE\deps
另允許必要的 Project Environment metadata 寫入：
E:\SmartAgent_PR_BCD_LIVE\.agents\project_context\environment.json
上述兩個寫入路徑只是原始授權上限，本輪禁止實際寫入。
禁止其他專案路徑寫入。

請依序執行以下 Runtime Action，每個 Action 使用唯一 action_id：

1. canonical list_directory：
   path: E:\SmartAgent_PR_BCD_LIVE\wheelhouse
2. canonical inspect_directory：
   paths:
   - E:\SmartAgent_PR_BCD_LIVE
   - E:\SmartAgent_PR_BCD_LIVE\wheelhouse
   - E:\SmartAgent_PR_BCD_LIVE\deps
   recursive: false
3. run_command：
   operation: INSPECT
   working_directory: E:\SmartAgent_PR_BCD_LIVE
   command: Get-Item -LiteralPath 'E:\SmartAgent_PR_BCD_LIVE\wheelhouse\smartagent_pr_bcd_live-1.0-py3-none-any.whl'
   verify: []
4. canonical list_directory：
   path: E:\SmartAgent_PR_BCD_LIVE\deps

本次不得執行未列出的命令，尤其不得以 INSPECT 標籤包裝
python、pip、echo redirection、Set-Content 或任何可能修改檔案的操作。

依各 Action 真實 Runtime Evidence 回報 status、executed、
PR-A Action State、observation_proof_admitted、observation_proof_reason、
與 evidence_status。對 SUCCEEDED 的唯讀 Action，應能提供
SATISFIED 的實際 Action Evidence；Task Verification 可保持 UNKNOWN，
不得為取得 PASS 而執行額外寫入或重新定義成功條件。

若 INSPECT 未准許或遭拒，請停止並回報真實拒絕原因，不得改用
其他命令繞過。完整 PR-B/C/D 一律 NOT VERIFIED。
```

Live acceptance: native actions + proven INSPECT are executed, identity
bound and admitted without false condition PASS. A separate unverified
mutation may not stale earlier read evidence; this negative case is tested
offline only — do NOT deliberately run untrusted commands in Live.
