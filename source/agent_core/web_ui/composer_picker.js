// First milestone: one editable element, selected with a real pointer click.
(() => {
  if (window.top !== window) return;
  const install = () => {
  if (window.__smartAgentComposerPicker) return;
  const state = window.__smartAgentComposerPicker = {action: '', selected: null, phase:'waiting'};
  let armed = false, selectedElement = null;
  const host = document.createElement('div');
  host.id = 'smartagent-composer-picker';
  host.style.cssText = 'all:initial;position:fixed;top:16px;right:16px;z-index:2147483647;';
  const ui = host.attachShadow({mode: 'open'});
  const node1=document.createElement('style');
  ui.append(node1);
  node1.append(document.createTextNode('\n    :host{font:14px system-ui;color:#e6edf7} .panel{font:14px system-ui;color:#e6edf7;width:320px;background:#142238;border:1px solid #6381a7;border-radius:12px;padding:16px;box-shadow:0 5px 24px #0006}\n    h3{margin:0 0 10px} p{line-height:1.6;margin:8px 0;overflow-wrap:anywhere} button{font:inherit;border:0;border-radius:6px;padding:8px 12px;margin:5px 4px 0 0;cursor:pointer;background:#dbeafe;color:#142238} button:disabled{opacity:.4;cursor:default} pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px monospace;max-height:110px;overflow:auto} .status{color:#93e5c0}\n  '));
  const node2=document.createElement('div');
  node2.setAttribute('class','panel');
  ui.append(node2);
  const node3=document.createElement('h3');
  node2.append(node3);
  node3.append(document.createTextNode('手動標記：輸入框（1 / 1）'));
  node2.append(document.createTextNode('\n    '));
  const node4=document.createElement('p');
  node2.append(node4);
  node4.append(document.createTextNode('先完成登入，再按「開始選取」，點一下網頁中的訊息輸入框。點到框內文字也可以。'));
  node2.append(document.createTextNode('\n    '));
  const node5=document.createElement('p');
  node5.setAttribute('class','status');
  node5.setAttribute('id','status');
  node2.append(node5);
  node5.append(document.createTextNode('等待開始選取'));
  const node6=document.createElement('pre');
  node6.setAttribute('id','preview');
  node2.append(node6);
  node2.append(document.createTextNode('\n    '));
  const node7=document.createElement('button');
  node7.setAttribute('id','pick');
  node2.append(node7);
  node7.append(document.createTextNode('開始選取'));
  const node8=document.createElement('button');
  node8.setAttribute('id','save');
  node8.setAttribute('disabled','');
  node2.append(node8);
  node8.append(document.createTextNode('儲存輸入框'));
  const node9=document.createElement('button');
  node9.setAttribute('id','cancel');
  node2.append(node9);
  node9.append(document.createTextNode('取消'));
  const outline = document.createElement('div');
  outline.style.cssText = 'position:fixed;pointer-events:none;border:3px solid #22c55e;border-radius:5px;z-index:2147483646;display:none;box-sizing:border-box;';
  document.documentElement.append(host, outline);
  const status = text => ui.getElementById('status').textContent = text;
  function validation(phase,message){
    state.phase=phase;
    const labels={invalid:'選取不符',preflight:'初步檢查通過，待功能驗證',verified:'功能驗證通過'};
    const colors={invalid:'#fca5a5',preflight:'#fde68a',verified:'#93e5c0'};
    const line=ui.getElementById('status');line.style.whiteSpace='pre-line';line.style.color=colors[phase]||'#e6edf7';
    status((labels[phase]||'')+(message?'\n'+message:''));
  }
  function reject(message){state.action='';state.selected=null;selectedElement=null;ui.getElementById('save').disabled=true;validation('invalid',message);show(null);}
  const visible = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const isEditable = el => el && !el.disabled && !el.readOnly && (
    el.matches('textarea') || el.matches('input:not([type]),input[type="text"],input[type="search"]') || el.isContentEditable
  );
  function editable(target) {
    if (!(target instanceof Element)) return null;
    let el = target.closest('textarea,input,[contenteditable]');
    if (el && el.isContentEditable) while (el.parentElement?.isContentEditable) el = el.parentElement;
    if (isEditable(el)) return el;
    if (!el) {
      const wrapper = target.closest('[role="textbox"]');
      const candidates = wrapper ? [...wrapper.querySelectorAll('textarea,input,[contenteditable]')].filter(isEditable) : [];
      if (candidates.length === 1) return candidates[0];
    }
    return null;
  }
  const attr = (name, value) => `[${name}=${JSON.stringify(value)}]`;
  function candidates(el) {
    const tag = el.tagName.toLowerCase(), values = [];
    const stable = value => value && value.length < 160 && !/[a-f0-9]{16,}|\d{8,}/i.test(value);
    if (stable(el.id)) values.push('#' + CSS.escape(el.id));
    for (const name of ['data-testid','data-test-id','data-qa','name','aria-label','placeholder']) {
      const value = el.getAttribute(name);
      if (stable(value)) values.push(tag + attr(name, value));
    }
    if (el.hasAttribute('contenteditable')) {
      const base = tag + attr('contenteditable', el.getAttribute('contenteditable'));
      if (el.getAttribute('role')) values.push(base + attr('role', el.getAttribute('role')));
      values.push(base);
    }
    for (const cls of el.classList) if (stable(cls)) values.push(tag + '.' + CSS.escape(cls));
    values.push(tag);
    return values;
  }
  function selectorFor(el) {
    const own = candidates(el);
    const unique = selector => {
      try { const found = document.querySelectorAll(selector); return found.length === 1 && found[0] === el; }
      catch { return false; }
    };
    for (const selector of own) if (unique(selector)) return selector;
    for (let parent = el.parentElement, depth = 0; parent && depth < 4; parent = parent.parentElement, depth++) {
      for (const prefix of candidates(parent)) for (const suffix of own) {
        const selector = prefix + ' ' + suffix;
        if (unique(selector)) return selector;
      }
    }
    throw new Error('無法產生唯一且可重複使用的定位方式，請重新選取。');
  }
  function show(el) {
    if (!el) { outline.style.display = 'none'; return; }
    const rect = el.getBoundingClientRect();
    Object.assign(outline.style, {display:'block',left:rect.left+'px',top:rect.top+'px',width:rect.width+'px',height:rect.height+'px'});
  }
  ui.getElementById('pick').onclick = () => {
    armed = true; selectedElement = null; state.selected = null;
    state.action='';state.phase='waiting';
    ui.getElementById('save').disabled = true;
    ui.getElementById('pick').textContent = '重新選取';
    ui.getElementById('preview').textContent = '';
    validation('waiting','請點一下訊息輸入框；Esc 可取消選取'); show(null);
  };
  document.addEventListener('pointermove', event => {
    if (armed && !event.composedPath().includes(host)) show(editable(event.target));
  }, true);
  // Block the page's original action while selecting, including pointerdown focus handlers.
  for (const name of ['pointerdown','mousedown','mouseup','pointerup','click']) {
    window.addEventListener(name, event => {
      if (!armed || event.composedPath().includes(host)) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (name !== 'click') return;
      const el = editable(event.target);
      if (!el || !visible(el)) { reject('這不是可輸入元件，請點訊息輸入框。'); return; }
      if (el.getRootNode() !== document) { reject('目前只支援主頁面的輸入框。'); return; }
      try {
        const selector = selectorFor(el);
        selectedElement = el;
        state.selected = {selector, tag:el.tagName.toLowerCase(), kind:el.isContentEditable ? 'contenteditable' : 'input'};
        armed = false; show(el);
        ui.getElementById('save').disabled = false;
        ui.getElementById('preview').textContent = selector;
        validation('preflight','✓ 元件可見且可輸入\n✓ 定位唯一且對應點選元件\n仍須實際輸入與讀回測試，才算功能驗證通過。');
      } catch (error) { reject(error.message); }
    }, true);
  }
  window.addEventListener('keydown', event => {
    if (armed && event.key === 'Escape') { armed = false; show(null); status('已停止選取，可重新開始。'); }
  }, true);
  ui.getElementById('save').onclick = () => {
    if (!selectedElement?.isConnected || !state.selected) { reject('元件已更新，請重新選取。'); return; }
    const found = document.querySelectorAll(state.selected.selector);
    if (found.length !== 1 || found[0] !== selectedElement || !isEditable(selectedElement) || !visible(selectedElement)) {
      reject('元件已改變，請重新選取。'); return;
    }
    armed = false; state.action = 'save'; status('正在儲存…'); ui.getElementById('save').disabled = true;
  };
  ui.getElementById('cancel').onclick = () => { armed = false; state.action = 'cancel'; };
  state.finish = (message,verified=false,failed=false) => { armed = false; show(null);host.style.display='block';validation(failed?'invalid':verified?'verified':'preflight',message);ui.getElementById('save').disabled=true;ui.getElementById('pick').disabled = true; ui.getElementById('cancel').textContent = '關閉'; };
  state.hide = () => { armed = false; host.style.display = 'none'; show(null); };
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, {once:true});
  else install();
})();
