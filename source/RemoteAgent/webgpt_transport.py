#!/usr/bin/env python3
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

from agent_core.conversation_identity import conversation_id, same_conversation
from agent_core.web_ui import create_web_ui_for_page, provider_from_url

CONNECTED="CONNECTED"; STALE="STALE"; RECONCILING="RECONCILING"; UNAVAILABLE="UNAVAILABLE"

@dataclass(frozen=True)
class TransportTurn:
    turn_index:int
    text:str
    role:str="assistant"

class WebGPTTransportAdapter:
    """WebGPT UI adapter. Browser/DOM details stay here; no private ChatGPT API."""
    def __init__(self,scraper:Any): self.scraper=scraper
    def _page(self):
        page=getattr(self.scraper,"_page",None)
        if page is None: raise RuntimeError("webgpt_page_unavailable")
        return page
    def _web_ui(self):
        getter=getattr(self.scraper,"_web_ui_adapter",None)
        if callable(getter): return getter()
        return create_web_ui_for_page(self._page())
    @staticmethod
    def _conversation_identity(url:str)->str:
        return conversation_id(url)
    @classmethod
    def _same_conversation(cls,left:str,right:str)->bool:
        return same_conversation(left,right)
    def check_health(self,conversation_url:str)->str:
        try:
            page=self._page(); current=str(getattr(page,"url","") or "")
            if provider_from_url(current) != provider_from_url(conversation_url or current): return UNAVAILABLE
            if conversation_url and not self._same_conversation(current,str(conversation_url)): return STALE
            return CONNECTED if self._web_ui().page_reachable() else UNAVAILABLE
        except Exception: return UNAVAILABLE
    def open_or_reconcile(self,conversation_url:str)->str:
        from agent_core.remote_binding import active
        binding = active()
        if binding and not self._same_conversation(binding['gpt_url'], conversation_url):
            return UNAVAILABLE
        try:
            self.scraper.navigate_to_conversation(str(conversation_url)); page=self._page()
            return CONNECTED if self._same_conversation(str(getattr(page,"url","") or ""),str(conversation_url)) else STALE
        except Exception: return UNAVAILABLE
    def read_new_turns(self,cursor:int,*,role:str="assistant")->list[TransportTurn]:
        turns=self._web_ui().observation_turns(role); count=len(turns); start=max(0,int(cursor or 0))
        if start>count: return []
        out=[]
        for index in range(start,count):
            text=turns[index].raw_text.strip()
            out.append(TransportTurn(index,text,str(role)))
        return out
    def current_turn_count(self,*,role:str="assistant")->int:
        return len(self._web_ui().observation_turns(role))
    def deliver_event(self,route:dict,event:dict)->dict:
        target=str((route or {}).get("conversation_url") or "")
        if not target: return {"delivered":False,"reason":"missing_conversation_url"}
        health=self.open_or_reconcile(target)
        if health!=CONNECTED: return {"delivered":False,"reason":"transport_unavailable","health":health}
        text=str((event or {}).get("rendered_text") or "").strip()
        if not text: return {"delivered":False,"reason":"missing_rendered_text"}
        try: return {"delivered":True,"reply":str(self.scraper.ask(text,new_conversation=False) or ""),"health":CONNECTED}
        except Exception as exc: return {"delivered":False,"reason":f"{type(exc).__name__}: {exc}","health":UNAVAILABLE}
