# GAR D1: last-assistant prewrite readiness gate (2026-10-31)

**Scope:** A temporary, read-only readiness condition in the existing ChatGPT bounded Composer transaction. Original baseline `c3d526c90321ac7c4fc0323ee720217ebcbea802`. Neither GAR supervision/injection nor the Send Verification protocol nor other Provider writer paths are modified.

## Why

A prior GAR D1 Live run successfully completed the WebGPT2 review and WebGPT1 resume but blocked while writing the next WebGPT1 message. A separate run observed a 17/4096 first-chunk readback. This gate is **not** a fix for ProseMirror multi-line insert failures. It only prevents the next Composer write while the previous conversation turn is visibly unresolved.

## Gate behavior

The existing `_wait_for_prewrite_stability` now checks `last_turn_status`, computed from pinned Provider Adapter **native DOM order** of the last User and Assistant nodes, not their relative counts or independent role ordinals. A nonempty, last-position Assistant response, `generation_active == False`, an empty or verified-owned Composer, an interactive editor, and **four consecutive stable snapshots** are required. The no-messages `EMPTY_CONVERSATION` case remains permitted so clean WebGPT1/WebGPT2 bootstrap can occur. A last User, missing/blank Assistant, unknown DOM order, active generation, foreign draft or changed message digest cannot pass.

Snapshots and Journal contain only role status and previously used message identities/hashes, no model response text. The parent now retains the Writer's explicit, no-mutation `PREWRITE_UNSTABLE`/`PREWRITE_FOREIGN_DRAFT` reason without invoking a redundant Observer. `collectData.bat` adds redacted `prewrite_reason_code` and `baseline_last_turn_status` fields to `composer_journals_redacted.json`.

**Safety:** No clear, replay, overwrite or send on an unverified Composer. No hidden "force pass" toggle. A prewrite rejection records `FAILED_PRE_WRITE`; an already partially mutated Writer still uses existing `QUARANTINED` rules.

## Offline + Live verification (manual Windows)

1. Run `run_gar_debug_regression.bat` (includes `validate_composer_operation`, collector and strict 204-file Manifest).
2. Run `run_gar_regression.bat` (original general GAR regression).
3. After stopping the old Controller, choose a **new WebGPT1 conversation with an empty Composer**, keep the designated separate WebGPT2 conversation and Chrome3, and launch `GAR_debug_webcopiolt.bat --webgpt-url "<clean existing WebGPT1 conversation URL>"`.
4. After READY, type `Explain RAII in C++.` once. When completed or safely interrupted, run `collectData.bat --request-id WA-...`, upload the resulting ZIP, and inspect exact stage.
5. GAR D1 E2E only passes with same-request L4 trigger, WebGPT2 accepted read-only review, WebGPT1 resume, fault injection release **and** `protocol_loop_completed` with `task_outcome=SUCCESS`.

This patch is not a cross-Provider rewrite, not D2, and not a claim of Windows Live PASS.
