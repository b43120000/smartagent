"""Browser-side trusted-input bridge owned by the Claude provider."""

BRIDGE_VERSION = 1

BRIDGE_SCRIPT = r"""
() => {
  const bridgeVersion = 1;
  if (window.__webAgentDirectBridgeVersion === bridgeVersion) return true;
  window.__webAgentDirectBridgeInstalled = true;
  window.__webAgentDirectBridgeVersion = bridgeVersion;
  window.__webAgentDirectQueue = window.__webAgentDirectQueue || [];
  const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const composer = () => {
    const xs = [
      document.querySelector('div.ProseMirror[contenteditable="true"]'),
      document.querySelector('[contenteditable="true"][role="textbox"]')
    ];
    return xs.find(visible) || xs.find(Boolean) || null;
  };
  const textOf = el => ((el && (el.innerText || el.value || el.textContent)) || '').trim();
  const clear = el => {
    el.focus();
    if ('value' in el) {
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
      if (setter) setter.call(el, ''); else el.value = '';
    } else {
      const range = document.createRange(); range.selectNodeContents(el);
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      document.execCommand('delete'); selection.removeAllRanges();
      if (textOf(el)) el.innerHTML = '';
    }
    el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'deleteContentBackward', data:null}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
  };
  const capture = event => {
    if (window.__webAgentAutomationSubmit) return false;
    if (!event.isTrusted) return false;
    const box = composer(), text = textOf(box);
    if (!box || !text) return false;
    event.preventDefault(); event.stopImmediatePropagation(); clear(box);
    window.__webAgentDirectQueue.push({text, captured_at:Date.now()});
    return true;
  };
  document.addEventListener('keydown', event => {
    if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
    const box = composer();
    if (box && (event.target === box || box.contains(event.target))) capture(event);
  }, true);
  document.addEventListener('click', event => {
    const button = event.target?.closest?.(
      'button[aria-label="Send message"], button[aria-label="Send Message"], button[aria-label*="Send message"], button[data-testid*="send"]'
    );
    if (button) capture(event);
  }, true);
  return true;
}
"""

__all__ = ["BRIDGE_SCRIPT", "BRIDGE_VERSION"]
