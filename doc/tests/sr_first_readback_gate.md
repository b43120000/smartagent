# Shared Runtime SR-04 / SR-05 — byte fidelity and repeated Assistant identity

Base commit: dc1ee74e77620a2ed05b8d05d9557be269006b1d.

This branch keeps **First-Readback-Only**, with GAR/D1/format repair/slot
reconstruction/Tool Action execution disabled. The request-owner assistant
turn is observed only after UI idle and text stability. The diagnostic stop
`SR_FIRST_READBACK_CAPTURED` is **expected**, not a task success.
The ordinary Runtime/Parser acceptance rules are unchanged when the flag is OFF.

## 1 — Offline (SmartAgent project Python)

Run from a CLEAN checkout of this exact candidate SHA:

    cmd.exe /c run_sr_first_readback_regression.bat

Expected commands=10, passed=10, failed=0. The 10th command validates strict
manifest SHA for 203 tracked core files. The new SR04/05 test includes:
mixed LF/CRLF+Unicode byte fidelity, long-response section markers,
two independent identical-content Assistant turns, stale/same-identity
negative case, and a tampered ZIP must be rejected.

If FAIL, do not start Live. The BAT prints last 80 test-log lines and saves
logs in localdata/logs/sr-first-readback-regression/<timestamp>/.

## 2 — Start WebGPT1 (SmartAgent .venv Python)

    cmd.exe /c launch_sr_first_readback_webgpt1.bat

The BAT sets:
    SMARTAGENT_SR_FIRST_READBACK_ONLY_V1=1
    SMARTAGENT_SR_EVIDENCE_DIR=<checkout>\localdata\logs\sr-first-readback
    SMARTAGENT_GAR_V1=0
    SMARTAGENT_GAR_D1_FAULT_INJECTION_V1=0
    SMARTAGENT_GAR_FRESH_CONTEXT_URL=(empty)
    TRI_ONE_TASK_RESULT_V1=1
    TRI_ONE_TEXT_ONLY_V1=1

It calls launch_webcopilot.bat, which uses .venv\Scripts\python.exe.
No change to Update.bat, main, browser profiles, or normal Parser admission.

## 3 — SR-04 Long Response (send once; wait for CAPTURED)

請只提供繁體中文文字，不要執行任何檔案或系統操作。

請撰寫一份 Android Camera 3A / ISP Quality Validation 長篇說明，
不少於 3,500 個中文字，且必須依序包含以下原樣標記：

SR04_BEGIN
SR04_SECTION_01
SR04_SECTION_02
SR04_SECTION_03
SR04_SECTION_04
SR04_SECTION_05
SR04_SECTION_06
SR04_SECTION_07
SR04_SECTION_08
SR04_SECTION_09
SR04_SECTION_10
SR04_SECTION_11
SR04_SECTION_12
SR04_SECTION_13
SR04_SECTION_14
SR04_SECTION_15
SR04_SECTION_16
SR04_SECTION_17
SR04_SECTION_18
SR04_SECTION_19
SR04_SECTION_20
SR04_END

請按序為每一節提供不同的完整技術說明，包含 AE、AWB、AF、Sensor
SNR、Dynamic Range、Tone Mapping、Flicker、低照度、CTS/ITS 及
KPI 量測等主題。每節至少兩段，不要把全部內容寫成短清單。
最後一行只輸出 SR04_END，不要在其後增加任何文字。

### SR-04 diagnosis

Inspect:
- capture.json `test_case.status=MARKERS_PRESENT` and
  `missing_markers=[]` (a failed marker check can be the model's own omission).
- SHA256/byte length for original vs every on-disk UTF-8 text artifact.
- decoded `final_response.content`, DOM innerText, DOM textContent, and
  the selected pre/code/table subtree. Note these are *different DOM views*;
  whitespace or controls may differ harmlessly.
- If the model omitted sections in its `final_response.content` and both DOM
  views show the same omission, do not label it as Runtime truncation.

## 4 — SR-05 Identical Content, two NEW requests

Send the *exact same prompt* below TWICE as two independent WebGPT1 user
requests. Wait for a separate `[SR-GATE] CAPTURED` after each send.
Do not send SR-04 or another prompt BETWEEN the two SR-05 requests.

請只提供文字答案，不要執行任何檔案或系統操作。
你的最終自然語言答覆必須嚴格只有以下兩行，逐字相同，不能增加
序號、Markdown、程式碼、日期或說明：

RAII 使用物件生命週期管理資源。
SR05_END

For the two requests, the expected `final_response.content` SHA
is identical; outer V9 Envelope may differ because progress/action IDs can
change. Compare separate Request IDs, Scope SHA and Assistant Durable IDs.
If distinct IDs/Scope and identical content, the evidence is *consistent
with* two independently bound turns. Missing/identical identities are
inconclusive or require review, **not** an automatic execution admission.

## 5 — Collect one ZIP

    cmd.exe /c collect_sr_first_readback.bat

The bundle contains all available SR captures from this checkout, including
legacy SR-02/03 samples if present, plus only allowlisted Offline logs.
New captures contain:
- extracted_response.txt, dom_turn_inner_text.txt, dom_turn_text_content.txt
- capture.json, turn_identity.json, dom_selected_structure.json
- evidence_integrity.json and sr05_pair_comparison.json in ZIP root
- bundle_manifest.json (per-file SHA256) and README_REVIEW_BEFORE_UPLOAD.txt

New byte-fidelity records are checked against the on-disk bytes before
packaging; corrupted new captures cause packaging to fail. Legacy captures
are labeled LEGACY_UNVERIFIED rather than treated as corrupted. No whole
page HTML, browser profile, cookies, tokens, arbitrary local logs or
other historical conversation text is automatically included.

The current response body and selected DOM texts CAN contain private
information. Inspect ZIP contents before sharing.

## Interpretation limitations

This test checks the **first observed** assistant response, not the hidden
network transport or raw original model tokens. DOM textContent may include
hidden elements. Provider identity can use a structural/ordinal fallback
rather than a proven stable message ID. SR-05 pair status is diagnostic;
it does not override existing `response_sha_unchanged` recovery
guards. Semantic ACK and final terminal admission remain NOT_RUN.
