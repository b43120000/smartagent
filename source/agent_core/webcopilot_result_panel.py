"""Isolated DOM presentation for a *view* of the authoritative Tri-One result.

No synthetic assistant turns, no HTML built from task content, no DOM scraping
or re-submission. The result is rendered only in a WebCopilot-owned shadow DOM.
"""
from __future__ import annotations

from collections.abc import Mapping

_SCRIPT = r"""
(view) => {
  if (!view || view.schema !== 'SMARTAGENT_WEBCOPILOT_RESULT_VIEW_V1') {
    return false;
  }
  let host = document.querySelector('[data-smartagent-webcopilot-result-panel]');
  if (!host) {
    host = document.createElement('div');
    host.setAttribute('data-smartagent-webcopilot-result-panel', '1');
    host.setAttribute('data-smartagent-private-ui', '1');
    host.style.cssText = 'position:fixed;z-index:2147483645;right:20px;bottom:76px;max-width:calc(100vw - 32px);pointer-events:auto;';
    document.body.appendChild(host);
  }
  const root = host.shadowRoot || host.attachShadow({mode:'open'});
  while (root.firstChild) root.removeChild(root.firstChild);
  const style = document.createElement('style');
  style.textContent = [
    ':host{color-scheme:light dark;}',
    '.card{box-sizing:border-box;width:min(480px, calc(100vw - 32px));max-height:68vh;overflow:auto;',
    'border:1px solid #64748b66;border-radius:14px;background:#17212e;color:#f8fafc;',
    'box-shadow:0 12px 42px rgba(0,0,0,.35);font:14px/1.55 system-ui,sans-serif;}',
    '.head{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;',
    'gap:12px;border-bottom:1px solid #64748b66;}',
    '.title{font-size:16px;font-weight:700;margin:0;min-width:0;overflow-wrap:anywhere;}',
    '.close{flex-shrink:0;color:#f8fafc;background:transparent;border:1px solid #94a3b8;',
    'border-radius:8px;cursor:pointer;padding:2px 9px;font-size:16px;}',
    '.body{padding:12px 16px 16px;overflow-wrap:anywhere;}',
    '.summary{margin:0 0 12px;white-space:pre-wrap;}',
    '.note{font-size:12px;color:#cbd5e1;border-top:1px solid #64748b66;',
    'padding-top:9px;margin-top:9px;}',
    '.meta{font-size:12px;color:#cbd5e1;margin-top:6px;overflow-wrap:anywhere;}',
    '.badge{font-size:11px;font-weight:700;letter-spacing:.03em;margin-right:6px;}',
    '.success{color:#6ee7b7}.warning{color:#fcd34d}.error{color:#fda4af}.info{color:#93c5fd}',
    '.evidence{font-size:12px;margin-top:10px;padding:8px;background:#33415599;border-radius:8px;}',
    'p{margin:0 0 10px;}ul{margin:0 0 10px;padding-left:20px;}li{margin:4px 0;}'
  ].join('');
  root.appendChild(style);
  const card = document.createElement('section');
  card.className = 'card';
  card.setAttribute('role','status');
  card.setAttribute('aria-live','polite');
  const head = document.createElement('header');
  head.className = 'head';
  const title = document.createElement('h3');
  title.className = 'title';
  title.textContent = String(view.title || '任務結果');
  head.appendChild(title);
  const close = document.createElement('button');
  close.type = 'button'; close.className = 'close'; close.textContent = '×';
  close.setAttribute('aria-label', '關閉任務結果');
  close.addEventListener('click', () => host.remove());
  head.appendChild(close);
  card.appendChild(head);
  const body = document.createElement('div');
  body.className = 'body';
  const badge = document.createElement('div');
  badge.className = 'badge ' + (
    ['success','warning','error','info'].includes(view.severity) ?
      view.severity : 'info');
  badge.textContent = view.verified === true ? '已驗收' : '尚未驗收';
  body.appendChild(badge);

  // Render explanatory paragraphs and simple bullets; textContent only.
  const summary = String(view.summary || '').slice(0, 3601);
  let list = null;
  for (const line of summary.split(/\n/)) {
    const item = line.trim();
    if (/^[-*•]\s+/.test(item)) {
      if (!list) { list = document.createElement('ul'); body.appendChild(list); }
      const li = document.createElement('li');
      li.textContent = item.replace(/^[-*•]\s+/, '');
      list.appendChild(li);
    } else if (item) {
      list = null;
      const p = document.createElement('p');
      p.textContent = item;
      body.appendChild(p);
    } else {
      list = null;
    }
  }
  if (Number.isInteger(view.evidence_count) && view.evidence_count > 0) {
    const e = document.createElement('div');
    e.className = 'evidence';
    e.textContent = 'Runtime Evidence：' + view.evidence_count + ' 筆' +
       (Array.isArray(view.evidence_refs) && view.evidence_refs.length ?
          '\n' + view.evidence_refs.join('、') : '');
    e.style.whiteSpace = 'pre-wrap';
    body.appendChild(e);
  }
  const note = document.createElement('div');
  note.className = 'note';
  note.textContent = String(view.note || '');
  body.appendChild(note);
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = 'Task ID：' + String(view.task_id || '') +
      ' · ' + String(view.source || 'UNVERIFIED');
  body.appendChild(meta);
  card.appendChild(body);
  root.appendChild(card);
  return true;
}
"""


def show_webcopilot_result(page, view: Mapping) -> bool:
    """Render safely; DOM failures do not change execution or delivery state."""
    if not isinstance(view, Mapping):
        return False
    if view.get("schema") != "SMARTAGENT_WEBCOPILOT_RESULT_VIEW_V1":
        return False
    try:
        return page.evaluate(_SCRIPT, dict(view)) is True
    except Exception:
        return False


__all__ = ["show_webcopilot_result"]
