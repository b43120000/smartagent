#!/usr/bin/env python3
from __future__ import annotations
import hashlib, os, re, time
from pathlib import Path
from agent_core.transport_message import NormalizedInboundMessage
from agent_core.webgpt_outbound_ledger import is_recorded_outbound_turn
from RemoteAgent.remote_protocol import REMOTE_AGENT_REQUEST, analyze_remote_transport

CURSOR_NAME="remote_agent_assistant_turn_count_v1"
USER_PAIR_CURSOR_NAME="remote_agent_user_pair_count_v2"
ASSISTANT_FINGERPRINT_CURSOR_NAME="remote_agent_assistant_last_fingerprint_v2"
USER_PAIR_FINGERPRINT_CURSOR_NAME="remote_agent_user_pair_last_fingerprint_v3"

_LOCAL_CONTROL_PREFIXES=(
    "[REMOTE_AGENT_ACK]", "[REMOTE_AGENT_EVENT]", "[REMOTE_AGENT_RESULT]",
    "[SMARTAGENT", "[AGENT_PROTOCOL_", "[AGENT_SESSION_",
    "[WEBAGENT_REQUEST_SOURCE]", "[WEBAGENT_USER_REQUEST]",
    "[WEBAGENT_TOOL_RESULTS]", "[WEBAGENT_PROTOCOL_REJECTED]",
    "[WEBAGENT_VERIFICATION_GATE]",
    "RUN_ID=", "RESULT_ID=",
)
_INTERNAL_AGENT_MARKERS=(
    "[AGENT_PROTOCOL_", "[AGENT_SESSION_",
    "[SMARTAGENT_V8_LOCAL_COMMIT]", "[SMARTAGENT_V8_REQUIRED]",
    "[SMARTAGENT_V8_RESPONSE_REPAIR]", "[SMARTAGENT_V8_ACTION_REPLAN]",
)

_INTERNAL_BLOCK_MARKERS=(
    ("[WEBAGENT_TOOL_RESULTS]", "[/WEBAGENT_TOOL_RESULTS]"),
    ("[WEBAGENT_REQUEST_TRACE]", "[/WEBAGENT_REQUEST_TRACE]"),
    ("[SMARTAGENT_RESULT_ATTACHMENT]", "[/SMARTAGENT_RESULT_ATTACHMENT]"),
)
_ATTACHMENT_UI_LABELS={"檔案", "文件", "file", "attachment", "顯示更多", "显示更多", "show more"}
_ATTACHMENT_NAME_RE=re.compile(r"^[^\r\n\\/]+\.(?:json|md|txt|csv|log|zip|pdf|docx?|xlsx?)$",re.IGNORECASE)


def _protocol_lines(text:str)->list[str]:
    value=str(text or "").replace("\ufeff", "").replace("\u200b", "")
    return [line.strip() for line in value.replace("\r", "\n").split("\n")]


def _has_complete_internal_envelope(text:str)->bool:
    """Recognize locally generated envelopes even after attachment UI prefixes.

    Matching whole marker lines and balanced blocks avoids treating ordinary
    human prose that merely mentions a protocol token as an internal turn.
    """
    lines=_protocol_lines(text)
    tokens=set(lines)
    marker_tokens={item for pair in _INTERNAL_BLOCK_MARKERS for item in pair}|{
        "[REMOTE_AGENT_ACK]", "[REMOTE_AGENT_EVENT]", "[REMOTE_AGENT_RESULT]",
    }
    marker_indexes=[index for index,line in enumerate(lines) if line in marker_tokens]
    if not marker_indexes:
        return False
    prelude=[line for line in lines[:min(marker_indexes)] if line]
    if prelude and not all(
        line.casefold() in _ATTACHMENT_UI_LABELS or _ATTACHMENT_NAME_RE.fullmatch(line)
        for line in prelude
    ):
        # A human may paste a complete protocol block while asking for help.
        # Only an empty prelude or browser attachment chrome is safe to suppress.
        return False
    if any(start in tokens and end in tokens for start,end in _INTERNAL_BLOCK_MARKERS):
        return True
    if "[REMOTE_AGENT_ACK]" in tokens or "[REMOTE_AGENT_EVENT]" in tokens:
        joined="\n".join(lines)
        return bool(re.search(r"(?m)^(?:event_id|request_id|task_id|event|status)=", joined))
    if "[REMOTE_AGENT_RESULT]" in tokens:
        joined="\n".join(lines)
        return "request_id=" in joined and ("status=" in joined or "summary=" in joined)
    return False

