# Git39 — Generic UNKNOWN command human approval and historical REJECTED recovery

Base: Git38 acaacc0fc8c8b2e97767b14778512a4917e3168b.

## Single PokeClaw loop and risk classification

For run_command, Runtime evaluates the canonical positive read grammar,
hard Command Security and a bounded set of known mutation forms.

- PROVEN_READ → existing Read Scope path; executes with real ToolResult.
- MUTATING → existing Write Scope, success_criteria, verify and safety.
- UNKNOWN → never elevated to read-only on Model assertion; requires
  existing write/execute authorization *and* one-action human confirmation.
- FORBIDDEN → existing command security and/or hard policy reject.
- Compound shell commands are UNKNOWN unless separately forbidden; no
  classification of an initial verb alone can authorize a compound command.

For example, an exact single Get-FileHash -LiteralPath '<path>'
-Algorithm SHA256 is PROVEN_READ and enforces literal file Read Scope.

UNKNOWN commands can use INSPECT only if the real classifier returns
UNKNOWN and the stronger general-mutation/postcondition checks are met.
The model has to include a nonempty success_criteria and structured
verify. No automatic command or operation rewrite is performed.

## Human confirmation

- Only commands that passed schema, hard security and scope preflight
  can request human permission. Permission alone grants no wider
  Workspace, ACL, Network or Write Ceiling authorization.
- Use existing Telegram security-confirmation event/buttons and the
  crash-safe SecurityApprovalLedger. An explicit authorized click is
  required. Binding covers task, request, new action ID, exact action
  digest (including command and epoch), cwd, workspace, approval kind,
  chat ID and manifest digest. Ledger consent is consumed once.
- A 600-second deadline is enforced and cannot become implicit approval.
  An authenticated Telegram user can reject immediately. Missing
  notifier/identity fails closed and is reported as unavailable.
- On timeout, the agent may propose one alternative known-safe/authorized
  Action; if that is rejected or fails, the task records FAIL and PAUSED
  with an explicit continuation hint. Another UNKNOWN command does not
  request another ten-minute wait on the same request.
- Continuation snapshots contain only pending unexecuted command hash,
  operation and old approval reference, never a reusable grant or command
  body. A resumed task re-proposes a new Action, reruns every Admission
  check and requests a new one-time user click.

## Comparator

- Exactly matching all real ToolResults and the Runtime ledger remains
  mandatory, including older failures and REJECTED results.
- REJECTED and FAILED cannot themselves prove a positive goal.
- A subsequent SUCCEEDED, executed=True ToolResult that independently
  proves the original completion conditions may PASS, even if earlier
  attempts failed or were rejected.
- UNKNOWN, RUNNING, PARTIAL remain fail-closed, as do all-failure traces.
- Model-generated goal checks must refer to succeeded evidence. A
  later successful operation does not automatically prove a missing
  SHA256/manifest comparison.

## Live case D

For an authorized SHA256 read, Git39 admits the strict Get-FileHash
command with operation INSPECT, obtains stdout/exit code via existing
PowerShell ToolResult, and the model compares observed digest to
manifest. More complex unclassified commands request one-time user
approval. No Git deployment stage or Goal Driver is introduced.

The GitHub CI tests do not attest to a local E: drive or authenticated
private GitHub fetch; a subsequent Windows Live deployment and independent
hash comparison are required.
