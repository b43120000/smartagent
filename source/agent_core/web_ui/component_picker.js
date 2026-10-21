(options => {
  if (window.top !== window) return;
  if (window.__smartAgentComponentPicker) return;
  const state = window.__smartAgentComponentPicker = {action:'',selected:null,revision:0,phase:'waiting'};
  let armed=false, selected=null;
  const field=options.field, turnMode=['user_turn','assistant_turn'].includes(field);
  state.field=field;state.marker=options.marker;
  const host=document.createElement('div'); host.id='smartagent-component-picker';
  host.style.cssText='all:initial;position:fixed;top:16px;right:16px;z-index:2147483647';
  const ui=host.attachShadow({mode:'open'});
  const node1=document.createElement('style');
  ui.append(node1);
  node1.append(document.createTextNode('\n  .panel{font:14px system-ui;color:#e6edf7;width:340px;background:#142238;border:1px solid #6381a7;border-radius:12px;padding:16px;box-shadow:0 5px 24px #0006}\n  h3{margin:0 0 10px}p{line-height:1.6;overflow-wrap:anywhere}button{font:inherit;border:0;border-radius:6px;padding:8px 10px;margin:4px;cursor:pointer;background:#dbeafe;color:#142238}button:disabled{opacity:.4}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:100px;overflow:auto;font:12px monospace}.status{color:#93e5c0}\n  '));
  const node2=document.createElement('div');
  node2.setAttribute('class','panel');
  ui.append(node2);
  const node3=document.createElement('h3');
  node2.append(node3);
  const node4=document.createElement('p');
  node4.setAttribute('id','instructions');
  node2.append(node4);
  const node5=document.createElement('p');
  node5.setAttribute('id','status');
  node5.setAttribute('class','status');
  node2.append(node5);
  node5.append(document.createTextNode('等待開始選取'));
  const checks=document.createElement('div');checks.id='checks';checks.style.cssText='font:13px system-ui;line-height:1.6;overflow-wrap:anywhere;max-height:170px;overflow:auto';node2.append(checks);
  const node6=document.createElement('pre');
  node6.setAttribute('id','preview');
  node2.append(node6);
  node2.append(document.createTextNode('\n  '));
  const node7=document.createElement('button');
  node7.setAttribute('id','pick');
  node2.append(node7);
  node7.append(document.createTextNode('開始選取'));
  const node8=document.createElement('button');
  node8.setAttribute('id','parent');
  node8.setAttribute('disabled','');
  node2.append(node8);
  node8.append(document.createTextNode('擴大範圍'));
  const node9=document.createElement('button');
  node9.setAttribute('id','child');
  node9.setAttribute('disabled','');
  node2.append(node9);
  node9.append(document.createTextNode('縮小範圍'));
  const node10=document.createElement('button');
  node10.setAttribute('id','save');
  node10.setAttribute('disabled','');
  node2.append(node10);
  node10.append(document.createTextNode('儲存並測試'));
  const node11=document.createElement('button');
  node11.setAttribute('id','cancel');
  node2.append(node11);
  node11.append(document.createTextNode('取消'));
  ui.querySelector('h3').textContent=options.title;
  ui.getElementById('instructions').textContent=options.instructions;
  ui.getElementById('instructions').style.whiteSpace='pre-line';
  if(options.showStateLegend){
    const legend=document.createElement('div');legend.id='state-legend';legend.style.cssText='font-size:12px;line-height:1.7';
    for(const [color,label] of [['#fca5a5','選取不符'],['#fde68a','初步檢查通過，待功能驗證'],['#93e5c0','功能驗證通過']]){
      const row=document.createElement('div');row.style.color=color;row.textContent='● '+label;legend.append(row);
    }node2.insertBefore(legend,node5);
  }
  const outline=document.createElement('div');
  outline.style.cssText='position:fixed;pointer-events:none;border:3px solid #22c55e;border-radius:5px;z-index:2147483646;display:none;box-sizing:border-box';
  document.documentElement.append(host,outline);
  const status=t=>ui.getElementById('status').textContent=t;
  function validation(phase, rows=[], message='') {
    state.phase=phase;
    const labels={invalid:'選取不符',preflight:'初步檢查通過，待功能驗證',verified:'功能驗證通過'};
    const colors={invalid:'#fca5a5',preflight:'#fde68a',verified:'#93e5c0'};
    ui.getElementById('status').style.color=colors[phase]||'#e6edf7';
    status((labels[phase]||message)+(labels[phase]&&message?'\n'+message:''));
    ui.getElementById('status').style.whiteSpace='pre-line';
    checks.replaceChildren();
    for(const row of rows){const line=document.createElement('div');line.textContent=(row.passed===true?'✓ ':row.passed===false?'✗ ':'… ')+row.label+'：'+row.detail;line.style.color=row.passed===false?'#fca5a5':'#cbd5e1';checks.append(line);}
    outline.style.borderColor=phase==='invalid'?'#ef4444':phase==='preflight'?'#f59e0b':'#22c55e';
  }
  function reject(message){state.revision++;state.selected=null;state.action='';selected=null;ui.getElementById('save').disabled=true;validation('invalid',[{label:'選取',passed:false,detail:message}]);show(null);}
  state.getSelectedElement=()=>selected;
  state.applyPreflight=(result,revision)=>{
    if(revision!==state.revision||!state.selected||state.action==='cancel')return false;
    state.validation=result;
    validation(result.passed?'preflight':'invalid',result.checks);
    ui.getElementById('save').disabled=!result.passed;
    if(!result.passed)state.action='';
    return true;
  };
  state.commitSelection=revision=>{
    if(revision!==state.revision||state.action!=='save'||!state.validation?.passed||!selected?.isConnected)return false;
    if(options.keepVisibleOnCommit){
      armed=false;show(null);validation('preflight',[],'已確認選取。正在執行功能驗證，請稍候；不會自動重送。');
      for(const id of ['pick','parent','child','save'])ui.getElementById(id).disabled=true;
    }else state.hide();
    return true;
  };
  const visible=e=>e?.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
  const attr=(n,v)=>`[${n}=${JSON.stringify(v)}]`;
  const root=()=> ['user_content','assistant_content'].includes(field) ? [...document.querySelectorAll(options.rootSelector)].find(e=>e.textContent.includes(options.marker)&&(field==='assistant_content'?!e.textContent.includes('SmartAgent DOM calibration'):e.textContent.includes('SmartAgent DOM calibration'))) : document;
  const stable=v=>v&&v.length<150&&!/[a-f0-9]{12,}|\d{5,}|DOMPROBE_|fallback-turn|^_r_|^radix-|\[object Object\]/i.test(v);
  function candidates(el) {
    const tag=el.tagName.toLowerCase(), xs=[];
    if(options.preferredTurnSelector&&el.matches(options.preferredTurnSelector))xs.push(options.preferredTurnSelector);
    if(['send','stop','blocking_state'].includes(field)&&stable(el.id))xs.push('#'+CSS.escape(el.id));
    for(const name of ['data-message-author-role','data-role','data-testid','data-test-id','data-cds','data-qa','data-content','aria-label','name']) {
      const value=el.getAttribute(name);
      if(stable(value)&&!(turnMode&&/\d/.test(value)))xs.push(tag+attr(name,value));
    }
    const unit=el.getAttribute('data-content-search-unit-key');
    if(unit)for(const role of ['user','assistant'])if(unit.endsWith(':'+role))xs.unshift(tag+`[data-content-search-unit-key$=':${role}']`);
    for(const cls of el.classList)if(stable(cls)&&!/^(f[a-z0-9]{6,}$|___)/i.test(cls))xs.push(tag+'.'+CSS.escape(cls));
    if(!turnMode)xs.push(tag);
    return xs;
  }
  function locate(el) {
    const scope=root();
    if(!scope||!scope.contains(el))throw Error(field==='assistant_content'?'請點本輪模型回答內的完整正文。':'請點剛送出的使用者訊息內的正文。');
    // Some sites use the text element itself as the calibrated user turn.
    if(['user_content','assistant_content'].includes(field)&&scope===el)return ':scope';
    const acceptable=selector=>{
      try {const nodes=[...scope.querySelectorAll(selector)];return nodes.includes(el)&&(turnMode||nodes.length===1);}catch{return false;}
    };
    for(const selector of candidates(el))if(acceptable(selector))return selector;
    for(let parent=el.parentElement,depth=0;parent&&parent!==scope&&depth<5;parent=parent.parentElement,depth++) {
      for(const prefix of candidates(parent))for(const suffix of candidates(el)) {
        const selector=prefix+' '+suffix;if(acceptable(selector))return selector;
      }
    }
    throw Error('無法建立穩定定位，請調整選取範圍。');
  }
  function resolve(target) {
    if(!(target instanceof Element)||target.closest('#smartagent-component-picker'))return null;
    if(['send','stop'].includes(field))return target.closest('button,[role="button"],input[type="submit"]');
    if(field==='blocking_state')return target.closest('[role="alert"],[role="dialog"]')||target.closest('div,p,section,article');
    if(options.contentRootSelector)return target.closest(options.contentRootSelector)||target.closest('p,div,span,article,section,li');
    if(target.closest('[contenteditable="true"],textarea,input'))return null;
    if(options.turnRootSelector&&document.querySelector(options.turnRootSelector))return target.closest(options.turnRootSelector);
    if(turnMode){
      const role=field==='user_turn'?'user':'assistant';
      for(let el=target;el&&el!==document.body;el=el.parentElement) {
        // Content styling (e.g. assistant-message markdown) is not a turn
        // boundary. Prefer the role/unit root that retains message identity.
        if(el.getAttribute('data-message-author-role')===role||el.getAttribute('data-role')===role||
           el.getAttribute('data-content-search-unit-key')?.endsWith(':'+role))return el;
      }
      for(let el=target;el&&el!==document.body;el=el.parentElement) {
        if((el.getAttribute('data-testid')||'').toLowerCase().includes(role))return el;
      }
    }
    return target.closest('p,div,span,article,section,li')||target;
  }
  function show(el){
    if(!el){outline.style.display='none';return;}
    const r=el.getBoundingClientRect();Object.assign(outline.style,{display:'block',left:r.left+'px',top:r.top+'px',width:r.width+'px',height:r.height+'px'});
  }
  function choose(el) {
    if(!el)throw Error(field==='assistant_turn'?'請選本輪模型回答外框，不要選使用者問題或其他區塊。':field==='user_turn'?'請選本輪使用者訊息外框，不要選模型回答或其他區塊。':'目前位置不是預期元件，請重新選取。');
    if(!visible(el)||['BODY','HTML','MAIN'].includes(el.tagName))throw Error('請選單則訊息或指定按鈕，不要選整個頁面。');
    if(['send','stop'].includes(field)&&(!el.matches('button,[role="button"],input[type="submit"]')||el.disabled||el.getAttribute('aria-disabled')==='true'))throw Error('請選可用的指定按鈕。');
    const selector=locate(el);selected=el;armed=false;
    state.revision++;state.action='';state.validation=null;
    state.selected={selector,tag:el.tagName.toLowerCase(),kind:field};
    ui.getElementById('save').disabled=!!options.preflightBridge;ui.getElementById('parent').disabled=['send','stop'].includes(field);ui.getElementById('child').disabled=['send','stop'].includes(field);
    ui.getElementById('preview').textContent=selector+'\n'+(el.innerText||'').slice(0,180);
    validation('checking',[],'已選取，正在初步檢查…');show(el);
  }
  ui.getElementById('pick').onclick=()=>{armed=true;state.revision++;state.selected=null;selected=null;state.validation=null;state.action='';ui.getElementById('save').disabled=true;ui.getElementById('pick').textContent='重新選取';validation('waiting',[],'請點選指定元件；Esc 可取消選取。');show(null);};
  ui.getElementById('parent').onclick=()=>{try{choose(selected?.parentElement);}catch(e){reject(e.message);}};
  ui.getElementById('child').onclick=()=>{try{const child=[...selected.children].find(e=>visible(e)&&e.textContent.includes(options.marker));choose(child);}catch(e){reject(e.message);}};
  document.addEventListener('pointermove',event=>{if(armed&&!event.composedPath().includes(host))show(resolve(event.target));},true);
  for(const type of ['pointerdown','mousedown','mouseup','pointerup','click'])window.addEventListener(type,event=>{
    if(!armed||event.composedPath().includes(host))return;
    event.preventDefault();event.stopImmediatePropagation();
    if(type==='click')try{choose(resolve(event.target));}catch(e){reject(e.message);}
  },true);
  window.addEventListener('keydown',event=>{if(armed&&event.key==='Escape'){armed=false;show(null);status('已停止選取。');}},true);
  ui.getElementById('save').onclick=()=>{
    if(!selected?.isConnected){reject('元件已更新，請重新選取。');return;}
    if(options.preflightBridge&&!state.validation?.passed)return;
    try {if(locate(selected)!==state.selected.selector)throw Error('定位已更新，請重新選取。');state.action='save';armed=false;status('初步檢查通過，待功能驗證\n正在再次確認選取狀態…');ui.getElementById('save').disabled=true;}catch(e){reject(e.message);}
  };
  ui.getElementById('cancel').onclick=()=>{armed=false;state.revision++;state.action='cancel';};
  state.hide=()=>{armed=false;host.style.display='none';show(null);};
  state.finish=message=>{armed=false;state.revision++;show(null);host.style.display='block';validation('verified',[],message);for(const id of ['pick','save','parent','child'])ui.getElementById(id).disabled=true;ui.getElementById('cancel').textContent='關閉';};
  state.fail=message=>{armed=false;state.revision++;show(null);host.style.display='block';validation('invalid',[],message);for(const id of ['pick','save','parent','child'])ui.getElementById(id).disabled=true;ui.getElementById('cancel').textContent='關閉';};
})
