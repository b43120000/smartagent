"""Telegram-authenticated software-only CMD controls."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

CONTROL_LABEL = "執行cmd"
HEADER = "run cmd by TG"
MAX_LINES = 12
# Individual commands are sequential and always bounded. Remote network
# operations (git fetch, etc.) routinely exceed the old 20-second deadline.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
DEFAULT_BATCH_TIMEOUT_SECONDS = 900
MIN_COMMAND_TIMEOUT_SECONDS = 1
MAX_COMMAND_TIMEOUT_SECONDS = 600
MAX_BATCH_TIMEOUT_SECONDS = 1800


def _bounded_timeout(value: int, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise CmdControlError("TG_CMD_TIMEOUT_CONFIG_INVALID")
    return value


def _command_failure_hint(command: str, stderr: str, exit_code: object) -> str:
    """Explain a verified Git error without rewriting or retrying the command."""
    if (exit_code == 128 and re.match(r"(?i)^git(?:\.exe)?\s+fetch\b", command)
            and re.search(r"(?i)fatal:\s+couldn't find remote ref\b", stderr)):
        return (
            "GIT_REMOTE_REF_NOT_FOUND：遠端沒有指定的 ref。"
            "這不是 Timeout。若要更新 origin 的 refs，使用 git fetch origin；"
            "若要查詢分支名稱，使用 git ls-remote --heads origin。"
            "不會自動改成其他分支或重試。"
        )
    if (exit_code == 128 and re.match(r"(?i)^git(?:\.exe)?\s+fetch\b", command)
            and re.search(r"(?i)could not resolve host:", stderr)):
        return (
            "GIT_DNS_UNRESOLVED：Git 子程序無法解析遠端主機。"
            "請在相同 Telegram CMD 執行環境比較 where git、where curl.exe、"
            "nslookup github.com；若一般 CMD 可用，優先檢查 PATH 和子程序環境。"
            "未自動修改 DNS、Git remote 或重試。"
        )
    return ""


class CmdControlError(ValueError):
    pass


@dataclass(frozen=True)
class CmdControlPolicy:
    enabled: bool
    user_ids: frozenset[int]
    chat_ids: frozenset[int]

    def assert_authorized(self, user_id: int, chat_id: int) -> None:
        if not self.enabled:
            raise CmdControlError("TG_CMD_DISABLED")
        if not self.user_ids or not self.chat_ids or user_id not in self.user_ids or chat_id not in self.chat_ids:
            raise CmdControlError("TG_CMD_ADMIN_NOT_AUTHORIZED")


def is_direct_cmd_request(text: str) -> bool:
    return bool(re.match(r"^run cmd by tg\s*(?:\r?\n|$)", str(text or "").lstrip(), re.I))


def render_cmd_template(smartagent_root: str | Path) -> str:
    return (
        "執行cmd（固定軟體流程，不經 WebGPT）\n"
        "請複製以下格式、修改後回傳：\n\n"
        "run cmd by TG\n"
        f'cmd path: "{Path(smartagent_root).resolve()}"\n'
        "command:\n"
        '"echo hello"\n'
        '"echo time"\n\n'
        "每行分別執行並回傳 Exit Code、stdout、stderr。"
    )


def parse_cmd_request(text: str) -> tuple[Path, tuple[str, ...]]:
    value = str(text or "").strip()
    if len(value) > 8192:
        raise CmdControlError("TG_CMD_REQUEST_TOO_LARGE")
    rows = value.splitlines()
    if len(rows) < 4 or rows[0].strip().casefold() != HEADER.casefold():
        raise CmdControlError("TG_CMD_TEMPLATE_INVALID")
    path_match = re.fullmatch(r'cmd path\s*:\s*"([^"\r\n]+)"\s*', rows[1].strip(), re.I)
    if not path_match or not re.fullmatch(r"command\s*:", rows[2].strip(), re.I):
        raise CmdControlError("TG_CMD_TEMPLATE_FIELDS_INVALID")
    folder = Path(path_match.group(1)).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise CmdControlError("TG_CMD_WORKSPACE_INVALID")
    commands = []
    for raw in rows[3:]:
        cmd = raw.strip()
        if not cmd:
            continue
        # A pasted second template or a glued header must never be executed
        # as CMD. Reject the entire batch before any command starts.
        if (re.search(r"(?i)run\s+cmd\s+by\s+tg", cmd)
                or re.match(r"(?i)^(?:cmd\s+path|command)\s*:", cmd)):
            raise CmdControlError("TG_CMD_DUPLICATE_TEMPLATE_OR_GLUED_HEADER")
        if len(cmd) >= 2 and cmd[0] == cmd[-1] == '"':
            cmd = cmd[1:-1]
        if not cmd or len(cmd) > 600 or "\x00" in cmd:
            raise CmdControlError("TG_CMD_LINE_INVALID")
        commands.append(cmd)
    if not 1 <= len(commands) <= MAX_LINES:
        raise CmdControlError("TG_CMD_COMMAND_COUNT_INVALID")
    return folder.resolve(), tuple(commands)


def execute_cmd_request(
    text: str, *, user_id: int, chat_id: int,
    policy: CmdControlPolicy | None = None, runner=None,
    trusted_install_root: str | Path | None = None,
    command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    batch_timeout_seconds: int = DEFAULT_BATCH_TIMEOUT_SECONDS,
) -> dict:
    """Sequential bounded cmd.exe invocation through the existing OS executor."""
    import time
    import uuid
    from agent_core.command_security import inspect_command
    from agent_core.path_security import is_within, reject_reparse_components
    from agent_core.workspace_access import access_snapshot
    from agent_core.windows_security import restricted_executor_required
    from agent_core.bounded_process import run_bounded_process

    # The dispatcher constructs this transient policy only after the normal
    # Telegram private-chat identity / pairing authorization. No second
    # JSON file, extra IDs or environment-variable opt-in is required.
    if policy is None:
        raise CmdControlError("TG_CMD_RECEIVER_AUTH_REQUIRED")
    policy.assert_authorized(user_id, chat_id)
    if os.name != "nt":
        raise CmdControlError("TG_CMD_WINDOWS_REQUIRED")
    # Match the installation's effective ACL mode. ACL ON is enforced by
    # bounded_process via the OS Restricted Executor. ACL OFF uses the same
    # bounded_process with the existing Software Guard command admission.
    # A manually submitted Telegram command never changes ACL mode.
    use_restricted_executor = restricted_executor_required()
    command_timeout_seconds = _bounded_timeout(
        command_timeout_seconds, minimum=MIN_COMMAND_TIMEOUT_SECONDS,
        maximum=MAX_COMMAND_TIMEOUT_SECONDS,
    )
    batch_timeout_seconds = _bounded_timeout(
        batch_timeout_seconds, minimum=command_timeout_seconds,
        maximum=MAX_BATCH_TIMEOUT_SECONDS,
    )
    cwd, commands = parse_cmd_request(text)
    roots = tuple(Path(p).resolve() for p in access_snapshot().get("writable_workspaces", ()))
    root = next((p for p in roots if is_within(cwd, p)), None)
    # The callback template defaults to the SmartAgent installation directory.
    # Permit exactly that trusted host-owned cwd for the authenticated admin
    # control; it is a working directory, not a new general Workspace grant.
    # Arbitrary user-supplied cmd paths still require the workspace allowlist.
    if root is None and trusted_install_root is not None:
        installed = Path(trusted_install_root).expanduser().resolve()
        if cwd == installed:
            root = installed
    if root is None:
        raise CmdControlError("TG_CMD_WORKSPACE_NOT_AUTHORIZED")
    reject_reparse_components(cwd, root)

    # Verify the entire batch before the first process starts.
    for command in commands:
        admission = inspect_command(command, workspace=cwd)
        if not admission.allowed:
            raise CmdControlError("TG_CMD_COMMAND_REJECTED:" + admission.code)

    executor = runner or run_bounded_process
    exe = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe")
    started = time.monotonic()
    tracking = "TG-CMD-" + uuid.uuid4().hex[:12].upper()
    lines = [
        f"CMD 執行結果 {tracking}",
        f"cmd path: {cwd}",
        "執行模式：" + (
            "ACL ON / Restricted Executor"
            if use_restricted_executor else "ACL OFF / Software Guard"
        ),
        (f"逐筆同步等待；單筆上限 {command_timeout_seconds}s，"
         f"整批上限 {batch_timeout_seconds}s；無人值守 stdin=NUL。"),
    ]
    results = []
    for index, command in enumerate(commands, 1):
        remaining = batch_timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            lines.append("總執行期限已到，後續命令不執行。")
            break
        # Exactly one interactive-pause builtin can be skipped without
        # executing anything. Do not treat set /p, choice, credential prompts
        # or arbitrary confirmation text as PAUSE.
        if command.casefold() in {"pause", "@pause"}:
            results.append({
                "line": index, "exit_code": 0, "status": "SUCCESS",
                "skipped_pause": True,
            })
            lines.extend(["", f"[{index}/{len(commands)}] > {command}",
                          "Exit Code: 0", "stdout:\nSKIPPED_PAUSE (TG noninteractive)"])
            continue
        info = executor(
            [exe, "/d", "/s", "/c", command],
            cwd=cwd, shell=False,
            timeout=min(command_timeout_seconds, max(1, int(remaining))),
            max_stream_bytes=10000,
            security_command=command,
            noninteractive=True,
        )
        passed = (
            info.get("process_started") is True and info.get("exit_code") == 0
            and not info.get("timed_out") and not info.get("security_rejected")
        )
        results.append({"line": index, "exit_code": info.get("exit_code"),
                        "status": "SUCCESS" if passed else "FAILED"})
        lines.extend([
            "", f"[{index}/{len(commands)}] > {command}",
            "Exit Code: " + str(info.get("exit_code")),
            "stdout:\n" + str(info.get("stdout") or "(empty)")[:10000],
        ])
        if info.get("stderr"):
            lines.append("stderr:\n" + str(info["stderr"])[:10000])
        if info.get("error"):
            lines.append("error: " + str(info["error"])[:1000])
        hint = _command_failure_hint(
            command, str(info.get("stderr") or ""), info.get("exit_code"),
        )
        if hint:
            lines.append("診斷：" + hint)
        if not passed:
            if info.get("timed_out"):
                lines.append("TIMED_OUT：此筆命令逾時；後續命令不執行。")
            elif info.get("exit_code") is None:
                lines.append("UNKNOWN：無法確認命令結果；後續命令不執行。")
            lines.append("指令未完成，停止執行後續行。")
            break
    success = len(results) == len(commands) and all(
        x["status"] == "SUCCESS" for x in results
    )
    lines.append("\n狀態：" + ("SUCCESS" if success else "FAILED/PARTIAL"))
    reply = "\n".join(lines)
    if len(reply) > 24000:
        reply = reply[:24000] + "\n...[回覆過長，後段省略]"
    return {"message": reply, "status": "SUCCESS" if success else "FAILED",
            "control_id": tracking, "results": results}
