# GAR D1: Long Multiline Composer Transport Unblock (2026-10-31)

## Fixed baseline and observed evidence

Baseline: `38ce3d6c1afe9a727672838a2bd82e3203979cc6`.
Previously observed Request `WA-363DF1B5D05B454C`: GAR L4 supervisor review, WebGPT1 resume and D1 injection release all passed, but the first real WebGPT1 Round 8 wrote a 11707-character prompt and the first 4096-character `keyboard.insert_text` was read back as only 17 characters (`[WEBUI_SUBMIT_ID]`). Readback failed after 33 observations / 8.31 s and correctly quarantined the draft. This does not prove a CPU-performance root cause.

## Narrow source change

For ChatGPT prompts at least 4096 characters that contain a newline, the existing bounded Writer now preserves the prompt as a series of content units (at most 1024 characters) and explicit `Shift+Enter` non-submit line breaks. Each unit is strictly read back and Journal-checkpointed before proceeding. No blind replay, bulk clear, unsafe verification bypass or new Provider contract. Short prompts and long single-line inputs use the existing legacy framed `insert_text` path, preserving the WebGPT2 bootstrap flow. The whole-operation time budget grows with unit count but is capped at 240 seconds; the existing independent post-write observation and send-state verification remain authoritative.

The collector additionally exports `readback_failure.unit_kind` (text/soft_break) to its redacted, request-scoped ZIP.

## Offline and Live gates

Offline (must PASS before Live):
- `run_gar_debug_regression.bat` (five checks: L4 supervisor, D1 injection, collector, Composer operation, strict 204-file Manifest)
- `run_gar_regression.bat` (existing broad GAR regression)

Live (separate human-run gate):
1. Confirm no other WebCopilot Controller. Use a distinct **clean WebGPT1 conversation with an empty draft**; preserve the existing quarantined conversation and journals. Keep the specified existing WebGPT2 URL and Chrome3.
2. Launch `GAR_debug_webcopiolt.bat --webgpt-url "<clean WebGPT1 ChatGPT conversation URL>"` from the same checkout.
3. After READY, send **exactly** `Explain RAII in C++.` once in WebGPT1 without attachments.
4. After terminal success or safe interruption, run `collectData.bat` or `collectData.bat --request-id WA-...`. Upload the ZIP after reviewing its redacted advice fields.

Success requires same-request L4 trigger, WebGPT2 accepted review, original WebGPT1 resume, D1 fault injection release, and `protocol_loop_completed` `task_outcome=SUCCESS` on the original task. A regular RAII SUCCESS without L4 is not proof.

**Safety:** The previous QUARANTINED/UNOWNED_DRAFT is not automatically deleted or recovered; a failed segmented unit remains quarantined. No claim of Windows Live PASS until user evidence is reviewed. Multi-Provider Reliable Composer and six-step D2 are deliberately out of scope.
