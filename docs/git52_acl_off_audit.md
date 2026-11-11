# Git52 ACL OFF read-only verification

Base commit: 6b5629e1572a79b3469b962862bcbb542d9b3aab.

## Included
- install_smart_agent/verify_acl_off.ps1: conservative read-only ACL OFF audit.
- check_acl_off_git52.bat: local Windows launcher.
- install_smart_agent/tests/validate_git52_acl_off_audit.ps1: disposable Windows fixture.
- .github/workflows/git52-acl-off-audit.yml: Windows CI and Software Guard regression.

## Local use
Run check_acl_off_git52.bat from the SmartAgent install directory.

With an authoritative historical Executor SID and known managed roots:
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_smart_agent\verify_acl_off.ps1 -ProjectRoot . -ExpectedExecutorSid "S-1-5-21-..." -ManagedPaths "E:\your-workspace" -RecurseManagedPaths

## Evidence semantics
- FAIL means a proven contradiction of OFF (e.g. transitioning_on, a local security residue, Turbo intent, or matching Executor ACE).
- INCOMPLETE means that the read-only scan did not prove the entire OS state.
- full_os_verification is always false: file-create/rename/delete postconditions and an authoritative historical managed-path inventory require separate verification.
- fixture_only=true means a non-production CI fixture; never certify a live installation from it.

Persistent workspace_access_policy.json and unused Executor account records are not automatically an OFF failure.
GitHub-hosted runners test their own disposable environment. They cannot see local D:\webcopilot2\smartagent.
