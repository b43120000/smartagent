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


def read_answer_text(element) -> str:
    """Serialize answer content while excluding controls and code toolbars."""
    value=element.evaluate('''el=>{
        const clone=el.cloneNode(true);
        for(const pre of clone.querySelectorAll('pre')){
            let branch=pre;
            for(let depth=0;branch.parentElement&&depth<3;depth++,branch=branch.parentElement){
                for(const sibling of [...branch.parentElement.children]){
                    if(sibling!==branch&&!sibling.querySelector('pre')&&sibling.querySelector('button'))sibling.remove();
                }
            }
            pre.replaceWith(document.createTextNode('\\n'+(pre.querySelector('code')?.textContent||pre.textContent||'')+'\\n'));
        }
        for(const node of clone.querySelectorAll('button,[role="button"],[aria-hidden="true"],[hidden],.cdk-visually-hidden,.sr-only,.visually-hidden,.screen-reader-only,script,style'))node.remove();
        for(const br of clone.querySelectorAll('br'))br.replaceWith('\\n');
        for(const node of clone.querySelectorAll('p,div,li,h1,h2,h3,h4,h5,h6'))node.append('\\n');
        return clone.textContent||'';
    }''')
    if value is None:value=element.inner_text()
    return str(value).replace('\r\n','\n').strip()
