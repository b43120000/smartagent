# Shared Runtime SR First-Readback-Only — WebGPT1 SR-02 / SR-03

## Why this build

This branch extends 75febba681949d2541f51f2423420dc2b2a35867.
GAR and deliberate V9 fault injection are OFF. The first stable, request-owned
assistant response observed after UI_CONFIRMED_IDLE is captured. Runtime does
**not** send format-repair prompts, perform Narrative Bridge reconstruction,
enter WebAgent Bounded Repair, execute any Tool Action, or send a final ACK.
SR_FIRST_READBACK_CAPTURED is an expected, safe diagnostic stop, NOT SUCCESS.

Shared WebGPT DOM, Parser and serializer are unchanged by the gate when the
flag is OFF. L4 is outside the scope of this experiment.

## 1. Preconditions

- Switch to the exact candidate SHA stated in the delivery.
- Stop previous Controllers when idle; never force-restart in-flight tools.
- Use the installed SmartAgent virtual environment in the same checkout:
  .venv\Scripts\python.exe.
- WebGPT1 must have its saved conversation/browser startup configured.
- Two NEW requests, not retries of interrupted prior work.

## 2. Offline Regression

    cmd.exe /c run_sr_first_readback_regression.bat

Expected:
    commands=9
    passed=9
    failed=0

Logs: localdata/logs/sr-first-readback-regression/<timestamp>/.
FAIL prints the last 80 test-log lines in the console. This is OFFLINE only.

## 3. Live launcher

    cmd.exe /c launch_sr_first_readback_webgpt1.bat

Effective environment:
    SMARTAGENT_SR_FIRST_READBACK_ONLY_V1=1
    SMARTAGENT_GAR_V1=0
    SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=0
    SMARTAGENT_GAR_FRESH_CONTEXT_URL=(empty)
    TRI_ONE_TASK_RESULT_V1=1
    TRI_ONE_TEXT_ONLY_V1=1

The existing launch_webcopilot.bat starts its Controller with SmartAgent
.venv Python. Neither Chrome profile nor shared runtime installation changes.

## 4. SR-02 Prompt

請只進行文字解說，不要執行檔案或系統操作。詳細解釋 C++ RAII，
提供三個 Markdown cpp 程式碼區塊，分別示範 std::ofstream、
std::unique_ptr、std::lock_guard。每段說明 destructor、lifetime
和 exception safety。最後單獨一行輸出 SR02_END。

## 5. SR-03 Prompt

請只提供文字與範例，不要執行檔案或系統操作。產生有效 JSON，
以 Markdown json 程式碼區塊呈現。JSON 包含 nested objects、array、
繁體中文、雙引號字串、Windows 路徑 C:\camera\tests\ae.json、
newline、tab、反斜線，以及一個含有三反引號 C++ Markdown
片段的字串欄位。完成後解釋逃逸字元。最後單獨一行輸出 SR03_END。

Wait until the prior request shows "[SR-GATE] CAPTURED" before submitting
the next. The Controller does NOT send a completion ACK to the model.

## 6. ZIP collection and review

    cmd.exe /c collect_sr_first_readback.bat

Output: localdata/logs/sr-first-readback/bundles/SR_FIRST_READBACK_*.zip

ZIP only includes capture.json, extracted_response.txt and the selected
assistant DOM node innerText (if available), SR Offline test logs, a version
identifier and a per-file SHA256 manifest. It deliberately excludes browser
profile, cookies, tokens, auth state, arbitrary runtime logs and whole-page
HTML. The captures contain verbatim assistant response text; review them
before sharing. No screenshot can be reconstructed from this ZIP alone.

Compare each capture's extracted_response.txt with dom_turn_inner_text.txt.
The latter may include extra UI controls; a byte mismatch is diagnostic,
not automatic proof of bad scraping. Check SR02_END / SR03_END, Markdown
fences, JSON escapes and parse normalizations. We cannot independently prove
the screen rendering matches capture bytes without a screenshot/visual record.

A valid protocol at this stage is only a **syntax result**: semantic ACK,
full WebAgent admission, tool execution and terminal SUCCESS are NOT_RUN.
