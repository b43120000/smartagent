"""Browser-native WebCopilot pre-resume decision modal.

The host owns recording and queuing. No model/tool invocation may be
inferred from DOM text, and the UI never directly executes commands.
"""
from __future__ import annotations

MODAL_ID = "smartagent-resume-approval-dialog"

_SHOW = r"""([approvalId, message]) => {
  const old = document.getElementById('smartagent-resume-approval-dialog');
  if (old && old.getAttribute('data-approval-id') === approvalId) return true;
  if (old) old.remove();
  const shade = document.createElement('div');
  shade.id = 'smartagent-resume-approval-dialog';
  shade.setAttribute('role','dialog');
  shade.setAttribute('aria-modal','true');
  shade.setAttribute('aria-label','SmartAgent 接續授權');
  shade.setAttribute('data-approval-id',approvalId);
  shade.style.cssText =
    'position:fixed;inset:0;z-index:2147483647;background:rgba(0,0,0,.7);'
    + 'display:flex;align-items:center;justify-content:center;pointer-events:auto;';
  const box = document.createElement('div');
  box.style.cssText =
    'background:#192026;color:#fff;width:min(640px,calc(100vw - 30px));'
    + 'padding:22px;border-radius:14px;box-shadow:0 6px 42px #000;'
    + 'font-family:system-ui,sans-serif;max-height:80vh;overflow:auto;';
  const title=document.createElement('h2');
  title.textContent='SmartAgent — 接續前先確認';
  title.style.cssText='margin:0 0 12px;font-size:19px;';
  const info=document.createElement('pre');
  info.textContent=message;
  info.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 monospace;';
  const note=document.createElement('p');
  note.textContent='核准首條指令；執行成功後，同一任務相同操作與結構（僅安全路徑不同）可免再次詢問。安全、Scope 與執行驗證不會跳過。';
  note.style.cssText='font-size:12px;opacity:.75;';
  const actions=document.createElement('div');
  actions.style.cssText='display:flex;gap:10px;justify-content:flex-end;';
  function make(label,choice,color){
    const button=document.createElement('button');
    button.type='button';button.textContent=label;
    button.style.cssText='background:'+color+';color:#fff;border:0;'
      +'border-radius:8px;padding:10px 14px;cursor:pointer;font-weight:600;';
    button.addEventListener('click',event=>{
      if (!event.isTrusted || shade.dataset.choice) return;
      shade.dataset.choice=choice;
      shade.dataset.clicked='1';
      button.disabled=true;
      for(const child of actions.querySelectorAll('button'))child.disabled=true;
      note.textContent='已選擇，正在記錄並建立接續任務…';
    });
    return button;
  }
  actions.append(make('拒絕','DENIED','#6b7280'));
  actions.append(make('允許本任務同類指令','APPROVED','#2563eb'));
  box.append(title,info,note,actions);
  shade.append(box);
  document.body.append(shade);
  return true;
}"""

_POLL = r"""approvalId => {
  const node = document.getElementById('smartagent-resume-approval-dialog');
  if (!node || node.dataset.approvalId !== approvalId) return null;
  return node.dataset.clicked === '1'
    && ['APPROVED','DENIED'].includes(node.dataset.choice)
      ? node.dataset.choice : null;
}"""

_CLOSE = r"""approvalId => {
  const node = document.getElementById('smartagent-resume-approval-dialog');
  if (node && node.dataset.approvalId === approvalId) node.remove();
}"""


def show_resume_dialog(page, challenge: dict, message: str) -> bool:
    return bool(page.evaluate(_SHOW, [str(challenge["approval_id"]), str(message)]))


def poll_resume_dialog(page, approval_id: str) -> str | None:
    result = page.evaluate(_POLL, str(approval_id))
    return result if result in {"APPROVED", "DENIED"} else None


def close_resume_dialog(page, approval_id: str) -> None:
    page.evaluate(_CLOSE, str(approval_id))


__all__ = ["show_resume_dialog", "poll_resume_dialog", "close_resume_dialog"]
