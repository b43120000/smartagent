#!/usr/bin/env python3
"""Strict isolated transport for the Agent 2 self-repair brain."""
from __future__ import annotations
import json,re
from pathlib import PurePosixPath
from typing import Any

PROTOCOL="self_repair"; VERSION=1; FENCE="self_repair_control"
REQUEST="SELF_REPAIR_REQUEST"; PLAN="SELF_REPAIR_PLAN"; FINAL="SELF_REPAIR_FINAL"
TYPES={REQUEST,PLAN,FINAL}; MAX_BYTES=64*1024
DEFAULT_REPAIR_URL="https://chatgpt.com/c/configure-your-repair-conversation"
ALLOWED_PREFIXES=("agent_core/","tests/")
FORBIDDEN=("launch_smart_agent.bat","install_smart_agent",".agents/",".git/","remoteagent/")

def _safe_path(value:str)->bool:
    v=str(value or "").replace("\\","/").lower().lstrip("./")
    if not v or ":" in v or v.startswith("/") or ".." in PurePosixPath(v).parts:return False
    if any(v==x or v.startswith(x) for x in FORBIDDEN):return False
    if v == "smart_agent.py":return True
    if v.startswith("tests/"):return PurePosixPath(v).name.startswith("validate_") and v.endswith(".py")
    return v.startswith("agent_core/") and v.endswith(".py")

def validate(message:Any)->tuple[bool,str]:
    if not isinstance(message,dict):return False,"not_object"
    if message.get("protocol")!=PROTOCOL:return False,"wrong_protocol"
    if message.get("protocol_version")!=VERSION:return False,"wrong_version"
    if message.get("type") not in TYPES:return False,"wrong_type"
    if not str(message.get("issue_id","")).startswith("ISSUE-"):return False,"invalid_issue_id"
    if message["type"] in {PLAN,FINAL}:
        paths=message.get("modified_paths",message.get("allowed_paths",[]))
        if not isinstance(paths,list) or any(not _safe_path(p) for p in paths):return False,"path_out_of_scope"
        inspect_paths=message.get("inspect_paths",[])
        if not isinstance(inspect_paths,list):return False,"inspect_paths_not_list"
        if len(inspect_paths)>4:return False,"inspect_paths_too_many"
        if any(not _safe_path(p) for p in inspect_paths):return False,"inspect_path_out_of_scope"
        files=message.get("files",[])
        if not isinstance(files,list):return False,"files_not_list"
        file_paths=[]
        for item in files:
            if not isinstance(item,dict):return False,"invalid_file_entry"
            path=str(item.get("path","") or "")
            if not _safe_path(path):return False,"path_out_of_scope"
            if not isinstance(item.get("content"),str):return False,"invalid_file_content"
            file_paths.append(path.replace("\\","/"))
        norm=[str(x).replace("\\","/") for x in paths]
        if message["type"]==PLAN and sorted(file_paths)!=sorted(norm):return False,"files_modified_paths_mismatch"
        if message["type"]==FINAL and files and sorted(file_paths)!=sorted(norm):return False,"files_modified_paths_mismatch"
    if message["type"]==FINAL and message.get("validation_status") not in {"PASS","FAIL"}:return False,"missing_validation_status"
    return True,"ok"

def parse(text:str)->tuple[dict|None,str]:
    raw=str(text or "").strip()
    m=re.fullmatch(r"```self_repair_control\s*\r?\n([\s\S]*?)\r?\n```",raw,re.I)
    if m:
        body=m.group(1).strip()
    else:
        # ChatGPT's rendered DOM omits the literal Markdown backticks while
        # preserving the code-block language label and body.  Accept only that
        # exact, exclusive rendering; surrounding prose remains forbidden.
        rendered=re.fullmatch(r"self_repair_control\s*\r?\n(\{[\s\S]*\})",raw,re.I)
        if not rendered:return None,"exclusive_fence_required"
        body=rendered.group(1).strip()
    if len(body.encode())>MAX_BYTES:return None,"too_large"
    try:data=json.loads(body)
    except Exception:return None,"invalid_json"
    ok,reason=validate(data);return (data,"ok") if ok else (None,reason)

def format_message(message:dict)->str:
    ok,reason=validate(message)
    if not ok:raise ValueError(reason)
    return f"```{FENCE}\n{json.dumps(message,ensure_ascii=False,separators=(',',':'))}\n```"
