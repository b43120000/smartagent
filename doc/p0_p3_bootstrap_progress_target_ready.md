# P0-P3 Protocol bootstrap repair + scoped AGENT_PROTOCOL_READY

## Incident
TASK-4F5601B205C8289373A7 / RR-TELEGRAM-F45E558EEFCD48C4
on f93f5420 failed before the first admitted Progress. Five consecutive
protocol rejections involved evidence_refs, total_steps, current_step,
pending_actions_not_preserved, base_evaluation, then a 600-second soft pause.
No operational Tool Actions were committed or executed.

## P0 Initial FULL Progress convergence
On the first report_progress with CONTINUE/PENDING and no accepted plan or
Tool Result, Runtime may normalize ONLY:
- absent outcome to PENDING
- missing/empty evidence_refs to the non-action REQUEST_ACCEPTED status marker
- absent/empty base_evaluation from the Planner's own nonempty current_focus
- omitted total_steps from a fully enumerated 1..N step list
- omitted current_step from its leading COMPLETED steps, except all-completed

Never repair explicit conflicting values, change Completion Contract, invent
a success condition, infer a Tool result, modify a goal, or run an action.
Any OTHER-TASK ref remains invalid. Log each field normalized. Unfixable
fields still fail closed with the existing Progress diagnostic.

## P1 CCRC unadmitted Action convergence
Under FIELD_REPAIR, a distinct Action ID with identical semantic payload
and identical dependency/replacement refs can be correlated to the already
held, never-executed action. Runtime restores the original Action ID, then
runs ALL normal Action/Progress/Scope/Gate admissions (no eager execution).
Changed action payloads cannot be restored. REPLAN_REQUIRED releases only
never-executed Progress holds; action results or permissions are not faked.
Existing bounded-repair and 600-second stagnation limits remain unchanged.

## P2 Exact target binding
Accept labeled Windows/Unix deployment roots with optional Markdown
backticks, quotation marks, and bold label punctuation; compare Windows
drive paths by normalized case/slashes. An explicit deployment label with
a malformed/unparseable path binds AMBIGUOUS and fails the Terminal Gate,
rather than silently bypassing it. Preserve exact-drive-root slash.

## P3 End-to-end regression + initialize READY
Added first-FULL/CCRC/path/session tests in dual Windows/Linux CI. A
response consisting only of AGENT_PROTOCOL_READY (or its square-bracketed
marker) in the awaited WebAgent BOOTSTRAP exchange is accepted as
SCOPED_MARKER_ONLY. The response MUST be exactly the marker (whitespace
allowed), from the browser-stable reply for that handshake, NOT a Task
reply, tool result, or SESSION_ATTACH. It does NOT echo protocol/session
identity, so readiness_identity_verified remains FALSE, while the runtime
stores the locally bound protocol state and records readiness_transport_scoped.
The full-identity handshake remains supported and preferred. Mismatched
complete identity blocks remain rejected, not downgraded to marker success.

## Live verification
Switch to the exact CI PASS commit and restart SmartAgent. Run a fresh
Case D bootstrap and check: Progress no longer sticks at 0/0; terminal
request-root compliance is bound for Markdown quoted path; the existing
content is independently verified; no destructive deployment is implied.
On the new bootstrap, one exact READY marker should arm the conversation
without repeated format repair. Neither CI nor READY marker proves any
actual task Goal SUCCESS.
