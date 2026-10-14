"""Offline real DOM tests: provider quota vs quoted user/assistant content."""
from playwright.sync_api import sync_playwright
import json
from agent_core.web_ui.providers.chatgpt.adapter import ChatGPTUIAdapter

def run():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            adapter=ChatGPTUIAdapter(page)
            panel='<div><div><div>你已達到上限。請稍後再試一次。</div></div><button>重試</button></div>'
            page.set_content(panel)
            assert adapter.blocking_transport_error()['reason']=='PROVIDER_RATE_LIMITED'
            full_panel='<div><div><div>這段對話已達到長度上限，請開始新聊天。</div></div><button>New chat</button></div>'
            page.set_content(full_panel)
            assert adapter.blocking_transport_error()['reason']=='PROVIDER_CONVERSATION_FULL'
            page.set_content('<div data-content-search-unit-key="turn:0:user">'+full_panel+'</div>')
            assert not adapter.blocking_transport_error()
            for wrapper in ('<div data-content-search-unit-key="turn:0:user">',
                '<div data-message-author-role="assistant">','<pre>','<div contenteditable="true">'):
                page.set_content(wrapper+panel+'</div>')
                assert not adapter.blocking_transport_error()
            page.set_content('<div style="display:none">'+panel+'</div>')
            assert not adapter.blocking_transport_error()
            page.set_content('<div>你已達到上限。請稍後再試一次。</div>')
            assert not adapter.blocking_transport_error()
            page.set_content('<div data-content-search-unit-key="turn:0:user">SENTINEL_SECRET_DO_NOT_LOG</div>')
            diagnostic = json.dumps(adapter.turn_mount_diagnostic())
            assert 'SENTINEL_SECRET_DO_NOT_LOG' not in diagnostic and 'text_len' in diagnostic
        finally:
            browser.close()
    print('PROVIDER_QUOTA_DOM_PASS')
    from RemoteAgent.telegram_delivery import TelegramDeliveryAdapter
    for event_type in ('TASK_FAILED','TASK_INTERRUPTED'):
        rendered=TelegramDeliveryAdapter.render({'event_type':event_type,'event_id':'E','request_id':'R',
            'payload':{'error':'WebScraperStageError: [WEB_PROVIDER_CONVERSATION_FULL] full'}})
        assert '對話已滿' in rendered and 'event_id: E' in rendered and 'request_id: R' in rendered
    print('CONVERSATION_FULL_TELEGRAM_RENDER_PASS')

if __name__=='__main__':run()
