# Temporary RCA auto-upload on worker interruption

This is a software-owned debug hook, not a Planner action or a recovery permission.

On shared RemoteAgent/WebCopilot worker exit states PAUSED, INTERRUPTED,
FAILED and CANCELLED, the worker calls the fixed Windows collector:

    C:\Users\user\Desktop\remoteagent\smartagent\collect.bat TASK-... RR-...

The collector generates a sanitized RCA ZIP under:

    C:\Users\user\Desktop\remoteagent\smartagent\localdata\diagnostics\gar_live_rca

The worker checks the ZIP's parent directory, CRC, V2/V3 schema, Task ID, Request
ID and SHA-256. It then requires GitHub CLI's existing credentials to confirm
that b43120000/test is private, uploads the ZIP to:

    https://github.com/b43120000/test/tree/main/tasks

Each ZIP gets a UTC timestamp, Task ID, Request ID, checksum prefix and unique
suffix. No GitHub credentials are printed, and existing ZIPs are not replaced.
The uploaded Git blob is verified through a fresh GitHub API read.

Worker Runtime log stages:
- AUTO_RCA_GITHUB_UPLOADED: includes github_url, github_commit, sha256.
- AUTO_RCA_GITHUB_UPLOAD_FAILED: includes a reason and local_zip when available.
- AUTO_RCA_HOOK_ERROR: diagnostic hook failed unexpectedly.

GitHub errors do not alter the interrupted Task's state; the local ZIP remains
for manual upload or later diagnosis. No retries or background promises are made.
This debug hook intentionally uses the stated fixed Windows path and does not
expand the Workspace ACL or override Runtime authorization.

Verification (offline, no Windows collector or network required):

    python -m unittest source/WebAgent/tests/test_auto_rca_upload.py -v

Limitations: this hook covers the shared RemoteAgent/WebCopilot worker, not a
separately launched standalone local WebAgent. Actual Windows collector and
private GitHub upload must be validated on the target machine.
