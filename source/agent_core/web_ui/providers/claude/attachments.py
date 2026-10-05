"""Claude composer attachment observations owned by the Claude provider."""
from __future__ import annotations


FILE_INPUT_SELECTOR = (
    "form:has(div.ProseMirror[contenteditable='true']) input[type='file'], "
    "form:has([contenteditable='true'][role='textbox']) input[type='file'], "
    "[data-testid*='composer'] input[type='file']"
)

ATTACH_BUTTON_SELECTORS = (
    'button[aria-label*="Attach"]',
    'button[aria-label*="Upload"]',
    'button[aria-label*="Add file"]',
    'button[aria-label*="Add files"]',
)

ATTACH_MENU_SELECTORS = (
    '[role="menuitem"]:has-text("Upload")',
    '[role="menuitem"]:has-text("Attach")',
    '[role="menuitem"]:has-text("Add file")',
)


def file_inputs(page) -> tuple:
    try:
        return tuple(page.query_selector_all(FILE_INPUT_SELECTOR))
    except Exception:
        return ()


def attach_button(page):
    for selector in ATTACH_BUTTON_SELECTORS:
        try:
            button = page.query_selector(selector)
            if button and button.is_visible():
                return button
        except Exception:
            continue
    return None


def attach_menu_items(page) -> tuple:
    result = []
    for selector in ATTACH_MENU_SELECTORS:
        try:
            item = page.query_selector(selector)
            if item and item.is_visible():
                result.append(item)
        except Exception:
            continue
    return tuple(result)


def attachment_dom_state(page, expected_names: list[str]) -> dict:
    return page.evaluate(r"""(expectedNames) => {
        const composer = document.querySelector(
            'div.ProseMirror[contenteditable="true"], [contenteditable="true"][role="textbox"]'
        );
        const root = composer?.closest('form') || composer?.parentElement?.parentElement || composer?.parentElement;
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const alerts = [...document.querySelectorAll('[role="alert"], [data-testid*="toast"], [class*="toast"]')]
            .filter(visible).map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean);
        const dialogs = [...document.querySelectorAll('[role="dialog"]')]
            .filter(visible).map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean);
        if (!root) return {text:'', busy:[], chips:[], chip_records:[], alerts, dialogs, upload_ring_count:0};
        const descriptorOf = el => [
            el?.textContent, el?.getAttribute?.('aria-label'), el?.getAttribute?.('title'),
            el?.getAttribute?.('data-file-name'), el?.getAttribute?.('data-testid')
        ].filter(Boolean).join(' ').trim();
        const busySelector = [
            '[role="progressbar"]','[aria-busy="true"]','[data-state="loading"]',
            '[data-state="uploading"]','[data-state="processing"]','[class*="loading"]',
            '[class*="upload"]','[class*="progress"]','[class*="spinner"]'
        ].join(',');
        const busyNodes = [...root.querySelectorAll(busySelector)].filter(visible);
        const allVisible = [...root.querySelectorAll('*')].filter(visible);
        const chipRecords = [];
        for (const name of expectedNames || []) {
            const candidates = allVisible.filter(el => descriptorOf(el).includes(name))
                .sort((a,b) => descriptorOf(a).length - descriptorOf(b).length);
            if (!candidates.length) continue;
            let node = candidates[0];
            for (let depth=0; depth<5 && node.parentElement && node.parentElement !== root; depth++) {
                if (node.querySelector('button[aria-label*="Remove"], button[aria-label*="Delete"]')) break;
                const parentText = descriptorOf(node.parentElement);
                if ((expectedNames || []).filter(other => parentText.includes(other)).length !== 1) break;
                node = node.parentElement;
            }
            const descriptor = descriptorOf(node);
            const localBusy = [...node.querySelectorAll(busySelector)].filter(visible);
            const stateText = [node.getAttribute('data-state'), node.getAttribute('aria-label'), node.className || '']
                .filter(Boolean).join(' ').toLowerCase();
            chipRecords.push({
                descriptor,
                removable: !!node.querySelector('button[aria-label*="Remove"], button[aria-label*="Delete"]'),
                processing: localBusy.length > 0,
                error: /(^|[\s_-])(error|failed|rejected)([\s_-]|$)/.test(stateText),
                explicit_complete: /(^|[\s_-])(complete|completed|success|ready|uploaded)([\s_-]|$)/.test(stateText),
                progress: [],
            });
        }
        return {
            text: root.innerText || '',
            busy: busyNodes.map(el => descriptorOf(el) || el.tagName),
            upload_ring_count: busyNodes.length,
            chips: chipRecords.map(record => record.descriptor),
            chip_records: chipRecords,
            alerts, dialogs,
        };
    }""", expected_names)


def composer_attachment_count(page) -> int:
    return int(page.evaluate(r"""() => {
        const composer = document.querySelector(
            'div.ProseMirror[contenteditable="true"], [contenteditable="true"][role="textbox"]'
        );
        const root = composer?.closest('form') || composer?.parentElement?.parentElement || composer?.parentElement;
        if (!root) return 0;
        const selected = [...root.querySelectorAll('input[type="file"]')]
            .reduce((count,input) => count + Number(input.files?.length || 0), 0);
        const removable = root.querySelectorAll(
            'button[aria-label*="Remove"], button[aria-label*="Delete"], button[data-testid*="remove"]'
        ).length;
        return Math.max(selected, removable);
    }""") or 0)


def clear_one_attachment(page) -> bool:
    result = page.evaluate(r"""() => {
        const composer = document.querySelector(
            'div.ProseMirror[contenteditable="true"], [contenteditable="true"][role="textbox"]'
        );
        const root = composer?.closest('form') || composer?.parentElement?.parentElement || composer?.parentElement;
        if (!root) return {clicked:false};
        const button = root.querySelector(
            'button[aria-label*="Remove"], button[aria-label*="Delete"], button[data-testid*="remove"]'
        );
        if (button) button.click();
        return {clicked:!!button};
    }""") or {}
    return isinstance(result, dict) and bool(result.get("clicked"))


__all__ = [
    "attach_button", "attach_menu_items", "attachment_dom_state",
    "clear_one_attachment", "composer_attachment_count", "file_inputs",
]
