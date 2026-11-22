# Git75 P0: Planner roundtrip reduction + Project Sync source freshness

## Scope and non-negotiable gates

This increment changes only the Git75 branch. It does **not** modify the
Git74.1 final Finish Comparator, Frozen Goal requirements, ACL/Turbo/Auto
Approve admission, irreversible command replay policy, or the terminal
green/PASS mapping. A deterministic optimization is recorded only when
its underlying Runtime MIN-EV is persisted and read-back verified.

### P0-1: Planner roundtrip reduction

* After an actually executed Action, Runtime binds its immutable MIN-EV to
  the current frozen Stage, exactly as before.
* If **all** Stage acceptance predicates have qualifying evidence,
  predeclared Expected/Observed matched, and evidence read-back passed,
  Runtime invokes the same `GoalStageLedger.commit()` validator and
  freezes the Stage automatically.
* No automation on missing Expected, failed verification, unpersisted
  evidence, unresolved Timeout or unknown effects. Existing Planner
  `stage_commit` remains available if Runtime did not auto-commit.
* Purely syntactic, lossless and deterministic decision aliases such as
  `args`→`arguments` can be canonicalized without an additional browser
  trip. No path, command, policy, approval or observed value is rewritten.
* Blackboard evidence: `GOAL75_PLANNER_ROUND_SAVED`,
  `GOAL75_STAGE_VERIFIED_COMMITTED` with
  `commit_origin=RUNTIME_FROZEN_EVIDENCE_GATE`,
  `GOAL75_LOCAL_SCHEMA_REPAIR`, and unchanged Stage Ledger hashes.

Do not equate saved round count with measured elapsed time. Measure actual
`browser_send_started` to `browser_response_completed` timing in a LIVE
Task, compare the number of Planner rounds with the same requirement on
the previous pinned build, and report model/browser wait separately from
Executor wait and command execution.

### P0-2: Project Sync independent E2E

The independent sidecar runs the **production underlying implementations**
of `project_sync INDEX_ONLY` and `query_project` against a temporary
real project (via `_execute_tool_impl`). This deliberately isolates source
freshness from host-specific Tool Admission: it is **not** an authorization
or Native Broker PASS. A separate regression drives the real
`execute_tool` dispatcher with a denied Broker and requires an unexecuted
REJECTED outcome. The sidecar exercises:

1. Initial INDEX_ONLY publication and actual file-byte query.
2. Pinned `read_context_ref` and source SHA evidence.
3. Change source bytes without changing file size or mtime.
4. Pinned stale read **rejected**, without silent index rebuilding.
5. Unpinned read triggers one `INDEX_REBUILT_ONCE` with new SHA.
6. No previous bytes returned after refresh; current snapshot changes.
7. Explicit INDEX_ONLY resync; subsequent query reuses current index.
8. Unauthorized/out-of-scope Project Sync target is rejected.

The test creates only a new temporary folder under the selected Workspace
and cleans it up. No existing project is mutated. The internal
implementation exercise does not grant permission to bypass the real
dispatcher or Windows OS Broker. It is not a claim that real Tool Admission,
Telegram→Browser→Planner delivery or FULL_BUNDLE browser Batch ACK has
been verified. If a task asks for transport sync, require separate RECEIVER-bound
acknowledgment and the `PROJECT_SYNC_READY` final barrier.

### CI and sidecar

```bat
cd /d C:\smartagent
set PYTHONPATH=C:\smartagent\source
C:\smartagent\.venv\Scripts\python.exe -m agent_core.goal75_project_sync_it --workspace-root E:\AIspace --output C:\smartagent\localdata\diagnostics\git75_project_sync_p0.json
```

Run this on an explicitly authorized test Workspace (prefer ACL OFF/Turbo
OFF for manual diagnostics). The JSON must contain `status: PASS`,
`native_broker_admission_verified: false`, verified new SHA, stale pinned
reference rejection, safe unpinned rebuild, no old-byte return, and scope
denial. This is **isolated source sidecar IT**, not real Tool Admission or
full Telegram Live.

### Suggested natural-language Telegram Live acceptance

In the authorized Workspace, create a brand-new dedicated test directory.
Run a normal `project_sync INDEX_ONLY` on its exact path. Use
`query_project/read_range` to confirm source A0 and SHA. Change A0 to B1
while preserving file size and mtime. Verify that an old pinned
`read_context_ref` fails, then use unpinned `query_project/read_range`
to obtain B1 and the new SHA. Explicitly resync the exact Project root and
verify a stable query reuses the new index. Verify out-of-scope sync is
rejected and no parent/sibling content was touched. Preserve every MIN-EV,
query ID, snapshot ID, action Expected/Observed, Stage Ledger commit,
and terminal Goal receipt. The final green state requires all original
criteria, never only a successful local index command.

Use `collect.bat <TASK-ID> <REQUEST-ID>` for the Telegram Live task and
inspect per-tool Project Sync evidence plus `unified_preflight_task.json`.
`STALE_REINDEX_REQUIRED` is not an accepted refresh. The reusable index
hint does **not** prove current source bytes; the actual queried file SHA
and content checks are mandatory.
