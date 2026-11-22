# Git62: post-pull transactional authorization finalizer (initial phase)

Invocation on the intended installed checkout:

```cmd
cd /d C:\smartagent
git pull --ff-only origin git62
update.bat <full-40-character-HEAD-commit>
```

This is a **post-pull finalizer**, not a complete staged updater. The local
`git pull` has already changed executable bytes, so it is impossible to promise
transactional recovery of those old bytes unless an earlier authenticated
backup was independently saved. **Do not treat this command as automatic
rollback or a substitute for a complete staged updater**.

The entrypoint verifies the exact git HEAD and the entire canonical
`config/protocol_manifest.json` ledger before UAC and repeats validation
after stopping agents. One UAC elevation invokes a read-only all-Root native
precheck; all enrolled Roots must pass. Normal Force Stop cleans the Runtime
but never requests `--after-git-update`. The pre-existing admin
`turbo_multi_root_upgrade_authorization.ps1` verifies the protected updater
pin, Executor ownership, SIDs, and broker isolation before publishing the
current manifest digest. Afterwards, the machine authorization, each Native
Root, and RemoteAgent security preflight must pass.

`update.bat <commit> --verify` performs preconditions and the native
read-only Root verification without Force Stop or an authorization write.

Current phase supports **ACL ON and two or more fully enrolled Turbo
Roots**. Other security states fail closed, rather than silently
reprovisioning identities or changing the permissions of an existing Root.

No normal update path may skip a failed kernel negative-read test.
A failure before authorization publication leaves the existing machine
authorization as-is. A post-publish failure returns RECOVERY_REQUIRED and
must not claim to have restored a previous version. A future staged update
will need its own durable, protected pre-pull backup and rollback mechanism.

Only run this with trusted commits and a verified Git checkout.
