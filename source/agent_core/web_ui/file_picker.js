data => {
 document.querySelector('#smartagent-file-picker')?.remove();
 window.__smartAgentFilePicker?.dispose?.();
 const host=document.createElement('div');host.id='smartagent-file-picker';
 host.style.cssText='all:initial;position:fixed;top:16px;right:16px;z-index:2147483647';
 const shadow=host.attachShadow({mode:'open'}),box=document.createElement('div');
 box.style.cssText='font:14px system-ui;background:#142238;color:#e6edf7;padding:18px;border:1px solid #6381a7;border-radius:12px;width:360px;max-width:90vw;overflow-wrap:anywhere';
 const title=document.createElement('h3');title.textContent=data.title;box.append(title);
 const help=document.createElement('p');help.textContent=data.help;box.append(help);
 for(const [color,text]of[['#fca5a5','選取不符'],['#fde68a','初步檢查通過，待功能驗證'],['#93e5c0','功能驗證通過']]){const p=document.createElement('div');p.style.color=color;p.textContent='● '+text;box.append(p);}
 const status=document.createElement('p');status.id='status';status.textContent='請按開始選取，再點選指定元件。';box.append(status);
 const outline=document.createElement('div');outline.style.cssText='position:fixed;pointer-events:none;border:3px solid #eab308;z-index:2147483646';document.documentElement.append(outline);
 let target=null,picking=false;
 const state=window.__smartAgentFilePicker={action:'',selection:null,phase:'waiting'};
 const label=(el)=>{for(const a of ['data-testid','aria-label','title','name','role']){const v=el.getAttribute(a);if(v&&!/DOMFILE_|DOMPROBE_|attachment_|\.txt/i.test(v))return el.tagName.toLowerCase()+'['+a+'='+JSON.stringify(v)+']';}if(el.id&&!/[a-f0-9]{12}|\d{5}/i.test(el.id))return '#'+CSS.escape(el.id);const classes=[...el.classList].filter(v=>!/[a-f0-9]{12}|\d{5}/i.test(v));return el.tagName.toLowerCase()+classes.slice(0,8).map(v=>'.'+CSS.escape(v)).join('');};
 const render=(el)=>{const r=el.getBoundingClientRect();outline.style.cssText+=';left:'+r.left+'px;top:'+r.top+'px;width:'+r.width+'px;height:'+r.height+'px';};
 const buttons={};
 state.update=(phase,text)=>{state.phase=phase;status.style.color={invalid:'#fca5a5',preflight:'#fde68a',verified:'#93e5c0'}[phase]||'#e6edf7';status.textContent=text;};
 state.finish=(text,passed)=>{state.update(passed?'verified':'invalid',text);buttons.cancel.textContent='關閉';for(const k of ['pick','expand','shrink','save'])buttons[k].disabled=true;};
 const choose=el=>{
  target=el;render(el);state.selection={selector:label(el),tag:el.tagName.toLowerCase()};
  let valid=true,reason='';
  const turnRoot=node=>node.closest(data.userRoot)||node.closest(data.assistantRoot);
  let root=document;
  if(data.stage===14){root=el.closest(data.assistantRoot);if(!root){valid=false;reason='請選本輪模型回答內的下載元件。';}}
  if((data.stage===11||data.stage===12)&&!el.matches('button,[role=button],[role=menuitem]')){valid=false;reason='請選附件入口／上傳選單按鈕。';}
  if(data.stage===13&&(turnRoot(el)||el.matches('button'))){valid=false;reason='請選輸入框附件卡片，需包含檔名及移除按鈕。';}
  if(data.stage===13&&!el.querySelector('button[aria-label*="remove" i],button[aria-label*="delete" i],button[aria-label*="\u79fb\u9664"],button[aria-label*="\u522a\u9664"],button[data-testid*="remove"]')){valid=false;reason='\u8acb\u64f4\u5927\u7bc4\u570d\u81f3\u5305\u542b\u6a94\u540d\u8207\u79fb\u9664\u6309\u9215\u7684\u5b8c\u6574\u9644\u4ef6\u5361\u7247\u3002';}
  const visibleMatches=s=>root?[...root.querySelectorAll(s)].filter(node=>node.getBoundingClientRect().height&&(data.stage!==13||!turnRoot(node))):[];
  if(root&&data.stage===12&&el.matches('button,[role="button"],[role="menuitem"]')&&visibleMatches(state.selection.selector).length!==1){
    let node=el,path='';
    for(let depth=0;node?.parentElement&&depth<5&&node!==root;depth++){
      const same=[...node.parentElement.children].filter(n=>n.tagName===node.tagName);
      const part=node.tagName.toLowerCase()+':nth-of-type('+(same.indexOf(node)+1)+')';path=part+(path?' > '+path:'');
      node=node.parentElement;const candidate=label(node)+' > '+path;
      const found=visibleMatches(candidate);if(found.length===1&&found[0]===el){state.selection.selector=candidate;break;}
    }
  }
  const matches=visibleMatches(state.selection.selector);
  if(matches.length!==1||matches[0]!==el){valid=false;reason='唯一性待確認：定位目前無法唯一對應；請調整範圍。';}
  state.update(valid?'preflight':'invalid',valid?'初步檢查通過，待功能驗證\n已選取：'+state.selection.selector+'；儲存後驗證功能。':reason);buttons.save.disabled=!valid;
 };
 const move=e=>{if(picking&&!e.composedPath().includes(host))render(e.target);};
 const click=e=>{if(!picking||e.composedPath().includes(host))return;e.preventDefault();e.stopImmediatePropagation();picking=false;choose(e.target.closest('button,[role="button"],[role="menuitem"],a,input')||e.target);};
 state.dispose=()=>{window.removeEventListener('pointerdown',panelEvent,true);window.removeEventListener('click',panelEvent,true);document.removeEventListener('pointermove',move,true);document.removeEventListener('click',click,true);outline.remove();};
 document.addEventListener('pointermove',move,true);document.addEventListener('click',click,true);
 for(const [id,text]of[['pick','開始選取'],['expand','擴大範圍'],['shrink','縮小範圍'],['save','儲存並測試'],['cancel','取消']]){const b=document.createElement('button');buttons[id]=b;b.id=id;b.textContent=text;b.style.cssText='padding:9px;margin:4px';box.append(b);b.onclick=()=>{if(id==='pick'){picking=true;state.update('waiting','請點選指定元件；綠框僅為預覽。');}else if(id==='expand'&&target?.parentElement)choose(target.parentElement);else if(id==='shrink'&&target?.firstElementChild)choose(target.firstElementChild);else if(id==='save'&&target){state.action='save';b.disabled=true;state.update('preflight','正在功能驗證，請稍候。');}else if(id==='cancel'){state.action='cancel';picking=false;window.removeEventListener('pointerdown',panelEvent,true);window.removeEventListener('click',panelEvent,true);document.removeEventListener('pointermove',move,true);document.removeEventListener('click',click,true);outline.remove();}};}buttons.save.disabled=true;
 const panelEvent=e=>{if(!e.composedPath().includes(host))return;e.preventDefault();e.stopImmediatePropagation();if(e.type==='click'){const b=e.composedPath().find(n=>n?.tagName==='BUTTON');if(b&&!b.disabled)b.onclick?.(e);}};
 window.addEventListener('pointerdown',panelEvent,true);window.addEventListener('click',panelEvent,true);
 shadow.append(box);document.documentElement.append(host);
}
