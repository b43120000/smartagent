"""Scoped, fail-closed evidence used while calibrating composer attachments."""


def install(page, input_selector):
    """Start observing before selecting a file; keep only factual DOM events."""
    page.evaluate("""selector => {
      const stamp=()=>({t:Date.now(),send:(document.querySelector('#send,[type=submit],[data-testid*=send]')||{}).disabled === false});
      const inputFiles=()=>Array.from(document.querySelectorAll(selector)).flatMap(i=>Array.from(i.files||[]).map(f=>({name:f.name,size:f.size,type:f.type,lastModified:f.lastModified})));
      const busy='[aria-busy="true"],[role="progressbar"],[data-state="uploading"],[data-state="processing"]';
      const selectedFiles=[];
      document.addEventListener('change',event=>{const input=event.target;if(input?.matches?.(selector))selectedFiles.splice(0,selectedFiles.length,...Array.from(input.files||[]).map(f=>({name:f.name,size:f.size,type:f.type,lastModified:f.lastModified})));},true);
      document.addEventListener('input',event=>{const input=event.target;if(input?.matches?.(selector)&&input.files?.length)selectedFiles.splice(0,selectedFiles.length,...Array.from(input.files).map(f=>({name:f.name,size:f.size,type:f.type,lastModified:f.lastModified})));},true);
      window.__smartAgentAttachmentProbe={events:[stamp()], inputFiles,selectedFiles,tracked:null,busySelector:'',sawBusy:false,busyEvents:[]};
      new MutationObserver(records=>{const p=window.__smartAgentAttachmentProbe;p.events.push(stamp());for(const r of records){const n=r.target.nodeType===1?r.target:r.target.parentElement;const host=n?.closest?.('div,li,section,article')||n;if(host&&((host.matches?.(busy))||host.querySelector?.(busy)))p.busyEvents.push(String(host.innerText||host.getAttribute?.('aria-label')||''));}if(p.tracked&&p.busySelector&&p.tracked.matches(p.busySelector))p.sawBusy=true;})
        .observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:['aria-busy','aria-invalid','data-state','data-upload-state','disabled','value']});
    }""", input_selector)


def input_files(page):
    return page.evaluate("""()=>window.__smartAgentAttachmentProbe?.inputFiles?.()||[]""")


def selected_files(page):
    """The browser event proves selection even when React subsequently clears FileList."""
    return page.evaluate("""()=>window.__smartAgentAttachmentProbe?.selectedFiles||[]""")


def observed_send_transition(page):
    events=page.evaluate("""()=>window.__smartAgentAttachmentProbe?.events||[]""")
    values=[event.get('send') for event in events]
    return False in values and True in values[values.index(False)+1:] if False in values else False


def track_chip(page, chip, busy_selector):
    chip.evaluate("""(node, selector)=>{const p=window.__smartAgentAttachmentProbe;p.tracked=node;p.busySelector=selector;if(node.matches(selector))p.sawBusy=true;}""", busy_selector)


def observed_busy(page, filename=''):
    return bool(page.evaluate("""name=>{const p=window.__smartAgentAttachmentProbe;return Boolean(p&&(p.sawBusy||(name&&p.busyEvents&&p.busyEvents.some(function(value){return value.includes(name);}))))}""", filename))
