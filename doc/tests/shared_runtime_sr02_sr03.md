# Shared Runtime SR-02 + SR-03 | WebGPT1 Live Gate

This is a shared Runtime readback / V9 format stability experiment, **not** GAR
L4 verification. WebGPT2 is unused, GAR and deliberate protocol fault injection
are OFF. Start from a clean, idle WebCopilot Controller; do not replay paused
requests. Do not change main, Update.bat, or installed browser profiles.

## 0. Offline regression

Run from the repository root:

    cmd.exe /c run_sr02_sr03_regression.bat

Expected: commands=8, passed=8, failed=0. The test runner prints the
last 80 lines of a failed case at the end of the console run. Local evidence:

    localdata/logs/shared-runtime-sr02-sr03/

These checks cover SR-02 and SR-03 fixture round-trip via the same V9 Parser,
the Narrative canonical path, exclusive single-fence transport, stale readback,
and an answer-only terminal test. Offline PASS is **not** Live PASS.

## 1. WebGPT1-only launcher

Close the previous Controller when idle. Then start:

    cmd.exe /c launch_sr02_sr03_webgpt1.bat

Expected effective settings:

    SMARTAGENT_GAR_V1=0
    SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=0
    SMARTAGENT_GAR_FRESH_CONTEXT_URL=(unset)
    TRI_ONE_TASK_RESULT_V1=1
    TRI_ONE_TEXT_ONLY_V1=1

Do not use launch_gar_d1_live_m365.bat for this gate.

## 2. SR-02 Live prompt (send as ONE request)

請只進行文字解說，不要執行檔案或系統操作。詳細說明 C++ RAII：
使用三個分開的 Markdown `FENCE3cpp` 程式碼區塊，依序示範
std::ofstream、std::unique_ptr 與 std::lock_guard，並解釋各範例的
destructor / lifetime / exception safety。每個範例後說明資源如何釋放。
最後單獨一行輸出 SR02_END。不要省略任何程式碼區塊。

## 3. SR-03 Live prompt (send separately after SR-02 is terminal)

請只提供文字及範例，不要執行檔案或系統操作。提供一段有效 JSON，
用一個 Markdown `FENCE3json` 程式碼區塊呈現。JSON 必須包含：
nested object、array、繁體中文、包含雙引號的字串、
Windows 路徑 `C:\camera\tests\ae.json`、包含 newline 和 tab 的
字串，以及一個字串欄位顯示 C++ Markdown 三反引號片段。
JSON 範例後用繁體中文解釋哪些字元需要跳脫。
最後單獨一行輸出 SR03_END。

## 4. Minimum Live acceptance

For each distinct request, confirm:
- The **Controller** captures the expected user request and reaches a terminal
  state; no other request was replayed.
- The reply is complete and preserves all requested code/JSON blocks (SR-02:
  3 cpp blocks; SR-03: parsable nested JSON with original escapes).
- Exactly the intended end marker is present and untruncated:
  SR02_END / SR03_END.
- V9/turn_commit is admitted according to the existing runtime evidence gate,
  not merely because the rendered app displays SUCCESS.
- No gar_d1_test_armed, gar_d1_test_fault_injected, or
  gar_fresh_planner_activated events appear for these requests.

Capture request ID, controller final status, and the rendered output; do not
provide private logs if the environment forbids it. Two screens with PASS
do not prove every DOM selector/freshness corner case; separate offline
negative tests remain necessary.

## 5. Limitations

Live prompts can **exercise** long C++/JSON Markdown, but cannot guarantee
a stale readback or ACK failure. Those are tested with deterministic offline
negative fixtures. Do not infer GAR D1 or L4 success from SR-02/SR-03.
