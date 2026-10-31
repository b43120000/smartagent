# P2 Hybrid Writer / Normal Admission diagnostic experiment

Candidate base: `06d53243131076035786aab29e9092683ef5ba17`.
Experiment is separate from P1 and `main`. The existing
`collect_sr_first_readback.bat` is NOT renamed.

## Start

After switching to the verified experiment SHA:

    cmd /c launch_p2_hybrid_webgpt1.bat

This starts NORMAL WebAgent runtime with:

- `SMARTAGENT_SR_FIRST_READBACK_ONLY_V1=0` (no controlled first-readback stop)
- `SMARTAGENT_P2_READONLY_DIAGNOSTIC_V1=1`
- `SMARTAGENT_COMPOSER_HYBRID_ONCE_V1=1`

Session Bootstrap/Attach always uses the normal protected Writer, even
when the Hybrid flag is enabled. Only after Controller READY, the first
official WebDirect task's Planner prompt over 512 characters (including
the WUS transport envelope) uses one-shot Hybrid. Final render ACK and
subsequent Planner rounds do not rearm it.
If no eligible write occurs, no Hybrid event is produced. The first 512
characters are verified in small units; the remaining content is written
without per-unit readback, with a strict whole-tail DOM readback and the
existing independent observer/revalidation before Send. Multiline input
uses Shift+Enter (never a raw multiline insert_text). A timeout or mismatch
is a failure, not permission to retry or submit. Existing writer remains
the default when the flag is absent.

A single conversation is sufficient. Test a benign text-only task first:
"Explain C++ RAII using a simple std::unique_ptr example. Do not execute
local commands or modify files." The Runtime adds its own protocol and
WUS envelope. Observe whether one Hybrid write was used, and whether normal
admission and terminal completion happen. Optionally, test a separate
read-only tool request afterward, without using any write/delete operations.

## Evidence

With the controller no longer writing to the log, run:

    cmd /c collect_sr_first_readback.bat

The single ZIP under `localdata/logs/sr-first-readback/bundles/` retains
P1 captures and **adds** optional `p2/` JSON:

- `p2_turn_observation.json`: request-bound assistant hashes and DOM counts
- `p2_admission_trace.json`: selected scalar fields from runtime events
- `p2_terminal_summary.json`: accepted/completed/failed IDs, UNKNOWN when
  evidence is incomplete
- `p2_writer_trace.json`: hybrid write timing, sizes, hashes only

Unfiltered `webagent_runtime.jsonl`, user prompts, final content, cookies,
browser profiles and whole-page HTML are NEVER copied by the P2 collector.
Operator still reviews the ZIP, which also contains historic P1 model text.

**Limitations:** P2 hook is best-effort, cannot change admission or action
execution, and may be missing after early failures. The collector selects
the newest controller_started session boundary, excludes earlier P2 failure
records from its verdict, records only allowlisted Browser Stage labels
(SESSION_BOOTSTRAP / SESSION_ATTACH / TASK_PLANNER / FINAL_RENDER_ACK), and
captures sanitized task ingress metadata plus classified protocol rejection
reasons (not the original error text). The event log is
bounded and cannot guarantee a complete task transcript. A PASS requires
correlated RequestScope, accepted turn commit, protocol admission, and
normal terminal result; one event alone is insufficient. Live must be
performed manually; GitHub offline CI does not test ChatGPT browser DOM.
