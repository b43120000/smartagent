# Git69 Execution Fidelity — Live Acceptance

Version: git69/execution-fidelity-multishell

## Preconditions

Use normal Planner → Runtime. Set ACL OFF, Turbo OFF, Auto Approve ON and report actual Effective values. Create a NEW unique directory under E:\\AIspace named git69_fidelity_live_<unique>. Do not delete, overwrite or modify any existing user files. All mutations must stay inside this fresh fixture. Never use Telegram Host CMD, unsafe Executor fallback, network Git mutations, arbitrary downloads, tool installation, or ACL/Turbo changes.

Under ACL ON + Turbo ON, DIRECT and CMD without Native Broker support must fail closed, not bypass to a host executor.

## Single long natural-language Live test

在 E:\\AIspace 內新建唯一的 git69_fidelity_live 子目錄，使用正常 Planner/Runtime 驗證 Execution Fidelity Matrix。每個 run_command 明確指定 shell_mode。只操作新建 Fixture，禁止清除任何既有目錄、禁止遠端 Git 操作與 Host CMD fallback。

請依序：
1. POWERSHELL: Write-Output 成功，記錄 exit 0 和 stdout。
2. POWERSHELL: throw 失敗，以及非終止 Write-Error 的 stderr/exit 語意。
3. CMD: echo stdout、stderr 重導向、exit /b 7，保留 exit 7。
4. CMD: Fixture 內建立全新測試 BAT，call 該 BAT 並驗證最終 exit /b 37。
5. DIRECT: 使用授權 Git exe 絕對路徑和 argv=[--version]，驗證 exit 0。
6. DIRECT: Fixture 內新建空 Git repo，執行 rev-parse HEAD，驗證預期 exit 128 和 stderr；不得 clone/fetch/push。
7. DIRECT: 安全本機程式分別輸出 stdout/stderr；檢查 bytes 與 raw SHA256。
8. exit 0 但 verify 缺少產物，驗證 execution=SUCCEEDED、verification=FAIL。
9. 不存在 exe 啟動失敗，驗證 exit_code=null、executed=false、failure_origin=PROCESS_START。
10. 大量輸出截斷，驗證 output_complete=false、capture_integrity=TRUNCATED、本地 ref/bytes/hash（如 backend 支援）。
11. 受控 timeout，驗證 UNKNOWN、possible_partial_execution、無虛構退出碼，不重播副作用。
12. 危險命令只能送 Admission，確認 REJECTED、executed=false，絕對不得真正執行。

每筆應有 Action ID、shell_mode、launch_form、command/argv integrity、backend、CWD、PID、process_state、exit_code、stdout/stderr byte count、raw hash、truncated、capture_integrity、execution/verification 狀態、failure_origin、MIN-EV。無法驗證就回報 BLOCKED/UNKNOWN，不得宣告 PASS。最後用 Finish V2 與真實 MIN-EV 比對所有 goal checks。

## Collector

Live 完成後於 C:\\smartagent 執行：

    collect.bat TASK-實際任務ID RR-實際請求ID

在 GAR ZIP 核對 executor_execution_fidelity.json、executor_run_commands.json、executor_command_capture_summary.json、native_execution_trace.json 與 Evidence Ref。分享的 ZIP 會遮蔽敏感文字；完整原始位元組留在本機私有 Runtime Evidence Store 或 capture_ref，避免公開洩漏。

Important: CI/Sidecar PASS is not a substitute for real Live E2E.
