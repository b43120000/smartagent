#!/usr/bin/env python3
from __future__ import annotations
from typing import Any
from RemoteAgent.webgpt_transport import WebGPTTransportAdapter

class WebGPTDeliveryAdapter:
    """Render transport-neutral RemoteEvents into WebGPT-facing natural language."""
    def __init__(self,transport:WebGPTTransportAdapter): self.transport=transport
    @staticmethod
    def render(event:dict,route:dict|None=None)->str:
        et=str(event.get('event_type','')); eid=str(event.get('event_id','')); rid=str(event.get('request_id','')); tid=str(event.get('task_id','')); status=str(event.get('status',''))
        payload=dict(event.get('payload') or {})
        marker=f"event_id={eid}"
        transport=str((route or {}).get('transport','')).upper()
        label='WebCopilot Agent1_n' if transport=='WEBGPT_COPILOT' else 'RemoteAgent'
        if et=='TASK_ACCEPTED': return f"[REMOTE_AGENT_ACK]\n{marker} event=TASK_ACCEPTED request_id={rid} task_id={tid} status={status}\n{label} 已接受任務。"
        if et=='TASK_STARTED': return f"[REMOTE_AGENT_ACK]\n{marker} event=TASK_STARTED request_id={rid} task_id={tid} status={status}\n{label} 已開始執行。"
        if et=='TASK_COMPLETED': return f"[REMOTE_AGENT_RESULT]\n{marker} request_id={rid} task_id={tid} status={status}\nsummary={str(payload.get('summary',''))}"
        if et=='TASK_FAILED': return f"[REMOTE_AGENT_RESULT]\n{marker} request_id={rid} task_id={tid} status={status}\nsummary={str(payload.get('error',''))}"
        if et=='TASK_INTERRUPTED': return f"[REMOTE_AGENT_RESULT]\n{marker} request_id={rid} task_id={tid} status={status}\nsummary={label} 任務中斷，v1 不會自動重跑。"
        return f"{label} {marker} event={et} request_id={rid} task_id={tid} status={status}。"
    def deliver_event(self,route:dict,event:dict)->dict:
        wire=dict(event); wire['rendered_text']=self.render(event,route)
        return self.transport.deliver_event(route,wire)
    def reconcile_event(self,route:dict,event:dict)->dict:
        target=str((route or {}).get('conversation_url') or '')
        if not target: return {'delivered':False,'reason':'missing_conversation_url','uncertain':True}
        health=self.transport.open_or_reconcile(target)
        if health!='CONNECTED': return {'delivered':False,'reason':'transport_unavailable','health':health,'uncertain':True}
        marker=f"event_id={str(event.get('event_id',''))}"
        observed=False
        for attempt in range(5):
            observed=any(marker in turn.text for turn in self.transport.read_new_turns(0,role='user'))
            if observed: break
            if attempt<4:
                page=getattr(getattr(self.transport,'scraper',None),'_page',None)
                if page is not None and hasattr(page,'wait_for_timeout'): page.wait_for_timeout(300)
        return {'delivered':observed,'definitive_not_delivered':not observed,'health':health,'reconciled':True}
