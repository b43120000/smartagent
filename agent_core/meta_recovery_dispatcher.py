#!/usr/bin/env python3
from __future__ import annotations
import hashlib, json, os, time, uuid
from pathlib import Path
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
from .self_repair_protocol import DEFAULT_REPAIR_URL, parse
from .issue_recorder import redact_sensitive
from .workspace import AGENT_PROJECT_ROOT
from .webgpt_rate_governor import WebGPTRateGovernor

DEFAULT_CDP=os.environ.get("SMARTAGENT_CHATGPT_CDP","http://127.0.0.1:1272")
STATE_ROOT=AGENT_PROJECT_ROOT/".agents"/"self_repair"
PLAN_PATH=STATE_ROOT/"meta_repair_plan.json"
RESULT_PATH=STATE_ROOT/"meta_dispatch_result.json"
MARKER="smartagent-meta-recovery-agent2"

def _atomic(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+".tmp")
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2,sort_keys=True),encoding="utf-8")
    tmp.replace(path)

def _cid(url):
    text=str(url or "")
    return text.split("/c/",1)[1].split("?",1)[0].split("#",1)[0].strip("/") if "/c/" in text else ""

def _is_repair_page(page):
    try:
        return _cid(page.url)==_cid(DEFAULT_REPAIR_URL) or str(page.evaluate("() => window.name || ''"))==MARKER
    except Exception:
        return False

def _get_repair_page(context):
    for page in list(context.pages):
        if _is_repair_page(page):
            return page,False
    page=context.new_page()
    try:
        page.evaluate("v => window.name=v",MARKER)
    except Exception:
        pass
    return page,True

def _navigate(page):
    if _cid(page.url)==_cid(DEFAULT_REPAIR_URL):
        return
    try:
        page.goto(DEFAULT_REPAIR_URL,wait_until="domcontentloaded",timeout=60000)
    except PlaywrightTimeoutError:
        pass
    page.locator("#prompt-textarea").wait_for(state="visible",timeout=30000)

def _dismiss_rate_limit_dialog(page):
    try:
        modal=page.locator('[data-testid="modal-conversation-history-rate-limit"]')
        if not modal.count() or not modal.is_visible():
            return False
        buttons=modal.locator("button")
        if buttons.count():
            buttons.last.click(timeout=10000)
            modal.wait_for(state="hidden",timeout=10000)
            return True
    except Exception:
        pass
    return False

def _build_request(meta):
    issue=str(meta.get("issue_id") or "")
    if not issue.startswith("ISSUE-"):
        issue="ISSUE-META-"+uuid.uuid4().hex[:12].upper()
    evidence_text=redact_sensitive(json.dumps(meta.get("diagnostic_evidence",{}),ensure_ascii=False),limit=12000)
    prompt=(
        "[SELF_REPAIR_REQUEST]\n"
        "你是 LocalAgent Agent 2 repair-only WebGPT。\n"
        "self-repair 流程本身發生故障。只規劃 LocalAgent 修復；"
        "禁止修改使用者專案、RemoteAgent、launcher、.agents、.git。\n"
        f"issue_id: {issue}\n"
        f"stage: {meta.get('stage','')}\n"
        f"reason: {meta.get('failed_reason','')}\n"
        f"fingerprint: {meta.get('fingerprint','')}\n"
        f"detail: {str(meta.get('detail',''))[:4000]}\n"
        f"diagnostic_evidence: {evidence_text}\n"
        "Use diagnostic_evidence first to infer the failing module/source location. "
        "Do not require the caller to name a source file. If source is still uncertain, request only bounded inspect_paths.\n"
        "只回傳 fenced self_repair_control JSON。type=SELF_REPAIR_PLAN，"
        "protocol=self_repair，protocol_version=1，"
        f"issue_id 必須完全等於 {issue}，modified_paths 只能列允許的 LocalAgent Python 路徑。"
        "若要修改檔案，另提供 files 陣列，每項只含 path 與完整修復後 content；path 集合必須與 modified_paths 完全一致。"
        "若不需修改，modified_paths=[] 且 files=[]。"
        "若需要先看目前工作樹原始碼才能安全修復，第一輪回傳 modified_paths=[]、files=[]、inspect_paths=[最多4個允許的Python路徑]；收到 SOURCE_CONTEXT 後再回傳完整 executable SELF_REPAIR_PLAN。"
    )
    return issue,prompt

def _assistant_fp(locator):
    try:
        if not locator.count(): return ""
        text=locator.last.inner_text(timeout=3000).strip()
        return hashlib.sha256(text.encode("utf-8",errors="replace")).hexdigest() if text else ""
    except Exception:
        return ""

def _wait_response(page,before,before_fp="",timeout_sec=300):
    deadline=time.monotonic()+timeout_sec
    stable=""; stable_since=0.0
    while time.monotonic()<deadline:
        assistants=page.locator('[data-message-author-role="assistant"]')
        count=assistants.count(); current_fp=_assistant_fp(assistants)
        fresh=bool(count>before or (current_fp and current_fp!=before_fp))
        if fresh:
            response=assistants.last.inner_text(timeout=5000).strip()
            active=(page.locator('button[data-testid="stop-button"]').count()>0
                    or page.locator('button[aria-label*="Stop"]').count()>0)
            if response and not active:
                if response==stable and stable_since and time.monotonic()-stable_since>=2.0:
                    return response
                if response!=stable:
                    stable=response; stable_since=time.monotonic()
        page.wait_for_timeout(500)
    raise TimeoutError("meta_recovery_agent2_response_timeout")

