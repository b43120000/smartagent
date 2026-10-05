#!/usr/bin/env python3
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass

CURSOR_NAME = "webcopilot_user_turn_count"
FINGERPRINT_CURSOR_NAME = "webcopilot_last_user_fingerprint_v2"

@dataclass(frozen=True)
class WebGPTPageSnapshot:
    url: str
    title: str
    conversation_id: str


def conversation_id(url: str) -> str:
    m = re.search(r"/c/([^/?#]+)", str(url or ""))
    return m.group(1) if m else ""


def is_webgpt_page(url: str) -> bool:
    text = str(url or "").lower()
    return text.startswith("https://chatgpt.com/") or text.startswith("https://www.chatgpt.com/")


class WebGPTObserver:
    def __init__(self, page, *, registry=None, gpt_url: str = "", cursor_name: str = CURSOR_NAME):
        self.page = page
        self.registry = registry
        self.gpt_url = str(gpt_url or getattr(page, "url", "") or "")
        self.cursor_name = str(cursor_name or CURSOR_NAME)
        self.cursor = 0
        self.initialized = False

    def snapshot(self) -> WebGPTPageSnapshot:
        url = str(getattr(self.page, "url", "") or "")
        if not is_webgpt_page(url):
            raise RuntimeError(f"not_webgpt_page:{url}")
        try:
            title = str(self.page.title() or "")
        except Exception:
            title = ""
        return WebGPTPageSnapshot(url=url, title=title, conversation_id=conversation_id(url))

    def _load_persisted_cursor(self) -> str:
        if self.registry is None or not self.gpt_url:
            return ""
        return str(self.registry.get_watch_cursor(self.gpt_url, self.cursor_name) or "")

    def _persist_cursor(self) -> None:
        if self.registry is not None and self.gpt_url:
            self.registry.set_watch_cursor(self.gpt_url, self.cursor_name, str(self.cursor))

    @staticmethod
    def _fingerprint(text: str) -> str:
        return hashlib.sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest()

    def _load_persisted_fingerprint(self) -> str:
        if self.registry is None or not self.gpt_url:
            return ""
        return str(self.registry.get_watch_cursor(self.gpt_url, FINGERPRINT_CURSOR_NAME) or "")

    def _persist_fingerprint(self, text: str) -> None:
        if self.registry is not None and self.gpt_url:
            self.registry.set_watch_cursor(
                self.gpt_url, FINGERPRINT_CURSOR_NAME, self._fingerprint(text)
            )

    def baseline(self) -> int:
        messages = self._user_messages()
        stored = self._load_persisted_cursor()
        if stored:
            try:
                self.cursor = max(0, int(stored))
            except ValueError:
                self.cursor = len(messages)
            if self.cursor > len(messages):
                self.cursor = len(messages)
                self._persist_cursor()
            if self.cursor >= len(messages) and messages:
                current_fingerprint = self._fingerprint(messages[-1])
                stored_fingerprint = self._load_persisted_fingerprint()
                if stored_fingerprint and stored_fingerprint != current_fingerprint:
                    self.cursor = len(messages) - 1
                    self._persist_cursor()
                elif not stored_fingerprint:
                    # One-time upgrade recovery from the old count-only
                    # cursor.  Internal protocol turns are historical
                    # handshake traffic; a plain newest turn is the pending
                    # human request that prompted this migration.
                    newest = messages[-1].lstrip()
                    if newest and not newest.startswith(("[AGENT_", "[SMARTAGENT", "[REMOTE_AGENT_")):
                        self.cursor = len(messages) - 1
                        self._persist_cursor()
                    else:
                        self._persist_fingerprint(messages[-1])
        else:
            self.cursor = len(messages)
            self._persist_cursor()
            if messages:
                self._persist_fingerprint(messages[-1])
        self.initialized = True
        return self.cursor

    def poll_new_user_turn(self, prefix: str | None = "webcopilot") -> tuple[int, str] | None:
        """Return the next matching turn without acknowledging that turn.

        Non-matching turns are safe to advance immediately. A matching trigger
        remains at ``cursor`` until the caller durably enqueues it and calls
        ``acknowledge_user_turn``. This preserves enqueue-before-cursor-ACK.
        """
        if not self.initialized:
            self.baseline()
        messages = self._user_messages()
        if len(messages) < self.cursor:
            self.cursor = len(messages)
            self._persist_cursor()
            return None
        if len(messages) == self.cursor and messages:
            stored_fingerprint = self._load_persisted_fingerprint()
            current_fingerprint = self._fingerprint(messages[-1])
            if stored_fingerprint and stored_fingerprint != current_fingerprint:
                self.cursor = len(messages) - 1
                self._persist_cursor()
        pattern = (
            re.compile(rf"(?is)^\s*{re.escape(prefix)}\b")
            if prefix else None
        )
        while self.cursor < len(messages):
            index = self.cursor
            text = messages[index]
            if text and (pattern is None or pattern.match(text)):
                return index, text
            self.cursor += 1
            self._persist_cursor()
        return None

    def acknowledge_user_turn(self, index: int) -> int:
        index = int(index)
        if index != self.cursor:
            raise RuntimeError(f"webgpt_cursor_ack_mismatch:expected={self.cursor}:got={index}")
        self.cursor = index + 1
        self._persist_cursor()
        messages = self._user_messages()
        if 0 <= index < len(messages):
            self._persist_fingerprint(messages[index])
        return self.cursor

    def poll_new_user_message(self, prefix: str = "webcopilot") -> str | None:
        """Compatibility API: observe and immediately acknowledge one trigger."""
        turn = self.poll_new_user_turn(prefix)
        if turn is None:
            return None
        index, text = turn
        self.acknowledge_user_turn(index)
        return text

    def _user_messages(self) -> list[str]:
        out: list[str] = []
        nodes = self.page.locator('[data-message-author-role="user"]')
        for index in range(nodes.count()):
            try:
                text = nodes.nth(index).inner_text().strip()
            except Exception:
                continue
            out.append(text)
        return out


