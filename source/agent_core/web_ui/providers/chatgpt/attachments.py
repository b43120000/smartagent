"""ChatGPT attachment DOM observations owned by the provider package."""
from __future__ import annotations


FILE_INPUT_SELECTOR = (
    'form[data-type="unified-composer"] input[type="file"], '
    'form:has(#prompt-textarea) input[type="file"], '
    'form:has(div.ProseMirror[contenteditable="true"][role="textbox"]) input[type="file"], '
    '[data-testid="composer"] input[type="file"], '
    '[data-testid*="composer"] input[type="file"]'
)

ATTACH_BUTTON_SELECTORS = (
    'button[data-testid="composer-plus-btn"]',
    'button[aria-label="Attach files"]',
    'button[aria-label="Upload file"]',
    'button[aria-label="Upload"]',
    'button[aria-label="Upload image"]',
    'button[aria-label="Add files and more"]',
    'button[aria-label="新增檔案和更多內容"]',
    'button[aria-label*="Add files"]',
    'button[aria-label*="新增檔案"]',
    'button[aria-label*="Attach"]',
    'button[aria-label*="Upload"]',
    'button[tooltip="Attach files"]',
)

ATTACH_MENU_SELECTORS = (
    'text="Upload from computer"',
    'text="Upload file"',
    'text="Attach files"',
    'text="從電腦上傳"',
    'text="上傳檔案"',
    '[role="menuitem"]:has-text("Upload")',
    '[role="menuitem"]:has-text("Attach")',
    '[role="menuitem"]:has-text("上傳")',
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
        const send = document.querySelector('button[data-testid="send-button"], button[aria-label*="Send"], button[aria-label*="傳送"]');
        const composer = send?.closest('form') || send?.closest('[data-testid*="composer"]') ||
            document.querySelector('#prompt-textarea, div.ProseMirror[contenteditable="true"][role="textbox"]')?.closest('form') ||
            document.querySelector('form[data-type="unified-composer"], [data-testid="composer"]');
        const visible = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const alerts = [...document.querySelectorAll('[role="alert"], [data-sonner-toast], [data-testid*="toast"], [class*="toast"]')]
            .filter(visible).map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean);
        const dialogs = [...document.querySelectorAll('[role="dialog"], [data-testid*="modal"], [class*="modal"]')]
            .filter(visible).map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean);
        if (!composer) return {text: '', busy: [], chips: [], chip_records: [], alerts, dialogs};
        const busySelector = [
            '[role="progressbar"]', '[aria-busy="true"]',
            '[data-state="loading"]', '[data-state="uploading"]',
            '[data-state="processing"]', '[data-state="pending"]',
            '[class*="uploading"]', '[class*="processing"]',
            '[class*="spinner"]', '[class*="animate-spin"]',
            '[class*="loading"]', '[class*="progress"]'
        ].join(',');
        const descriptorOf = el => [
            el?.textContent, el?.getAttribute?.('aria-label'), el?.getAttribute?.('title'),
            el?.getAttribute?.('data-file-name')
        ].filter(Boolean).join(' ').trim();
        const explicitChips = [...composer.querySelectorAll(
            '[data-testid*="attachment"], [class*="attachment"], '
            + '[data-testid="file-thumbnail"], [data-testid="composer-file"], '
            + '[data-file-name], [data-testid*="file"], [class*="file"]'
        )].filter(visible);
        const allVisible = [...composer.querySelectorAll('*')].filter(visible);
        const filenameChips = [];
        for (const name of expectedNames || []) {
            const matches = allVisible.filter(el => {
                const descriptor = descriptorOf(el);
                return descriptor.includes(name)
                    && (expectedNames || []).filter(other => descriptor.includes(other)).length === 1;
            }).sort((a, b) => descriptorOf(a).length - descriptorOf(b).length);
            if (!matches.length) continue;
            let root = matches[0];
            for (let depth = 0; depth < 6 && root.parentElement && root.parentElement !== composer; depth++) {
                if (root.querySelector(
                    'button[aria-label*="Remove"], button[aria-label*="Delete"], '
                    + 'button[aria-label*="移除"], button[aria-label*="刪除"], '
                    + 'button[data-testid*="remove"]'
                )) break;
                const parentDescriptor = descriptorOf(root.parentElement);
                if ((expectedNames || []).filter(other => parentDescriptor.includes(other)).length !== 1) break;
                root = root.parentElement;
            }
            filenameChips.push(root);
        }
        const chipElements = [...new Set([...explicitChips, ...filenameChips])];
        const progressPercent = node => {
            const value = Number(node.getAttribute('aria-valuenow') ?? node.value);
            const maximum = Number(node.getAttribute('aria-valuemax') ?? node.max ?? 100);
            return Number.isFinite(value) && Number.isFinite(maximum) && maximum > 0
                ? Math.round((value / maximum) * 100) : null;
        };
        const activelyBusy = node => visible(node) && !(
            node.getAttribute('role') === 'progressbar' && progressPercent(node) >= 100
        );
        const removeControlSelector = [
            'button[aria-label*="Remove"], button[aria-label*="Delete"], '
            + 'button[aria-label*="移除"], button[aria-label*="刪除"], button[data-testid*="remove"]'
        ].join('');
        const isRemoveControl = node => !!node.closest(removeControlSelector);
        const isUploadRing = node => {
            if (!visible(node) || isRemoveControl(node)) return false;
            if (node.getAttribute('role') === 'progressbar' && progressPercent(node) >= 100) return false;
            const style = getComputedStyle(node);
            const animated = style.animationName && style.animationName !== 'none'
                && style.animationPlayState !== 'paused';
            const semantics = [
                node.getAttribute('role'), node.getAttribute('aria-label'),
                node.getAttribute('data-state'), node.className?.baseVal || node.className || ''
            ].filter(Boolean).join(' ').toLowerCase();
            const semanticBusy = /(progress|upload|loading|processing|pending|spinner|animate-spin)/.test(semantics);
            const svgAnimation = !!node.querySelector?.('animate, animateTransform');
            const tag = String(node.tagName || '').toLowerCase();
            const strokeDash = String(style.strokeDasharray || '').toLowerCase();
            const svgProgressRing = (tag === 'circle' || tag === 'svg') && (
                semanticBusy || animated || svgAnimation
                || (strokeDash && strokeDash !== 'none' && strokeDash !== '0px')
            );
            const conicRing = String(style.backgroundImage || '').includes('conic-gradient');
            return activelyBusy(node) && (semanticBusy || animated || svgAnimation || svgProgressRing || conicRing);
        };
        const indicatorSelector = busySelector + ', svg, circle, [style*="conic-gradient"]';
        const uploadRings = [...composer.querySelectorAll(indicatorSelector)].filter(isUploadRing);
        const busy = uploadRings.map(el =>
            el.getAttribute('aria-valuenow') || el.getAttribute('data-state')
            || el.getAttribute('aria-label') || el.tagName
        );
        const chipRecords = chipElements.map(el => {
            const child = el.querySelector('[data-file-name], [aria-label], [title]');
            const descriptor = [
                el.textContent, el.getAttribute('aria-label'), el.getAttribute('title'),
                el.getAttribute('data-file-name'), child?.getAttribute('data-file-name'),
                child?.getAttribute('aria-label'), child?.getAttribute('title')
            ].filter(Boolean).join(' ').trim();
            const localRings = [...el.querySelectorAll(indicatorSelector)].filter(isUploadRing);
            const progressValues = [el, ...el.querySelectorAll('[aria-valuenow], progress')]
                .map(node => progressPercent(node)).filter(value => value !== null);
            const stateText = [
                el.getAttribute('data-state'), el.getAttribute('aria-label'),
                el.className?.baseVal || el.className || ''
            ].filter(Boolean).join(' ').toLowerCase();
            return {
                descriptor,
                removable: !!(el.matches?.(removeControlSelector) || el.querySelector?.(removeControlSelector)),
                processing: localRings.length > 0,
                error: /(^|[\s_-])(error|failed|rejected)([\s_-]|$)/.test(stateText),
                explicit_complete: progressValues.some(value => value >= 100) ||
                    /(^|[\s_-])(complete|completed|success|ready|uploaded)([\s_-]|$)/.test(stateText),
                progress: progressValues,
            };
        }).filter(record => record.descriptor);
        return {
            text: composer.innerText || '', busy, upload_ring_count: uploadRings.length,
            chips: chipRecords.map(record => record.descriptor), chip_records: chipRecords,
            alerts, dialogs
        };
    }""", expected_names)


def composer_attachment_count(page) -> int:
    return int(page.evaluate("""() => {
        const send = document.querySelector('button[data-testid="send-button"], button[aria-label*="Send"], button[aria-label*="傳送"]');
        const composer = send?.closest('form') || send?.closest('[data-testid*="composer"]') ||
            document.querySelector('#prompt-textarea, div.ProseMirror[contenteditable="true"][role="textbox"]')?.closest('form') ||
            document.querySelector('form[data-type="unified-composer"], [data-testid="composer"]');
        if (!composer) return 0;
        const nodes = composer.querySelectorAll(
            '[data-testid*="attachment"], [data-testid="file-thumbnail"], '
            + '[data-testid="composer-file"], [data-file-name], '
            + 'button[aria-label*="Remove"], button[aria-label*="移除"], '
            + 'button[data-testid*="remove"], button[aria-label*="Delete"], button[aria-label*="刪除"]'
        );
        const selectedFiles = [...composer.querySelectorAll('input[type="file"]')]
            .reduce((count, input) => count + Number(input.files?.length || 0), 0);
        return nodes.length + selectedFiles;
    }""") or 0)


def clear_one_attachment(page) -> bool:
    result = page.evaluate("""() => {
        const send = document.querySelector('button[data-testid="send-button"], button[aria-label*="Send"], button[aria-label*="傳送"]');
        const composer = send?.closest('form') || send?.closest('[data-testid*="composer"]') ||
            document.querySelector('#prompt-textarea, div.ProseMirror[contenteditable="true"][role="textbox"]')?.closest('form') ||
            document.querySelector('form[data-type="unified-composer"], [data-testid="composer"]');
        if (!composer) return {clicked: false};
        const selectors = [
            'button[aria-label*="Remove"]', 'button[aria-label*="移除"]',
            'button[data-testid*="remove"]', 'button[aria-label*="Delete"]',
            'button[aria-label*="刪除"]'
        ];
        const button = selectors.map(selector => composer.querySelector(selector)).find(Boolean);
        if (button) button.click();
        const input = composer.querySelector('input[type="file"]');
        if (input) input.value = '';
        return {clicked: !!button};
    }""") or {}
    return isinstance(result, dict) and bool(result.get("clicked"))


__all__ = [
    "attach_button", "attach_menu_items", "attachment_dom_state",
    "clear_one_attachment", "composer_attachment_count", "file_inputs",
]
