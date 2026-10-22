"""Trusted input bridge using the active calibrated M365 selectors."""
from .composer import READ_COMPOSER_TEXT_JS

BRIDGE_VERSION = 2
BRIDGE_SCRIPT = r"""() => {
    if (window.__m365BridgeInstalled) {
        if (Number(window.__webAgentDirectBridgeVersion || 0) !== 2)
            throw new Error('M365_INPUT_BRIDGE_UPGRADE_REQUIRES_RELOAD: reload the page before reinstalling the bridge');
        return true;
    }
    window.__m365BridgeInstalled = true;
    window.__webAgentDirectBridgeInstalled = true;
    window.__webAgentDirectBridgeVersion = 2;
    window.__webAgentDirectQueue = window.__webAgentDirectQueue || [];
    const box = () => [...document.querySelectorAll(window.__m365Selectors.composer)]
        .find(el => el.isConnected && el.getClientRects().length);
    const readText = __M365_COMPOSER_READER__;
    const capture = event => {
        if (!event.isTrusted || window.__webAgentAutomationSubmit) return;
        const el = box();
        if (!el) return;
        const text = readText(el);
        if (!text || !text.trim()) return;
        event.preventDefault(); event.stopImmediatePropagation();
        window.__webAgentDirectQueue.push({text, captured_at:Date.now()});
        if ('value' in el) el.value = ''; else el.textContent = '';
        el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'deleteContentBackward'}));
    };
    document.addEventListener('keydown', event => {
        const el = box();
        if (el && (event.target === el || el.contains(event.target)) && event.key === 'Enter'
            && !event.shiftKey && !event.isComposing) capture(event);
    }, true);
    document.addEventListener('click', event => {
        if (event.target.closest?.(window.__m365Selectors.send)) capture(event);
    }, true);
    return true;
}""".replace("__M365_COMPOSER_READER__", READ_COMPOSER_TEXT_JS)
