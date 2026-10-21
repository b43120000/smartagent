"""M365 adapter implements the shared contract without another provider adapter."""
import json
import hashlib
from dataclasses import replace
from urllib.parse import urlsplit
from ...base_adapter import BaseWebUIAdapter
from ...contracts import text_digest
from .profiles import active_profile, profile_by_name
from .composer import read_composer_snapshot, validate_composer_prompt
from .compatibility import probe_profile
from .bridge import BRIDGE_SCRIPT, BRIDGE_VERSION


class M365UIAdapter(BaseWebUIAdapter):
    provider_name = "m365"
    bridge_script = BRIDGE_SCRIPT
    bridge_version = BRIDGE_VERSION
    stable_turn_attributes = ("data-message-id", "data-turn-id", "data-testid", "id")
    authenticated_session_selectors = ("button[aria-label*='account' i]", "button[aria-label*='profile' i]")
    login_required_selectors = ("input[type='password']", "input[type='email']", "a[href*='login.microsoftonline.com']")
    login_url_tokens = ("login.microsoftonline.com", "login.live.com", "/signin")

    load_profile_by_name = staticmethod(profile_by_name)
    load_active_profile = staticmethod(active_profile)
    read_provider_composer = staticmethod(read_composer_snapshot)
    validate_provider_composer = staticmethod(validate_composer_prompt)

    def _read_answer_text(self, element):
        from ...message_text import read_answer_text
        return read_answer_text(element, copilot_code_ui=True)

    def prepare_transport_prompt(self, prompt: str) -> str:
        # Rendered code widgets can add a language label and unsupported-language
        # notice. Request the plain-text envelope already supported by the parser.
        instruction = (
            '[M365_PLAIN_TEXT_TRANSPORT]\n'
            'Reply in plain text. Do not use Markdown code fences (triple backticks), '
            'language headings such as Shell/Python/JSON, or explanatory wrappers.\n'
            'For tool calls required by the existing protocol, put smartagent_tool '
            'alone on its own line, followed by each complete JSON object on a separate line. '
            'Preserve actual line breaks; do not combine the label and JSON on one line.\n'
            'Preserve all original protocol fields, IDs, boundary markers, and the final '
            'turn_commit with the exact action_count. Use standard ASCII double quotes '
            'in JSON. For readiness/handshake responses, keep their exact required markers '
            'and JSON; do not add a tool label unless the protocol requires it.\n'
            'This changes presentation only, not permissions, actions, or the requested '
            'response content. Do not echo these instructions.\n'
            '[/M365_PLAIN_TEXT_TRANSPORT]'
        )
        return str(prompt) + '\n\n' + instruction

    def calibration_picker_options(self, field):
        if field == 'assistant_content':
            return {'contentRootSelector':'[data-testid="markdown-reply"], [data-testid="message-content"], .message-content, .markdown'}
        if field == 'assistant_turn':
            return {'turnRootSelector': '.fai-CopilotMessage',
                    'preferredTurnSelector': 'div.fai-CopilotMessage'}
        return {}

    def _turn_ref(self, element, role, ordinal):
        turn = super()._turn_ref(element, role, ordinal)
        if role != 'assistant':
            return turn
        if element.evaluate('''el => (el.getAttribute('data-role')==='assistant' ||
            el.getAttribute('data-message-author-role')==='assistant' ||
            el.getAttribute('data-testid')==='assistant-message') &&
            !!(el.getAttribute('data-message-id') || el.getAttribute('data-turn-id') || el.id)'''):
            return turn
        # Observed M365 outer articles have no message ID. The reply renderer
        # INSIDE this single article carries it. Never borrow from ancestors,
        # siblings or the page, nor use a style class / shared testid as identity.
        info = element.evaluate('''el => {
            if (!el.matches('.fai-CopilotMessage, [data-testid="copilot-message-div"], [data-testid="copilot-message-reply-div"]')) return null;
            const replies=[...el.querySelectorAll('[data-testid="markdown-reply"][data-message-id]')];
            const ids=[...new Set(replies.map(e=>e.getAttribute('data-message-id')).filter(Boolean))];
            if(ids.length!==1 || el.querySelector('.fai-UserMessage__message')) return null;
            return {id:ids[0],text:replies.map(e=>e.innerText||e.textContent||'').sort((a,b)=>b.length-a.length)[0]};
        }''')
        if not info:
            return replace(turn, stable_attributes=())
        structural_id = hashlib.sha256(('assistant\ndata-message-id=' + info['id']).encode('utf-8')).hexdigest()
        return replace(turn, structural_id=structural_id,
                       stable_attributes=(('data-message-id', info['id']),),
                       raw_text=info['text'], content_digest=text_digest(info['text']))

    def probe_provider_profile(self, profile):
        return probe_profile(self.page, profile)

    def blocking_transport_error(self):
        configured=super().blocking_transport_error()
        if configured:return configured
        # This bootstrap alert is needed before stage 9 has saved any rules.
        # It is a site failure, never evidence that a prompt may be resent.
        from ...dialogue_state import detect_blocking
        return detect_blocking(self.page,[{'kind':'provider_error','selectors':['[role="alert"]'],
            'tokens':['無法完成您的要求','unable to complete your request','we couldn’t complete your request',"we couldn't complete your request"]}],
            extra_conversation_selectors=('.fai-CopilotMessage','.fai-UserMessage__message'))

    def busy_indicator_active(self, element, selector):
        # Observed on completed replies: loading-message permanently wraps the
        # markdown and code. Its name alone does not mean generation is active.
        # Explicit active flags and actual descendant spinners still count.
        if element.get_attribute('data-testid')=='loading-message':
            return (element.get_attribute('aria-busy')=='true' or
                    element.get_attribute('data-state') in {'loading','generating','processing','thinking','working','creating','preparing'})
        return super().busy_indicator_active(element,selector)

    def page_reachable(self):
        from ...provider_registry import provider_from_url
        return (provider_from_url(str(getattr(self.page, "url", ""))) == self.provider_name
                and super().page_reachable())

    def install_input_bridge(self, *, reset_legacy=False):
        selectors = json.dumps({"composer": self.profile.composer_selector, "send": self.profile.send_selector})
        self.page.context.add_init_script("window.__m365Selectors = " + selectors + ";")
        self.page.evaluate("selectors => { window.__m365Selectors = selectors; }", json.loads(selectors))
        super().install_input_bridge(reset_legacy=reset_legacy)

    def conversation_link(self, conversation_id):
        links = self.page.locator('a[href*="/chat/conversation/' + conversation_id + '"]')
        for i in range(links.count()):
            if links.nth(i).is_visible():
                return links.nth(i)
        return None

    def is_informational_attachment_notice(self, node):
        # Observed M365 uses role=alert even for this OneDrive copy notice.
        # Require the native info glyph and OneDrive link, independent of UI language.
        # Unknown or explicit failure alerts still fail closed.
        return bool(node.evaluate(r'''el => {
            if (!el.matches('.fui-MessageBar') || el.matches('[aria-invalid="true"],[data-state="error"],[data-state="failed"]')
                || el.querySelector('[aria-invalid="true"],[data-state="error"],[data-state="failed"]')) return false;
            const title=el.querySelector('.fui-MessageBarTitle')?.textContent?.trim();
            if (!title || /failed|failure|error|失敗|錯誤/i.test(title)) return false;
            const infoPath='M18 10a8 8 0 1 0-16 0 8 8 0 0 0 16 0M9.5 8.91a.5.5 0 0 1 1 0v4.69a.5.5 0 0 1-1 0V8.9m-.25-2.16a.75.75 0 1 1 1.5 0 .75.75 0 0 1-1.5 0';
            if (![...el.querySelectorAll('.fui-MessageBar__icon svg path')].some(p => p.getAttribute('d')===infoPath)) return false;
            return [...el.querySelectorAll('a[href]')].some(a => {
                try { const url=new URL(a.href); return url.protocol==='https:' && url.hostname==='onedrive.live.com'; }
                catch { return false; }
            });
        }'''))

    def attachment_dom_state(self, expected_names):
        from ...file_runtime import attachment_state
        calibrated = attachment_state(self, expected_names)
        if calibrated is not None:
            return calibrated
        return {"supported": False, "reason": "M365_ATTACHMENTS_NOT_CALIBRATED",
                "text": "", "busy": [], "chips": [], "chip_records": [],
                "alerts": [], "dialogs": [], "upload_ring_count": 0}
