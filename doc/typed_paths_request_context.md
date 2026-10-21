# Typed path recovery and request-local input reuse

Execution package change, 2026-10-13. Shared RemoteAgent/WebCopilot WebAgent loop; no provider selector changes.

## Typed bridge replies

Strict JSON is still accepted. Invalid JSON now reports the decoder reason, line, column and offset instead of only `narrative_slot_invalid`. The next slot prompt gives exact Windows escaping guidance. Exhausted field recovery includes the diagnostic reason and available line/column in its failure message.

For a path-list slot, prefer:

```text
[V9_PATHS]
C:\example\one
C:\example\two
[/V9_PATHS]
```

For a single path, use `[V9_PATH]` / `[/V9_PATH]` with one literal line. No JSON unescaping, quote removal, slash normalization, drive inference or ADS stripping occurs in literal tags. Blank lines, C0 controls, more than 32 paths and oversized values are rejected. CRLF line endings normalize to LF. These markers are not a generic parser for commands, source, plans, queries or other list fields. Arbitrary malformed protocol JSON remains rejected globally.

## Request context

Only stable input fields are eligible: `path`, `paths`, `project_root`, `output_path`, `expected_filename`. Path entries must be absolute. Workspace/root override fields, commands, plans, decisions, snapshots, tool results and verification states are not cached through this feature.

The Runtime seeds explicit user-request paths and remembers stable literals from successful executed actions. The context journal is under the calling loop's runtime base:

`localdata/runtime/request_contexts/<request_id>.json`

Entries are bound to request ID, task epoch and exact field. References contain `ref_id` and positive integer `version`. Values are immutable; invalidation appends a new version, and prior versions cannot resolve as current. Provenance is a fixed enum. The original goal is retained as a digest here, not copied as raw text; normal Progress and request ownership remain the owners of the actual goal/plan. Journals remain for local diagnostics; this does not introduce cross-request reuse or durable action resume.

Normal prompts advertise up to 16 complete entries within an 8 KiB catalog. Missing entries are not inferred. To reuse a listed input:

```json
{"tool":"list_directory","action_id":"A-READ","field_refs":{"path":{"ref_id":"CTX-<listed-id>","version":1}}}
```

Do not supply both a literal and a reference for the same field. Wrong field, request, epoch, version, malformed/corrupt journal and stale entries fail closed. Resolved fields still pass normal schema, action digest, target authorization, dependency, native tool and terminal evidence validation. Context data never creates access rights or proves success.

Narrative single-slot recovery can select a listed reference using `[V9_CONTEXT_REF]` with one JSON ref/version object. The canonical action preserves this as `field_refs`; normal admission rechecks and expands it. A reference cannot be replaced by a guessed value if it fails.

Accepted Progress planning fields already use the existing explicit inheritance policy. Snapshot/tool-result reuse stays governed by native evidence lifecycles; this context journal does not bypass those rules.

## Validation

`python -m WebAgent.tests.validate_typed_paths_request_context`

Fixed local tests exercise malformed Windows JSON → precise feedback → literal path success, strict JSON alternatives, whitespace/ADS preservation, controls/type rejection, explicit reference reuse, exact field/version/request/epoch binding, append-only invalidation, corrupt journal rejection, restored bridge identity, existing authorization denial after expansion, and cached action replay without a second tool invocation.

These are deterministic local loop/tool tests, not live browser IT. Existing running processes must restart to load new source. No development-package sync or Git checkpoint is performed by this change.