def run_webgpt_observer_self_tests() -> dict[str, bool]:
    class Node:
        def __init__(self, owner, index): self.owner=owner; self.index=index
        def inner_text(self): return self.owner.messages[self.index]
    class Nodes:
        def __init__(self, owner): self.owner=owner
        def count(self): return len(self.owner.messages)
        def nth(self, index): return Node(self.owner,index)
    class Page:
        url="https://chatgpt.com/c/test"
        def __init__(self,messages): self.messages=list(messages)
        def locator(self,_): return Nodes(self)
        def title(self): return "test"
    class Registry:
        def __init__(self): self.values={}
        @property
        def value(self): return self.values.get(CURSOR_NAME,"")
        def get_watch_cursor(self,_url,name): return self.values.get(name,"")
        def set_watch_cursor(self,_url,name,value): self.values[name]=str(value)
    reg=Registry(); page=Page(["old"])
    obs=WebGPTObserver(page,registry=reg,gpt_url=page.url)
    first_baseline=obs.baseline()==1 and reg.value=="1"
    page.messages += ["webcopilot same", "webcopilot same"]
    pending=obs.poll_new_user_turn()
    durable_before_ack=(pending==(1,"webcopilot same") and reg.value=="1" and obs.cursor==1)
    obs.acknowledge_user_turn(1)
    pending2=obs.poll_new_user_turn(); obs.acknowledge_user_turn(2)
    duplicate_text_distinct_turns=(pending2==(2,"webcopilot same") and reg.value=="3")
    page.messages.append("webcopilot after restart")
    obs2=WebGPTObserver(page,registry=reg,gpt_url=page.url)
    restart_cursor=obs2.baseline()==3
    unseen=obs2.poll_new_user_turn()
    restart_resumes_unseen=(unseen==(3,"webcopilot after restart") and reg.value=="3")
    obs2.acknowledge_user_turn(3)
    page.messages[-1]="plain same-length branch replacement"
    branch=obs2.poll_new_user_turn(prefix=None)
    same_length_branch_detected=(branch==(3,"plain same-length branch replacement") and obs2.cursor==3)
    obs2.acknowledge_user_turn(3)
    out={"first_start_baseline":first_baseline,"durable_before_cursor_ack":durable_before_ack,"duplicate_text_distinct_turns":duplicate_text_distinct_turns,"restart_cursor_preserved":restart_cursor,"restart_resumes_unseen":restart_resumes_unseen,"same_length_branch_detected":same_length_branch_detected}
    out["all_passed"]=all(out.values())
    return out

__all__ = ["CURSOR_NAME","FINGERPRINT_CURSOR_NAME","WebGPTObserver","WebGPTPageSnapshot","conversation_id","is_webgpt_page","run_webgpt_observer_self_tests"]
