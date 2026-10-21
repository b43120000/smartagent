"""Configured, read-only blocking and owned-response lifecycle observations."""
import time
from .contracts import text_digest
from .profile_store import _validate_selector

DEFAULT_BLOCKING_RULES = [
    {'kind':'rate_limit','selectors':['[role="alert"]','[role="dialog"]'],
     'tokens':['too many requests','rate limit','usage limit','使用次數上限','達到使用限制','太多要求']},
    {'kind':'conversation_full','selectors':['[role="alert"]','[role="dialog"]'],
     'tokens':['conversation is too long','maximum length','對話已滿','對話過長']},
    {'kind':'login_required','selectors':['[role="dialog"]'],
     'tokens':['log in to continue','sign in to continue','請先登入','登入以繼續']},
    {'kind':'disconnected','selectors':['[role="alert"]','[role="dialog"]'],
     'tokens':['connection interrupted','network error','reconnecting','連線中斷','網路錯誤']},
    {'kind':'provider_error','selectors':['[role="alert"]','[role="dialog"]'],
     'tokens':['something went wrong','error generating','unable to complete your request','發生錯誤','無法產生回覆','無法完成您的要求']},
]
REASONS={'rate_limit':'PROVIDER_RATE_LIMITED','conversation_full':'PROVIDER_CONVERSATION_FULL',
         'login_required':'PROVIDER_LOGIN_REQUIRED','disconnected':'PROVIDER_DISCONNECTED',
         'provider_error':'PROVIDER_ERROR'}


def validate_blocking_rules(rules):
    if not isinstance(rules,list) or not 1<=len(rules)<=16:raise ValueError('invalid_blocking_rules')
    for rule in rules:
        if not isinstance(rule,dict) or rule.get('kind') not in REASONS:raise ValueError('invalid_blocking_kind')
        for key in ('selectors','tokens'):
            values=rule.get(key)
            if not isinstance(values,list) or not 1<=len(values)<=24 or any(not isinstance(v,str) or not v.strip() or len(v)>2000 for v in values):
                raise ValueError('invalid_blocking_'+key)
        for selector in rule['selectors']:_validate_selector(selector,field='blocking_selector')
    return rules


def detect_blocking(page, rules, *, extra_conversation_selectors=()):
    validate_blocking_rules(rules)
    from .dom_config import load_dom_config
    from .provider_registry import provider_from_url
    try:provider=provider_from_url(getattr(page,'url',''))
    except ValueError:document={}
    else:document=load_dom_config(provider) or {}
    conversation_selectors=[entry['selector'] for name,entry in document.get('elements',{}).items()
                            if name in {'user_turn','assistant_turn'}]+list(extra_conversation_selectors)
    for rule in rules:
        for selector in rule['selectors']:
            for element in page.locator(selector).all():
                if not element.is_visible():continue
                # Quoted errors in conversation content are not transport errors.
                if element.evaluate('''el=>!!el.closest('pre,code,[contenteditable="true"],[data-message-author-role],[data-content-search-unit-key],[data-testid="user-message"],[data-testid="assistant-message"]')'''):continue
                if element.evaluate('(el,selectors)=>selectors.some(s=>!!el.closest(s))',conversation_selectors):continue
                text=element.inner_text().strip()
                if any(token.casefold() in text.casefold() for token in rule['tokens']):
                    return {'reason':REASONS[rule['kind']],'kind':rule['kind'],'message':text[:1000],
                            'selector':selector,'action':'manual_required','auto_resubmit':False}
    return {}


def configured_blocking(adapter):
    from .dom_config import load_capability
    settings=load_capability(adapter.provider_name,'blocking_state')
    if not settings:return {}
    result=detect_blocking(adapter.page,settings['rules'])
    if result:return result
    auth=adapter.authentication_state()
    if auth.status=='LOGIN_REQUIRED':
        return {'reason':'PROVIDER_LOGIN_REQUIRED','kind':'login_required','message':'網站登入已失效，請手動登入。',
                'action':'manual_required','auto_resubmit':False}
    return {}


def arm_generation_trace(page, stop_selector):
    page.evaluate('''selector=>{
        window.__smartAgentGenerationObserver?.disconnect();
        const state=window.__smartAgentGenerationTrace={samples:[],last:null};
        const observe=()=>{
            const active=[...document.querySelectorAll(selector)].some(el=>{
                const r=el.getBoundingClientRect();return !!(r.width&&r.height)&&getComputedStyle(el).visibility!=='hidden';
            });
            if(state.last!==active){state.samples.push({active,time:performance.now()});state.last=active;}
        };
        observe();window.__smartAgentGenerationObserver=new MutationObserver(observe);
        window.__smartAgentGenerationObserver.observe(document.body,{childList:true,attributes:true,subtree:true});
    }''',stop_selector or ':not(*)')


def finish_generation_trace(page):
    return page.evaluate('''()=>{window.__smartAgentGenerationObserver?.disconnect();return window.__smartAgentGenerationTrace?.samples||[];}''')


def wait_owned_response(adapter, scope, marker, *, quiet_seconds=5, timeout=180, allow_raw_preview=False):
    deadline=time.monotonic()+timeout;stable_since=None;previous=None;samples=[]
    while time.monotonic()<deadline:
        if adapter.page.evaluate('()=>window.__smartAgentDialoguePanel?.action==="cancel"||window.__smartAgentComponentPicker?.action==="cancel"||window.__smartAgentFilePicker?.action==="cancel"'):
            raise InterruptedError('calibration_cancelled')
        blocked=adapter.blocking_transport_error()
        if blocked:raise RuntimeError('provider_blocked:'+blocked['reason'])
        adapter.confirm_user_turn(scope)
        turn=adapter.latest_owned_assistant(scope)
        # Before stage 6 there may be no body selector at all (Other URL).
        # A built-in body selector may also be outdated or partial. Raw content
        # is only a readiness preview, never a published body proof.
        text=(turn.raw_text if allow_raw_preview else adapter.extract_final_text(turn)) if turn else ''
        activity=adapter.activity(turn)
        media=adapter.media_state(turn) if turn else {}
        busy=bool(activity.generating or activity.busy_count or media.get('media_pending') or media.get('image_pending') or media.get('video_pending'))
        digest=text_digest(text)
        sample={'active':busy,'length':len(text),'digest':digest,'user_bound':scope.user_turn is not None}
        if not samples or samples[-1]!=sample:samples.append(sample)
        if turn and marker in text and not busy:
            if stable_since is None or previous!=digest:stable_since=time.monotonic()
            elif stable_since is not None and time.monotonic()-stable_since>=quiet_seconds:
                return turn,text,samples
        else:stable_since=None
        previous=digest
        adapter.page.wait_for_timeout(200)
    raise TimeoutError('owned_response_not_idle_and_stable:no_automatic_resubmit')
