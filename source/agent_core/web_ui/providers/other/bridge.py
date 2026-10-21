"""Calibrated input capture scoped to one Other origin and owned page."""
BRIDGE_VERSION = 1
BRIDGE_SCRIPT = r"""() => {
    const config = __OTHER_CONFIG__;
    if (location.origin !== config.origin) return false;
    if (window.__smartAgentOtherBridgeInstalled === config.provider) {
        Object.assign(window.__smartAgentOtherBridgeConfig, config);
        return true;
    }
    window.__smartAgentOtherBridgeConfig = config;
    const visible = el => el.isConnected && el.getClientRects().length;
    const box = () => {
        const matches = [...document.querySelectorAll(config.composer)].filter(visible);
        return matches.length === 1 ? matches[0] : null;
    };
    window.__webAgentDirectQueue = window.__webAgentDirectQueue || [];
    const capture = event => {
        if (!event.isTrusted || window.__webAgentAutomationSubmit || location.origin !== config.origin) return;
        const el = box();
        if (!el || !el.isConnected) return;
        const text = 'value' in el ? el.value : el.innerText;
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
        if (event.target.closest?.(config.send)) capture(event);
    }, true);
    window.__smartAgentOtherBridgeInstalled = config.provider;
    window.__webAgentDirectBridgeInstalled = true;
    window.__webAgentDirectBridgeVersion = 1;
    return true;
}"""
