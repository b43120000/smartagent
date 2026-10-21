"""Read message content without duplicated accessibility-only labels."""

def read_message_text(element) -> str:
    value = element.evaluate(r"""el => {
        const excluded = '[aria-hidden="true"],[hidden],.cdk-visually-hidden,.sr-only,.visually-hidden,.screen-reader-only';
        if (!el.querySelector(excluded)) return el.innerText || '';
        const clone = el.cloneNode(true);
        for (const node of clone.querySelectorAll(excluded)) node.remove();
        for (const br of clone.querySelectorAll('br')) br.replaceWith('\n');
        for (const block of clone.querySelectorAll('p,div,li,h1,h2,h3,h4,h5,h6,pre')) block.append('\n');
        return clone.textContent || '';
    }""")
    # Non-browser contract doubles may not implement this read-only evaluation.
    if value is None:
        value = element.inner_text()
    return str(value).replace('\r\n', '\n').strip()


def read_answer_text(element, *, copilot_code_ui=False) -> str:
    """Serialize content; M365 can remove its positively identified code UI."""
    script=r'''(el, copilotCodeUI)=>{
        const clone=el.cloneNode(true);
        if(copilotCodeUI){
            // Only the site's dismissible unsupported-language notice proves
            // this is a smartagent_tool code widget. Never strip arbitrary prose.
            const noticePattern=/^smartagent_tool\s+並沒有完全支援[。.]\s*語法標示是基於\s+([A-Za-z][A-Za-z0-9+#. -]{0,40})[。.]$/;
            const compact=node=>{
                const copy=node.cloneNode(true);
                for(const n of copy.querySelectorAll('button,[role="button"],[aria-hidden="true"]'))n.remove();
                return (copy.textContent||'').replace(/\s+/g,' ').trim();
            };
            for(const pre of [...clone.querySelectorAll('pre')]){
                const code=pre.querySelector('code');
                if(!code)continue;
                let wrapper=pre.parentElement,found=null;
                for(let depth=0;wrapper&&depth<6;depth++,wrapper=wrapper.parentElement){
                    if(wrapper.querySelectorAll('pre').length!==1)break;
                    const notices=[...wrapper.querySelectorAll('*')].filter(n=>
                        !n.matches('pre,code')&&!n.closest('pre,code')&&!n.querySelector('pre,code')&&
                        (n.matches('[role="alert"],.fui-MessageBar')||n.querySelector('button,[role="button"]'))&&
                        noticePattern.test(compact(n)));
                    if(notices.length){found={wrapper,notices};break;}
                }
                if(!found)continue;
                const languages=new Set(found.notices.map(n=>compact(n).match(noticePattern)[1]));
                for(const notice of found.notices)notice.remove();
                for(const node of [...found.wrapper.querySelectorAll('span,header,div')]){
                    if(!node.closest('pre,code')&&!node.querySelector('pre,code')&&
                       languages.has((node.textContent||'').trim())&&
                       (node.compareDocumentPosition(pre)&Node.DOCUMENT_POSITION_FOLLOWING))node.remove();
                }
                // Restore the protocol label the widget demoted to a UI notice.
                // JSON bytes/order are untouched; strict parser still validates all blocks.
                pre.replaceWith(document.createTextNode('\n```smartagent_tool\n'+code.textContent+'\n```\n'));
            }
        }
        for(const pre of clone.querySelectorAll('pre')){
            let branch=pre;
            for(let depth=0;branch.parentElement&&depth<3;depth++,branch=branch.parentElement){
                for(const sibling of [...branch.parentElement.children]){
                    if(sibling!==branch&&!sibling.querySelector('pre')&&sibling.querySelector('button'))sibling.remove();
                }
            }
            pre.replaceWith(document.createTextNode('\n'+(pre.querySelector('code')?.textContent||pre.textContent||'')+'\n'));
        }
        for(const node of clone.querySelectorAll('button,[role="button"],[aria-hidden="true"],[hidden],.cdk-visually-hidden,.sr-only,.visually-hidden,.screen-reader-only,script,style'))node.remove();
        for(const br of clone.querySelectorAll('br'))br.replaceWith('\n');
        for(const node of clone.querySelectorAll('p,div,li,h1,h2,h3,h4,h5,h6'))node.append('\n');
        return clone.textContent||'';
    }'''
    value=element.evaluate(script, True) if copilot_code_ui else element.evaluate(script)
    if value is None:value=element.inner_text()
    return str(value).replace('\r\n','\n').strip()
