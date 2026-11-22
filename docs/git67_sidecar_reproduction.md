# Git67 Sidecar Reproduction — NO FIX

## Basis and boundaries

Branch: `git67/case-c-action-contract-repair` (initial SHA
`d122d0adf341d2a47feccb99cd78cfba48abe94d`).

The previously reported RCA contained two **different** findings:

1. Incorrect backup path `E:\\AIspace.case_d_backups` was present in stored
   Task A and Task B request content. The original upstream Telegram payload
   and user input were not retained; first corruption point remains **UNKNOWN**.
2. In `WebAgentProtocolLoop._restore_continuation` a result without
   `action_digest` passes an OR check using the corresponding Action Ledger
   `signature`, then triggers `KeyError('action_digest')` through the eager
   argument to `dict.setdefault`.

This sidecar calls the *real unmodified git67 functions* against in-memory
fixtures and a mocked Telegram HTTP response. The source shapes simulate
the RCA's 12-result and 2-result samples; they do **not** claim to be copies
of those historical data records.

## Run

From the repo checkout:

```powershell
$env:PYTHONPATH = 'source'
python -B -m pytest -q source/WebAgent/tests/test_git67_sidecar_reproduction.py
python -B -m WebAgent.tests.sidecar_git67_reproduction
```

To inspect actual historical UTF-8 request text on a Windows machine,
provide optional files (read-only). Paths must point to existing extracted
**plain request text**, not JSON records or synthesized summaries:

```powershell
python -B -m WebAgent.tests.sidecar_git67_reproduction --request-a C:\\path\\to\\task-a-request.txt --request-b C:\\path\\to\\task-b-request.txt
```

The report verifies SHA256, byte length and path presence against
`01ba6338d521f40c25e8f56c274a73562d6f1147273d32fdbd0c721914db741a`.

## Interpretation

- **REPRODUCED**: in-memory Result Records lacking `action_digest`,
  paired with Action Ledger `signature`, trigger the exact KeyError in
  production git67 source. The complete-digest control must restore.
- **NOT REPRODUCED**: any test unexpectedly passes or fails differently.
- **INCONCLUSIVE**: path-corruption origin. In-memory Telegram JSON decoding,
  receiver normalization, continuation parser and minimal decision parser
  preserve either provided path. This does NOT cover the original Telegram
  upstream payload, mobile clipboard, Markdown rendering, a live ChatGPT
  response or the historical full Prompt.

Production path authorization remains untouched. No Mutation ToolCalls,
fixture operations, credential access, file deletes, or network calls are
made. An operator can opt to supply actual request text separately.

Do not label `path fidelity PASS` as `first corruption point discovered`.
Do not interpret the intentionally reproduced KeyError as CI infrastructure
failure; the tests **expect** it and verify the traceback.
