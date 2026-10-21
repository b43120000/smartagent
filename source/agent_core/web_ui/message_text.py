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


def read_answer_text(element, *, copilot_code_ui=False, diagnostics=None) -> str:
    """Serialize content; M365 can remove its positively identified code UI."""
    script=r'''(el, copilotCodeUI)=>{
        const clone=el.cloneNode(true);
        const diagnostic=[];
        if(copilotCodeUI){
            // Only the site's dismissible unsupported-language notice proves
            // this is a smartagent_tool code widget. Never strip arbitrary prose.
            const noticePattern=/^smartagent_tool\s+並沒有完全支援[。.]\s*語法標示是基於\s+([A-Za-z][A-Za-z0-9+#. -]{0,40})[。.]$/;
            const compact=node=>{
                const copy=node.cloneNode(true);
                for(const n of copy.querySelectorAll('button,[role="button"],[aria-hidden="true"]'))n.remove();
                return (copy.textContent||'').replace(/\s+/g,' ').trim();
            };
            const virtualSelector='.scriptor-codeblock-virtualized';
            const widgets=[...(clone.matches(virtualSelector)?[clone]:[]),...clone.querySelectorAll(virtualSelector)];
            for(const widget of widgets){
                const report={structure:'virtualized_div',body_candidates:0,reconstructed:false};
                diagnostic.push(report);
                // Nested widgets and conventional code blocks use other paths.
                if(widget.querySelector(virtualSelector)){report.reason='nested_widget';continue;}
                if(widget.querySelector('pre,code')){report.reason='conventional_code';continue;}
                const notices=[...widget.querySelectorAll('[data-testid="message-bar-body-info"],.fui-MessageBar,[role="alert"]')]
                    .filter(n=>noticePattern.test(compact(n)));
                if(!notices.length){report.reason='recognized_notice_missing';continue;}
                const languages=new Set(notices.map(n=>compact(n).match(noticePattern)[1]));
                const metadata=[...widget.children].filter(n=>notices.some(notice=>n===notice||n.contains(notice)));
                if(metadata.length!==1){report.reason='ambiguous_metadata';continue;}
                const header=metadata[0].cloneNode(true);
                for(const n of [...header.querySelectorAll('[data-testid="message-bar-body-info"],.fui-MessageBar,[role="alert"]')]){
                    if(noticePattern.test(compact(n)))n.remove();
                }
                // Only the exact language label may remain in the header.
                // Any unknown warning or prose is retained and rejected downstream.
                if(!languages.has(compact(header))){report.reason='unexpected_header_text';continue;}
                const bodies=[...widget.children].filter(n=>n!==metadata[0]&&compact(n));
                report.body_candidates=bodies.length;
                if(bodies.length!==1){report.reason='ambiguous_body';continue;}
                const body=bodies[0].cloneNode(true);
                for(const n of body.querySelectorAll('button,[role="button"],[aria-hidden="true"],[hidden],script,style'))n.remove();
                for(const br of body.querySelectorAll('br'))br.replaceWith('\n');
                for(const n of body.querySelectorAll('p,div,li'))n.append('\n');
                const text=body.textContent||'';
                if(!text.trimStart().startsWith('{')){report.reason='json_body_not_found';continue;}
                const replacement=document.createTextNode('\n```smartagent_tool\n'+text+'\n```\n');
                if(widget===clone)widget.replaceChildren(replacement);
                else widget.replaceWith(replacement);
                report.reconstructed=true;report.reason='reconstructed';
            }
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
                diagnostic.push({structure:'pre_code',body_candidates:1,reconstructed:true,reason:'reconstructed'});
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
        const text=clone.textContent||'';
        return copilotCodeUI?{text,diagnostic:diagnostic.slice(0,10)}:text;
    }'''
    value=element.evaluate(script, True) if copilot_code_ui else element.evaluate(script)
    if isinstance(value, dict):
        if diagnostics is not None:
            diagnostics.extend(value.get('diagnostic', []))
        value=value.get('text', '')
    if value is None:value=element.inner_text()
    return str(value).replace('\r\n','\n').strip()
