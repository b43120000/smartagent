# Git40: Software-owned pre-resume human approval (Telegram + WebCopilot)

Base: Git39 \`741c59da4367754e73ffb16a2104b1067ddd1c85\`.

## User behavior

1. A task hits an unclassified shell command. The existing bounded
   UNKNOWN_COMMAND prompt times out or cannot be delivered, and the task
   pauses without executing the command. Protected state now remembers the
   exact command, its operation, working directory and source task identity;
   ordinary public Blackboard / continuation stores keep only its hash.
2. The user sends \`接續工作 TASK-...\`. The *software transport*, before
   starting the child task or talking to the model, looks up the paused
   pending command for that task and confirms owner/workspace/epoch.
3. Telegram sends an inline 「允許執行一次」/「拒絕」 prompt. WebCopilot
   injects an explicit two-button modal in its linked browser page; only a
   direct trusted click can select an outcome. Neither UI executes commands.
4. The choice is persisted via SecurityApprovalLedger with a fresh APR and
   transport-scoped channel + task owner. A valid decision creates the child
   continuation task; a one-hour *bounded* one-use approval lease supports
   long legitimate recovery jobs, without changing permanent permissions.
5. When the Executor proposes the exact same command, operation and
   working directory *after fresh admission*, it atomically consumes this
   one-time decision. APPROVED proceeds to the usual execution safety and
   Scope checks. REJECTED never runs the command. Changed commands cannot
   reuse the grant. No old APR or evidence is replayed.
6. Historical Git39 Telegram tasks can recover the exact command from
   the trusted SecurityConfirmationRequired event if and only if source
   Task ID, old APR and command SHA256 match. If raw command is absent,
   the software explicitly refuses to manufacture an approval prompt.

## Security and crash behavior

- A button is *consent*, not a new Workspace, ACL, Network, or Write
  Ceiling grant. All command checks still run for the new child task.
- No child task is queued until an authorized owner clicks. Approval
  cannot be inferred from the ChatGPT model message.
- A 600-second pending challenge expires fail-closed. A selection is
  preserved for at most 3600 seconds, bound to a specific resumed child,
  and consumed once.
- Decisions, source command details and bindings are stored under the
  Runtime's protected Security state root with a process lock.
- The ordinary public model context receives only pending hashes.
- If the local app restarts during a WebCopilot prompt, the user may
  send \`接續工作 TASK-ID\` again to request a fresh prompt; the old
  approval is never automatically considered consent.

## No regressions to PokeClaw

The generic \`ToolCall -> Admission -> Approval -> ToolResult -> Model\`
loop is unchanged. There are no Case D-specific procedures,
Goal Driver/Prerequisite operations, hidden shell execution, or automatic
model command rewrites.

## Real-world validation

CI covers the serialized approval Ledger, exact matching, denial,
legacy SHA-verified Telegram event recovery, WebCopilot UI script and
prior Git33–39 behavior. It cannot prove that a real Windows user's
Telegram button or browser-modal click was received; run a local Live
approval/continuation scenario for that.
