# Evidence-typed remote access and runtime software inventory

Base Git commit: 2b6214c7dcceedbbf60c55219b7956d1dd0c9dea
Goal: prevent Case D UNKNOWN authorization from being treated as DENIED, and
ensure the Planner knows usable preinstalled and task-recorded software.

## A. Access state contract

For a pinned GitHub E1 artifact reference, these are intentionally different:
- discovery state: ARTIFACT_REFERENCE_FOUND
- current repository authentication: NOT_CHECKED until independently verified
- authorization_state: UNKNOWN until an admission receipt exists
- artifact_retrieved: false until actually downloaded
- source_integrity: UNKNOWN until separately verified

The Planner's remote_artifact_access_readiness uses:
UNKNOWN -> a safe Tool/Network/Scope readiness check under normal admission
AUTHORIZED -> a trusted request/epoch/repository/candidate-bound security receipt
DENIED -> a trusted confirmed Runtime policy denial
NEEDS_USER_APPROVAL -> a trusted Runtime determination that additional user
approval is required (not guessed from absent evidence).

Receipt schema: SMARTAGENT_REMOTE_ACCESS_ADMISSION_RECEIPT_V1.
This version defines and validates the consumer contract. The wider Runtime
security/admission pipeline must be the only producer of a trusted receipt;
do NOT let model text, a repository manifest, or a successful Git --version
probe fabricate such a receipt. Without one, the state stays UNKNOWN.
Current Git executable availability alone does not prove GitHub permissions.
A bounded one-time goal interruption repair asks the Planner to evaluate
the existing run_command/GIT_INSPECT route and actual admission checks before
replacing UNKNOWN with a user approval request. If policy denies the probe,
the Planner must preserve that evidence and request the exact missing approval.

## B. Software inventory

The Planner now receives runtime_software_inventory on *each* decision round.
Data sources:
- actually probed application-shipped Python/Git in canonical .venv:
  CURRENT_AVAILABLE only if the binary probe reports AVAILABLE;
- shallow Agent-owned .venv Scripts/bin/git/cmd inventory, bounded to 24
  executable candidates, marked PRESENT_REPROBE_REQUIRED;
- already-scoped ProjectSync environment.json verified tool/dependency
  records, bounded to 16 per category, marked RECORDED_REPROBE_REQUIRED.
No .venv candidate discovery invokes an executable or makes network requests.
No ProjectSync record or historical evidence grants execution or installation
authorization. An explicitly user-selected path takes precedence; the
canonical .venv is a bundled/default *location*, not permission to modify a
shared environment. Shared .venv mutations remain separately authorized.

## Expected Case D Live observations

- Source Discovery: catalog_status READY, catalog_matched_count 1,
  artifact_candidate_count 1.
- Planner Context: runtime_software_inventory.bundled_tool_probes includes
  git CURRENT_AVAILABLE when its actual local probe succeeds.
- Planner Context: remote_artifact_access_readiness.authorization_state UNKNOWN
  and next_verification CHECK_GIT_REPOSITORY_NETWORK_AND_SCOPE_ADMISSION
  until actual scoped security/admission evidence is available.
- When Planner seeks early user input with no access attempt, a one-time
  REMOTE_ARTIFACT_ACCESS_READINESS_REQUIRED diagnostic guides a real admitted
  probe rather than assuming a denial.
- Git remote access, archive download, verification, and destination writes
  are NOT performed by this metadata-only change.

## Test boundaries

Regression covers all four access states, request/epoch/repository binding,
missing receipt, mismatched/forged state, .venv executable candidate names,
probed Git/Python, ProjectSync recorded tool and dependency candidates,
source discovery and prior Goal/Planner regressions.

End-to-end authorization transition requires a separate Runtime-owned
security receipt producer / action admission integration and Live test;
absence of such a receipt is not claimed as an AUTHORIZED or DENIED result.
