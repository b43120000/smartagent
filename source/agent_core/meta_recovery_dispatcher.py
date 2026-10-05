#!/usr/bin/env python3
from __future__ import annotations
import json, os, time, uuid
from pathlib import Path
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
from .self_repair_protocol import DEFAULT_REPAIR_URL, parse
from .issue_recorder import redact_sensitive
from .workspace import AGENT_PROJECT_ROOT
from .webgpt_rate_governor import WebGPTRateGovernor
from .paths import self_repair_root
from .web_ui import create_web_ui_for_page
from WebAgent.browser_bridge import execution_page_lease

DEFAULT_CDP=os.environ.get("SMARTAGENT_CHATGPT_CDP","http://127.0.0.1:1272")
STATE_ROOT=self_repair_root()
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

def _repair_target_id():
    return _cid(DEFAULT_REPAIR_URL)

def _is_repair_page(page):
    try:
        target_id=_repair_target_id()
        return bool(target_id and _cid(page.url)==target_id) or str(page.evaluate("() => window.name || ''"))==MARKER
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
    target_id=_repair_target_id()
    if not target_id:
        raise RuntimeError(
            "self_repair_url_not_configured: set SMARTAGENT_SELF_REPAIR_URL "
            "to a dedicated ChatGPT /c/... conversation"
        )
    if _cid(page.url)==target_id:
        return
    try:
        page.goto(DEFAULT_REPAIR_URL,wait_until="domcontentloaded",timeout=60000)
    except PlaywrightTimeoutError:
        pass
    composer=create_web_ui_for_page(page).visible_composer()
    if composer is None:
        raise RuntimeError("meta_recovery_composer_unavailable")
    composer.wait_for(state="visible",timeout=30000)

def _dismiss_rate_limit_dialog(page):
    return create_web_ui_for_page(page).dismiss_rate_limit_dialog()

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

def _wait_response(page,scope,timeout_sec=300):
    web_ui=create_web_ui_for_page(page)
    deadline=time.monotonic()+timeout_sec
    stable=""; stable_since=0.0
    while time.monotonic()<deadline:
        if scope.user_turn is None:
            web_ui.confirm_user_turn(scope)
        assistant=web_ui.latest_owned_assistant(scope)
        if assistant is not None:
            response=web_ui.extract_final_text(assistant)
            active=web_ui.generation_active()
            if response and not active:
                if response==stable and stable_since and time.monotonic()-stable_since>=2.0:
                    return response
                if response!=stable:
                    stable=response; stable_since=time.monotonic()
        page.wait_for_timeout(500)
    raise TimeoutError("meta_recovery_agent2_response_timeout")

def _submit_with_rate_gate(web_ui,rate_lease):
    rate_lease.before_submit()
    try:
        send=web_ui.send_control()
        if send is None:
            raise RuntimeError("meta_recovery_send_control_unavailable")
        send.click(timeout=20000)
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
    state_root=self_repair_root(root)
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
        with execution_page_lease(timeout_sec=120.0,label="Agent2 common conversation page"):
            page,created=_get_repair_page(context)
            result["dedicated_page_created"]=created
            try:
                _navigate(page)
            except BaseException:
                if created:
                    try: page.close()
                    except Exception: pass
                raise
        result["rate_limit_dialog_dismissed"]=_dismiss_rate_limit_dialog(page)
        web_ui=create_web_ui_for_page(page)
        scope=web_ui.capture_request(prompt,conversation_id=_cid(page.url))
        composer=web_ui.visible_composer()
        if composer is None:
            raise RuntimeError("meta_recovery_composer_unavailable")
        composer.fill(prompt)
        _dismiss_rate_limit_dialog(page)
        _submit_with_rate_gate(web_ui,rate_lease)
        response=_wait_response(page,scope)
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
            scope=web_ui.capture_request(followup,conversation_id=_cid(page.url))
            composer=web_ui.visible_composer()
            if composer is None:
                raise RuntimeError("meta_recovery_composer_unavailable")
            composer.fill(followup); _dismiss_rate_limit_dialog(page); _submit_with_rate_gate(web_ui,rate_lease)
            response=_wait_response(page,scope); envelope,reason=parse(response)
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