def _submit_with_rate_gate(page,rate_lease):
    rate_lease.before_submit()
    try:
        page.locator('button[data-testid="send-button"]').click(timeout=20000)
    finally:
        rate_lease.record_submit()

def dispatch_once(meta,cdp=DEFAULT_CDP,root=AGENT_PROJECT_ROOT):
    root=Path(root).resolve()
    rate_lease=WebGPTRateGovernor(root).acquire(wait=True)
    try:
        return _dispatch_once_locked(meta,cdp,root,rate_lease)
    finally:
        rate_lease.release()

def _dispatch_once_locked(meta,cdp,root,rate_lease):
    state_root=root/".agents"/"self_repair"
    plan_path=state_root/"meta_repair_plan.json"
    result_path=state_root/"meta_dispatch_result.json"
    issue,prompt=_build_request(meta)
    result={"status":"STARTED","issue_id":issue,"started_at":time.time()}
    with sync_playwright() as pw:
        browser=pw.chromium.connect_over_cdp(cdp)
        if not browser.contexts:
            raise RuntimeError("no_chromium_context")
        context=browser.contexts[0]
        planner_pages=[p for p in context.pages if "/c/" in str(p.url or "") and not _is_repair_page(p)]
        planner_before=[str(p.url or "") for p in planner_pages]
        page,created=_get_repair_page(context)
        result["dedicated_page_created"]=created
        _navigate(page)
        result["rate_limit_dialog_dismissed"]=_dismiss_rate_limit_dialog(page)
        assistants_before=page.locator('[data-message-author-role="assistant"]')
        before=assistants_before.count()
        before_fp=_assistant_fp(assistants_before)
        page.locator("#prompt-textarea").fill(prompt)
        _dismiss_rate_limit_dialog(page)
        _submit_with_rate_gate(page,rate_lease)
        response=_wait_response(page,before,before_fp)
        envelope,reason=parse(response)
        if not envelope:
            result.update(status="PROTOCOL_REJECTED",parser_reason=reason,response=response[:12000])
            _atomic(result_path,result); return result
        if envelope.get("type")!="SELF_REPAIR_PLAN" or envelope.get("issue_id")!=issue:
            result.update(status="PLAN_REJECTED",parser_reason="plan_identity_mismatch")
            _atomic(result_path,result); return result
        inspect_paths=list(envelope.get("inspect_paths") or [])
        if inspect_paths and not envelope.get("modified_paths") and not envelope.get("files"):
            source=[]; total=0
            for rel in inspect_paths:
                target=(root/rel).resolve()
                try:
                    target.relative_to(root)
                except Exception:
                    result.update(status="SOURCE_CONTEXT_REJECTED",parser_reason="inspect_path_escape")
                    _atomic(result_path,result); return result
                try:
                    content=target.read_text(encoding="utf-8")
                except Exception as exc:
                    result.update(status="SOURCE_CONTEXT_FAILED",parser_reason=f"{type(exc).__name__}: {exc}")
                    _atomic(result_path,result); return result
                encoded=content.encode("utf-8")
                if len(encoded)>16384 or total+len(encoded)>49152:
                    result.update(status="SOURCE_CONTEXT_REJECTED",parser_reason="source_context_too_large")
                    _atomic(result_path,result); return result
                total+=len(encoded); source.append({"path":rel,"content":content})
            followup="[SELF_REPAIR_SOURCE_CONTEXT]\nissue_id: "+issue+"\nCurrent worktree source follows. Return only the final fenced self_repair_control SELF_REPAIR_PLAN with files matching modified_paths exactly.\n"+json.dumps(source,ensure_ascii=False)
            assistants_before=page.locator('[data-message-author-role="assistant"]'); before=assistants_before.count(); before_fp=_assistant_fp(assistants_before)
            page.locator("#prompt-textarea").fill(followup); _dismiss_rate_limit_dialog(page); _submit_with_rate_gate(page,rate_lease)
            response=_wait_response(page,before,before_fp); envelope,reason=parse(response)
            if not envelope:
                result.update(status="PROTOCOL_REJECTED",parser_reason=reason,response=response[:12000]); _atomic(result_path,result); return result
            if envelope.get("type")!="SELF_REPAIR_PLAN" or envelope.get("issue_id")!=issue:
                result.update(status="PLAN_REJECTED",parser_reason="plan_identity_mismatch"); _atomic(result_path,result); return result
            result["source_context_paths"]=inspect_paths
        planner_after=[str(p.url or "") for p in planner_pages if p in context.pages]
        planner_preserved=(len(planner_after)==len(planner_before) and planner_after==planner_before)
        if not planner_preserved:
            result.update(status="PLANNER_PAGE_MUTATED",planner_urls_before=planner_before,planner_urls_after=planner_after)
            _atomic(result_path,result); return result
        _atomic(plan_path,{"version":1,"issue_id":issue,"received_at":time.time(),"plan":envelope})
        result.update(status="PLAN_READY",completed_at=time.time(),
                      modified_paths=list(envelope.get("modified_paths") or []),
                      summary=str(envelope.get("summary","") or ""),
                      plan_path=str(plan_path),planner_page_preserved=True)
        _atomic(result_path,result)
        return result

class MetaRecoveryDispatcher:
    def __init__(self,root=AGENT_PROJECT_ROOT,cdp=DEFAULT_CDP):
        self.root=Path(root); self.cdp=cdp
    def dispatch_if_required(self,meta):
        if not meta.get("repair_dispatch_required"):
            return {"status":"NOT_REQUIRED"}
        return dispatch_once(meta,self.cdp,self.root)
