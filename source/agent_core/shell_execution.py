"""Explicit Windows shell launch contract.  Not an authorization grant."""
from __future__ import annotations
import os
import re
from collections.abc import Mapping

SHELL_MODES = frozenset({"POWERSHELL", "CMD", "DIRECT"})

class ShellModeError(ValueError):
    pass

def execution_contract(action: Mapping) -> dict:
    mode = action.get("shell_mode", "POWERSHELL")
    if not isinstance(mode, str) or mode not in SHELL_MODES:
        raise ShellModeError("shell_mode_invalid:POWERSHELL|CMD|DIRECT")
    command = action.get("command")
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise ShellModeError("command_invalid")
    arguments = action.get("argv")
    if mode != "DIRECT":
        if arguments is not None:
            raise ShellModeError("argv_only_allowed_for_DIRECT")
        if mode == "CMD" and (len(command) > 8192 or "\r" in command or "\n" in command):
            raise ShellModeError("CMD_command_invalid")
        return {
            "shell_mode":mode, "policy_command":command,
            "executable":"", "argv":[],
            "launch_argv":["cmd.exe","/d","/s","/c",command] if mode=="CMD" else [],
            "launch_form":"CMD_EXPLICIT" if mode=="CMD" else "POWERSHELL_LEGACY"
        }
    if not isinstance(arguments, list) or len(arguments)>64:
        raise ShellModeError("DIRECT_argv_must_be_list_max64")
    if not (re.fullmatch(r"[A-Za-z]:[\\/][^\x00\r\n]+[.]exe", command, re.I)
            or (os.name!="nt" and command.startswith("/") and os.path.isabs(command))):
        raise ShellModeError("DIRECT_executable_absolute_file_required")
    if any(not isinstance(arg,str) or len(arg)>2048 or any(c in arg for c in ("\x00","\r","\n")) for arg in arguments):
        raise ShellModeError("DIRECT_argv_invalid")
    def quote(token):
        return token if re.fullmatch(r"[A-Za-z0-9_./:@%-]+",token) else "'" + token.replace("'","''") + "'"
    policy="& '" + command.replace("'","''") + "'"
    if arguments:
        policy+=" "+" ".join(quote(arg) for arg in arguments)
    if len(policy)>8192:
        raise ShellModeError("DIRECT_policy_command_too_long")
    return {
        "shell_mode":mode, "policy_command":policy,
        "executable":command, "argv":list(arguments),
        "launch_argv":[command,*arguments], "launch_form":"DIRECT_EXECUTABLE_ARGV",
    }

def effective_action_for_policy(action:Mapping)->dict:
    updated=dict(action)
    updated["command"]=execution_contract(action)["policy_command"]
    return updated
