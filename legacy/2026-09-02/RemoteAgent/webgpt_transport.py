#!/usr/bin/env python3
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any

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
    @staticmethod
    def _conversation_identity(url:str)->str:
        match=re.search(r"/c/([0-9a-zA-Z-]+)",str(url or ""))
        return match.group(1).lower() if match else str(url or "")
    @classmethod
    def _same_conversation(cls,left:str,right:str)->bool:
        return bool(left and right) and cls._conversation_identity(left)==cls._conversation_identity(right)
    def check_health(self,conversation_url:str)->str:
        try:
            page=self._page(); current=str(getattr(page,"url","") or "")
            if not current.startswith("https://chatgpt.com/"): return UNAVAILABLE
            if conversation_url and not self._same_conversation(current,str(conversation_url)): return STALE
            page.locator("body").count(); return CONNECTED
        except Exception: return UNAVAILABLE
    def open_or_reconcile(self,conversation_url:str)->str:
        try:
            self.scraper.navigate_to_conversation(str(conversation_url)); page=self._page()
            return CONNECTED if self._same_conversation(str(getattr(page,"url","") or ""),str(conversation_url)) else STALE
        except Exception: return UNAVAILABLE
    def read_new_turns(self,cursor:int,*,role:str="assistant")->list[TransportTurn]:
        nodes=self._page().locator(f'[data-message-author-role="{role}"]'); count=nodes.count(); start=max(0,int(cursor or 0))
        if start>count: return []
        out=[]
        for index in range(start,count):
            try: text=nodes.nth(index).inner_text().strip()
            except Exception: continue
            out.append(TransportTurn(index,text,str(role)))
        return out
    def current_turn_count(self,*,role:str="assistant")->int:
        return int(self._page().locator(f'[data-message-author-role="{role}"]').count())
    def deliver_event(self,route:dict,event:dict)->dict:
        target=str((route or {}).get("conversation_url") or "")
        if not target: return {"delivered":False,"reason":"missing_conversation_url"}
        health=self.open_or_reconcile(target)
        if health!=CONNECTED: return {"delivered":False,"reason":"transport_unavailable","health":health}
        text=str((event or {}).get("rendered_text") or "").strip()
        if not text: return {"delivered":False,"reason":"missing_rendered_text"}
        try: return {"delivered":True,"reply":str(self.scraper.ask(text,new_conversation=False) or ""),"health":CONNECTED}
        except Exception as exc: return {"delivered":False,"reason":f"{type(exc).__name__}: {exc}","health":UNAVAILABLE}