def is_internal_agent_turn(text:str)->bool:
    """Identify Agent-authored WebGPT turns that must never become ingress."""
    value=str(text or "").lstrip()
    return (
        not value
        or value.startswith(_LOCAL_CONTROL_PREFIXES)
        or any(marker in value for marker in _INTERNAL_AGENT_MARKERS)
        or _has_complete_internal_envelope(value)
    )

def turn_identity(url:str,index:int,role:str="assistant")->str:
    raw=f"WEBGPT|chatgpt.com|{url}|{role}|{int(index)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

class RemoteIngressObserver:
    def __init__(self,*,registry,task_queue,adapter,workspace:str,conversation_url:str,event_store=None,runtime_log=None,ingress_gateway=None):
        self.registry=registry; self.task_queue=task_queue; self.adapter=adapter; self.event_store=event_store
        self.runtime_log=runtime_log
        self.ingress_gateway=ingress_gateway
        self.workspace=str(workspace); self.conversation_url=str(conversation_url)
    def _log(self,event:str,**detail)->None:
        if self.runtime_log is not None:
            self.runtime_log.write(event,component="ingress",conversation_url=self.conversation_url,**detail)
    def _cursor(self,name:str=CURSOR_NAME)->int:
        raw=self.registry.get_watch_cursor(self.conversation_url,name)
        try: return max(0,int(raw or 0))
        except ValueError: return 0
    def _ack(self,value:int,name:str=CURSOR_NAME)->None:
        self.registry.set_watch_cursor(self.conversation_url,name,str(int(value)))
    @staticmethod
    def _content_fingerprint(*values:str)->str:
        raw="\n---TURN---\n".join(str(value or "") for value in values)
        return hashlib.sha256(raw.encode("utf-8",errors="replace")).hexdigest()
    def _ack_fingerprint(self,name:str,*values:str)->None:
        self.registry.set_watch_cursor(
            self.conversation_url,name,self._content_fingerprint(*values)
        )
    def baseline(self)->int:
        current=self._cursor()
        if current: return current
        count=self.adapter.current_turn_count(role="assistant"); self._ack(count)
        if count:
            latest=self._turn_at(count-1,"assistant")
            if latest is not None:
                self._ack_fingerprint(ASSISTANT_FINGERPRINT_CURSOR_NAME,latest.text)
        return count
    def _turn_at(self,index:int,role:str):
        for turn in self.adapter.read_new_turns(index,role=role):
            if int(turn.turn_index)==int(index): return turn
            break
        return None
    @staticmethod
    def _is_local_control_turn(text:str)->bool:
        return is_internal_agent_turn(text)
    def _enqueue(self,request:dict,*,turn_index:int,identity_role:str,identity_discriminator:str=""):
        record=self.registry.find_by_url(self.conversation_url) or {}
        workspace=str(record.get("active_workspace") or self.workspace)
        if not workspace or not Path(workspace).is_dir():
            self._log("ERROR",stage="AUTHORIZATION",turn_index=turn_index,error="authorized_workspace_unavailable")
            raise ValueError("authorized_workspace_unavailable")
        enriched=dict(request); enriched["workspace"]=str(Path(workspace).resolve()); enriched["conversation_url"]=self.conversation_url
        enriched["transport"]="WEBGPT"; enriched["endpoint"]="chatgpt.com"
        identity=turn_identity(
            self.conversation_url,turn_index,
            identity_role + str(identity_discriminator or ""),
        )
        if self.ingress_gateway is not None:
            accepted=self.ingress_gateway.accept(
                NormalizedInboundMessage(
                    transport="WEBGPT",
                    endpoint="chatgpt.com",
                    conversation_key=self.conversation_url,
                    sender_id=identity_role,
                    source_message_id=f"{identity_role}:{int(turn_index)}{identity_discriminator}",
                    text=str(request.get("request","") or ""),
                    received_at=time.time(),
                    idempotency_key=identity,
                    reply_context={"conversation_url":self.conversation_url},
                    metadata={
                        "request_id":str(request.get("request_id","") or ""),
                        "origin_turn_index":int(turn_index),
                        "origin_role":identity_role,
                    },
                ),
                workspace=workspace,
            )
            return accepted.task,accepted.created
        route={"transport":"WEBGPT","endpoint":"chatgpt.com","conversation_url":self.conversation_url}
        task,is_new=self.task_queue.enqueue_remote_request(
            enriched,origin_turn_fingerprint=identity,
            metadata={"transport":"WEBGPT","reply_route":route,"origin_turn_index":turn_index,"origin_role":identity_role},
        )
        if is_new:
            if self.event_store is not None:
                self.event_store.emit('TASK_ACCEPTED',task,status='QUEUED',payload={})
            self._log("TASK_ACCEPTED",turn_index=turn_index,source=identity_role,request_id=getattr(task,"request_id",request.get("request_id","")),task_id=getattr(task,"task_id",""))
        return task,is_new
    def _poll_user_fallback(self)->list:
        """Use a paired human turn only when its assistant emitted no control.

        A conversation must already be explicitly remote_enabled before this
        observer exists.  ACK/result/SmartAgent traffic is excluded so delivery
        messages can never loop back into the task queue.
        """
        user_count=self.adapter.current_turn_count(role="user")
        assistant_count=self.adapter.current_turn_count(role="assistant")
        pair_count=min(user_count,assistant_count)
        raw_cursor=self.registry.get_watch_cursor(self.conversation_url,USER_PAIR_CURSOR_NAME)
        pair_identity_discriminator=""
        if str(raw_cursor or "").strip():
            cursor=self._cursor(USER_PAIR_CURSOR_NAME)
            if cursor>pair_count:
                rebased=max(0,pair_count-1)
                self._log("RECONNECT",stage="USER_CURSOR_REBASED",old_cursor=cursor,new_cursor=rebased,turn_count=pair_count)
                cursor=rebased
            elif cursor>=pair_count and pair_count:
                latest_user=self._turn_at(pair_count-1,"user")
                latest_assistant=self._turn_at(pair_count-1,"assistant")
                stored_pair_fp=self.registry.get_watch_cursor(
                    self.conversation_url,USER_PAIR_FINGERPRINT_CURSOR_NAME
                )
                current_pair_fp=self._content_fingerprint(
                    latest_user.text if latest_user is not None else "",
                    latest_assistant.text if latest_assistant is not None else "",
                )
                if not stored_pair_fp or stored_pair_fp!=current_pair_fp:
                    rebased=max(0,pair_count-1)
                    self._log("RECONNECT",stage="USER_BRANCH_REPLACED",old_cursor=cursor,new_cursor=rebased,turn_count=pair_count)
                    cursor=rebased
        else:
            # Upgrade recovery: inspect only the newest completed pair, never
            # replay the full history of an already-linked conversation.
            cursor=max(0,pair_count-1)
        created=[]
        for index in range(cursor,pair_count):
            user_turn=self._turn_at(index,"user")
            assistant_turn=self._turn_at(index,"assistant")
            if user_turn is None or assistant_turn is None: break
            if is_recorded_outbound_turn(self.conversation_url,index,user_turn.text):
                self._log("POLL",stage="LOCAL_OUTBOUND_ECHO_IGNORED",turn_index=index)
                self._ack(index+1,USER_PAIR_CURSOR_NAME)
                self._ack_fingerprint(USER_PAIR_FINGERPRINT_CURSOR_NAME,user_turn.text,assistant_turn.text)
                continue
            report=analyze_remote_transport(assistant_turn.text)
            if report.get("diagnostics"):
                self._log("ERROR",stage="USER_FALLBACK_PARSER",turn_index=index,diagnostics=report.get("diagnostics"))
                break
            control_requests=[m for m in report.get("messages",[]) if m.get("type")==REMOTE_AGENT_REQUEST]
            if control_requests or self._is_local_control_turn(user_turn.text):
                self._ack(index+1,USER_PAIR_CURSOR_NAME)
                self._ack_fingerprint(USER_PAIR_FINGERPRINT_CURSOR_NAME,user_turn.text,assistant_turn.text)
                continue
            request_text=str(user_turn.text or "").strip()
            request_id="RR-USER-"+hashlib.sha256(
                f"{self.conversation_url}|{index}|{request_text}".encode("utf-8",errors="replace")
            ).hexdigest()[:16].upper()
            self._log("REQUEST_DETECTED",turn_index=index,request_count=1,source="user_fallback")
            # Identity is derived only from the inbound user turn.  A changed
            # paired assistant reply (for example our own ACK/result delivery)
            # must never turn the same user message into a new task.
            pair_identity_discriminator=":content:"+self._content_fingerprint(request_text)[:16]
            task,is_new=self._enqueue(
                {"type":REMOTE_AGENT_REQUEST,"protocol":"remote_agent","protocol_version":1,"request_id":request_id,"request":request_text},
                turn_index=index,identity_role="user_fallback",
                identity_discriminator=pair_identity_discriminator,
            )
            if is_new: created.append(task)
            self._ack(index+1,USER_PAIR_CURSOR_NAME)
            self._ack_fingerprint(USER_PAIR_FINGERPRINT_CURSOR_NAME,user_turn.text,assistant_turn.text)
        return created
    def poll(self)->list:
        cursor=self._cursor(); created=[]
        assistant_identity_discriminator=""
        assistant_count=self.adapter.current_turn_count(role="assistant")
        if cursor>assistant_count:
            # ChatGPT branch changes/reloads can reduce the visible DOM turn
            # count. Inspect only the newest completed assistant turn so the
            # receiver recovers without replaying old conversation history.
            rebased=max(0,assistant_count-1)
            self._log("RECONNECT",stage="ASSISTANT_CURSOR_REBASED",old_cursor=cursor,new_cursor=rebased,turn_count=assistant_count)
            cursor=rebased
            latest=self._turn_at(rebased,"assistant") if assistant_count else None
            if latest is not None:
                assistant_identity_discriminator=":branch:"+self._content_fingerprint(latest.text)[:16]
        elif cursor>=assistant_count and assistant_count:
            latest=self._turn_at(assistant_count-1,"assistant")
            stored_fp=self.registry.get_watch_cursor(
                self.conversation_url,ASSISTANT_FINGERPRINT_CURSOR_NAME
            )
            current_fp=self._content_fingerprint(latest.text if latest is not None else "")
            if not stored_fp or stored_fp!=current_fp:
                rebased=max(0,assistant_count-1)
                self._log("RECONNECT",stage="ASSISTANT_BRANCH_REPLACED",old_cursor=cursor,new_cursor=rebased,turn_count=assistant_count)
                cursor=rebased
                if stored_fp:
                    assistant_identity_discriminator=":branch:"+current_fp[:16]
        for turn in self.adapter.read_new_turns(cursor,role="assistant"):
            report=analyze_remote_transport(turn.text)
            requests=[m for m in report.get("messages",[]) if m.get("type")==REMOTE_AGENT_REQUEST]
            if report.get("diagnostics"):
                self._log("ERROR",stage="PARSER",turn_index=turn.turn_index,diagnostics=report.get("diagnostics"))
                break
            if not requests:
                self._ack(turn.turn_index+1)
                self._ack_fingerprint(ASSISTANT_FINGERPRINT_CURSOR_NAME,turn.text)
                continue
            self._log("REQUEST_DETECTED",turn_index=turn.turn_index,request_count=len(requests))
            for request in requests:
                task,is_new=self._enqueue(
                    request,turn_index=turn.turn_index,identity_role="assistant",
                    identity_discriminator=assistant_identity_discriminator,
                )
                if is_new: created.append(task)
            self._ack(turn.turn_index+1)
            self._ack_fingerprint(ASSISTANT_FINGERPRINT_CURSOR_NAME,turn.text)
        created.extend(self._poll_user_fallback())
        return created
