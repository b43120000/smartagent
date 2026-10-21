"""Controlled browser file flow; no model service or personal file access."""
from pathlib import Path
from unittest.mock import patch

from .test_dialogue_calibration import DialogueTests
from .. import file_calibration as flow
from .. import file_runtime
from ..file_config import load_files,save_files
from ..dom_config import save_composer,save_element,load_dom_config,VALIDATION_KEYS
from ..factory import create_web_ui_for_page
from ...web_runtime import WebLLMScraper

FILE_HTML='''<!doctype html><meta charset="utf-8"><textarea id="composer"></textarea>
<button id="entry" onclick="document.querySelector('#menu').hidden=false">Attach</button>
<div id="menu" role="menu" hidden><button id="upload" role="menuitem" onclick="document.querySelector('#file').click()">Upload</button></div>
<input id="file" type="file" name="files" hidden><section id="chips"></section>
<button id="send">Send</button><button id="stop" hidden>Stop</button><main></main>
<script>
var fileText='',n=0;window.submits=0;
document.querySelector('#file').onchange=async e=>{const file=e.target.files[0];fileText=await file.text();document.querySelector('#menu').hidden=true;document.querySelector('#chips').replaceChildren();const chip=document.createElement('div');chip.className='fixture-chip';chip.setAttribute('aria-busy','true');chip.textContent=file.name;const remove=document.createElement('button');remove.ariaLabel='Remove';remove.textContent='×';remove.onclick=()=>{chip.remove();document.querySelector('#file').value='';};chip.append(remove);document.querySelector('#chips').append(chip);setTimeout(()=>{chip.removeAttribute('aria-busy');chip.dataset.uploadState='complete';},80);};
document.querySelector('#send').onclick=()=>{window.submits++;n++;const composer=document.querySelector('#composer'),user=document.createElement('article');user.dataset.messageAuthorRole='user';user.dataset.messageId='u'+n;const body=document.createElement('div');body.className='user-text';body.textContent=composer.value;user.append(body);document.querySelector('main').append(user);composer.value='';document.querySelector('#chips').replaceChildren();document.querySelector('#file').value='';
const turn=document.createElement('article');turn.dataset.messageAuthorRole='assistant';turn.dataset.messageId='a'+n;const text=document.createElement('div');text.className='markdown';text.textContent=fileText;const link=document.createElement('a');link.className='result-download';link.download='result.txt';link.href=URL.createObjectURL(new Blob([fileText],{type:'text/plain'}));link.textContent='Download';text.append(link);turn.append(text);document.querySelector('main').append(turn);};
</script>'''


class FileTests(DialogueTests):
    # Parent tests intentionally not inherited: use its isolated config/browser setup.
    def setUp(self):
        super().setUp()
        self.page.set_content(FILE_HTML)
        for name,selector in [('assistant_content','.markdown'),('stop','#stop')]:
            save_element('chatgpt',self.page.url,name,selector,{}, {VALIDATION_KEYS[name]:True})
        self.folder=self.root/'fixtures'

    def test_m365_upload_recalibration_does_not_require_stage13_inventory(self):
        self.page.goto('https://m365.cloud.microsoft/chat/conversation/fixture')
        self.page.set_content(FILE_HTML)
        save_composer('m365',self.page.url,'#composer',{})
        save_files('m365',dict(entry='#entry',entry_verified=True,input='#file',upload_verified=True))
        adapter=create_web_ui_for_page(self.page)
        with self.assertRaisesRegex(ValueError,'unknown_existing_attachments'):
            adapter.composer_attachment_count()
        with patch.object(flow,'select',return_value='#upload'):
            result=flow.run_stage(self.page,12,self.folder)
        self.assertTrue(result['upload_verified'])
        self.assertEqual(self.page.evaluate('window.submits'),0)

    def test_upload_baseline_preserves_preexisting_file_and_draft(self):
        self.page.goto('https://m365.cloud.microsoft/chat/conversation/fixture')
        self.page.set_content(FILE_HTML)
        save_composer('m365',self.page.url,'#composer',{})
        save_files('m365',dict(entry='#entry',entry_verified=True,input='#file',upload_verified=True))
        self.page.locator('#file').set_input_files({'name':'existing.txt','mimeType':'text/plain','buffer':b'KEEP'})
        adapter=create_web_ui_for_page(self.page)
        with self.assertRaisesRegex(ValueError,'existing_draft_or_attachment_preserved'):
            flow.bootstrap_attachment_baseline(adapter)
        self.assertEqual(self.page.locator('#file').evaluate('el=>el.files[0].name'),'existing.txt')
        self.assertEqual(self.page.evaluate('window.submits'),0)

    def test_attachment_picker_uses_semantic_class_across_upload_state_changes(self):
        self.page.locator('#chips').evaluate('''el=>{el.innerHTML='<div class="fx-Attachment temporaryState">sample.txt<button aria-label="Remove">x</button></div>';}''')
        flow.show(self.page,13)
        panel=self.page.locator('#smartagent-file-picker')
        panel.locator('#pick').click()
        self.page.locator('.fx-Attachment').dispatch_event('click')
        selector=self.page.evaluate('window.__smartAgentFilePicker.selection.selector')
        self.assertEqual(selector,'div.fx-Attachment')
        self.page.locator('.fx-Attachment').evaluate("el=>el.className='fx-Attachment readyState'")
        self.assertEqual(self.page.locator(selector).count(),1)
        self.assertFalse(panel.locator('#save').is_disabled())

    def test_m365_onedrive_copy_notice_is_not_an_upload_failure(self):
        self.page.goto('https://m365.cloud.microsoft/chat/conversation/fixture')
        self.page.set_content(FILE_HTML)
        save_composer('m365',self.page.url,'#composer',{})
        save_files('m365',dict(input='#file',upload_verified=True,chip='.fixture-chip',
            busy='[aria-busy=true]',error='[role=alert]',remove='button',attachment_probe={'completion':'busy_idle'},
            attachment_inventory_verified=True,attachment_verified=True))
        adapter=create_web_ui_for_page(self.page)
        title='\u5f9e\u88dd\u7f6e\u4e0a\u50b3\u5c07\u6703\u50b3\u9001\u8907\u672c\u7d66 OneDrive (\u500b\u4eba)\u3002'
        self.page.locator('body').evaluate('''(body,title)=>{const bar=document.createElement('div');bar.className='fui-MessageBar';bar.role='alert';bar.innerHTML='<span class="fui-MessageBarTitle"></span><a href="https://onedrive.live.com/">Manage</a>';bar.querySelector('span').textContent=title;body.append(bar);}''',title)
        info_path='M18 10a8 8 0 1 0-16 0 8 8 0 0 0 16 0M9.5 8.91a.5.5 0 0 1 1 0v4.69a.5.5 0 0 1-1 0V8.9m-.25-2.16a.75.75 0 1 1 1.5 0 .75.75 0 0 1-1.5 0'
        bar=self.page.locator('.fui-MessageBar')
        self.assertTrue(file_runtime.attachment_state(adapter,[])['alerts'])
        bar.evaluate("(el,d)=>{const icon=document.createElement('i');icon.className='fui-MessageBar__icon';const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',d);svg.append(path);icon.append(svg);el.append(icon);}",info_path)
        self.assertEqual(file_runtime.attachment_state(adapter,[])['alerts'],[])
        bar.locator('.fui-MessageBarTitle').evaluate("el=>el.textContent='Uploading from your device sends a copy to OneDrive (Personal).'")
        self.assertEqual(file_runtime.attachment_state(adapter,[])['alerts'],[])
        bar.locator('a').evaluate("el=>el.href='https://onedrive.live.com.example.invalid/'")
        self.assertTrue(file_runtime.attachment_state(adapter,[])['alerts'])
        bar.locator('a').evaluate("el=>el.href='https://onedrive.live.com/'")
        bar.locator('path').evaluate("el=>el.setAttribute('d','M0 0')")
        self.assertTrue(file_runtime.attachment_state(adapter,[])['alerts'])
        bar.locator('path').evaluate('(el,d)=>el.setAttribute("d",d)',info_path)
        self.page.locator('.fui-MessageBar').evaluate("el=>el.setAttribute('aria-invalid','true')")
        self.assertTrue(file_runtime.attachment_state(adapter,[])['alerts'])
        self.page.locator('.fui-MessageBar').evaluate("el=>{el.removeAttribute('aria-invalid');el.querySelector('span').textContent='Upload failed';}")
        self.assertEqual(file_runtime.attachment_state(adapter,[])['alerts'],['Upload failedManage'])

    def test_complete_file_flow_and_agent_adapter_consumption(self):
        with patch.object(flow,'select',return_value='#entry'):
            self.assertTrue(flow.run_stage(self.page,11,self.folder)['entry_verified'])
        with patch.object(flow,'select',return_value='#upload'):
            self.assertTrue(flow.run_stage(self.page,12,self.folder)['upload_verified'])
        # Each numbered BAT opens a clean owned tab.
        self.page.set_content(FILE_HTML)
        with patch.object(flow,'select',side_effect=lambda page,stage:(page.locator('.fixture-chip').wait_for(),'.fixture-chip')[1]):
            result13=flow.run_stage(self.page,13,self.folder)
            self.assertTrue(result13['attachment_verified'])
            self.assertTrue(load_files('chatgpt').get('attachment_verified'))
        adapter=create_web_ui_for_page(self.page)
        self.assertEqual(len(adapter.attachment_file_inputs()),1)
        self.assertEqual(adapter.composer_attachment_count(),0)
        with patch.object(flow,'select',return_value='a.result-download'):
            result=flow.run_stage(self.page,14,self.folder)
        self.assertTrue(result['model_read_attachment'])
        self.assertTrue(result['download_content_verified'])
        self.assertTrue(flow.run_stage(self.page,15,self.folder)['download_content_verified'])
        self.assertEqual(self.page.evaluate('window.submits'),2)
        self.assertTrue(load_files('chatgpt')['roundtrip'])
        # Text recalibration must preserve file selectors while invalidating full proof.
        save_element('chatgpt',self.page.url,'send','button#send',{}, {'click_verified':True})
        self.assertIn('input',load_files('chatgpt'))
        self.assertNotIn('roundtrip',load_files('chatgpt'))

    def test_download_keyboard_activation_with_preview_overlay(self):
        save_files('chatgpt',dict(download='a.result-download',download_verified=True))
        self.page.evaluate("fileText='DOWNLOAD_OWNED\\n'")
        scope=flow.begin_request(self.page,dict(prompt='download test',marker='DOWNLOAD_OWNED'))
        adapter=create_web_ui_for_page(self.page)
        turn=adapter.latest_owned_assistant(scope)
        self.assertIsNotNone(turn)
        turn.element.evaluate("""el=>{el.style.position='relative';el.style.minHeight='80px';const overlay=document.createElement('button');overlay.ariaLabel='Preview';overlay.style.cssText='position:absolute;inset:0;z-index:10';el.append(overlay);} """)
        result=file_runtime.download_owned(adapter,scope,self.root/'overlay-download.txt')
        self.assertEqual(Path(result['path']).read_text(encoding='utf-8'),'DOWNLOAD_OWNED\n')

    def test_file_only_reply_proves_reading_only_after_exact_download(self):
        for wrong_bytes in (False,True):
            with self.subTest(wrong_bytes=wrong_bytes):
                self.page.set_content(FILE_HTML)
                save_files('chatgpt',dict(entry='#entry',entry_verified=True,input='#file',upload_verified=True,
                    chip='.fixture-chip',busy='[aria-busy=true]',error='[role=alert]',remove='button',
                    attachment_probe={'completion':'busy_idle'},attachment_inventory_verified=True,attachment_verified=True,
                    download='a.result-download',download_verified=True))
                self.page.evaluate(r"""bad=>{const button=document.querySelector('#send'),send=button.onclick;
                    button.onclick=()=>{send();const body=document.querySelector('main article:last-child .markdown');
                    const link=body.querySelector('a');link.textContent='result.txt';body.replaceChildren(link);
                    if(bad)link.href=URL.createObjectURL(new Blob(['WRONG\n'],{type:'text/plain'}));};} """,wrong_bytes)
                if wrong_bytes:
                    with self.assertRaisesRegex(AssertionError,'download_content_mismatch'):
                        flow.run_stage(self.page,15,self.folder)
                else:
                    result=flow.run_stage(self.page,15,self.folder)
                    self.assertTrue(result['download_content_verified'])
                    self.assertFalse(result['marker_in_response'])
                    self.assertEqual(result['attachment_read_proof'],'downloaded_exact_bytes')
                self.assertEqual(self.page.evaluate('window.submits'),1)

    def test_file_only_mode_does_not_skip_explicit_render_error(self):
        from dataclasses import replace
        self.page.evaluate("fileText='Unable to load this response'")
        adapter=create_web_ui_for_page(self.page)
        scope=flow.begin_request(self.page,dict(prompt=adapter.prepare_transport_prompt('render test'),marker='HIDDEN'))
        self.page.locator('a.result-download').last.evaluate('el=>el.style.display="block"')
        turn=adapter.latest_owned_assistant(scope)
        restored=replace(turn,raw_text='result.txt')
        with patch.object(flow,'wait_owned_response',side_effect=[(turn,'',[]),(restored,'',[])]):
            with patch.object(self.page,'reload') as reload:
                result=flow.wait_file_reply(adapter,scope,'HIDDEN',stage=15,timeout=20,allow_file_only=True)
        self.assertEqual(result[3],1)
        self.assertEqual(reload.call_count,1)
        self.assertEqual(self.page.evaluate('window.submits'),1)

    def test_native_reply_render_reload_keeps_marker_and_submit_identity(self):
        self.page.evaluate("fileText='Unable to load this response'")
        adapter=create_web_ui_for_page(self.page)
        scope=flow.begin_request(self.page,dict(prompt=adapter.prepare_transport_prompt('reload test'),marker='REPLY_PROBE'))
        self.page.locator('a.result-download').last.evaluate('el=>el.style.display="block"')
        turn=adapter.latest_owned_assistant(scope)
        with patch.object(flow,'wait_owned_response',side_effect=[TimeoutError(),(turn,'REPLY_PROBE',[])]) as wait:
            state={'reloaded':False};native_identity=adapter.durable_turn_id
            def reload_page(**kwargs):state['reloaded']=True
            def volatile_identity(turn):return native_identity(turn)+(':remounted' if state['reloaded'] else '')
            with patch.object(self.page,'reload',side_effect=reload_page) as reload:
                with patch.object(adapter,'durable_turn_id',side_effect=volatile_identity):
                    result=flow.wait_file_reply(adapter,scope,'REPLY_PROBE',stage=15,timeout=20)
        self.assertEqual(result[3],1)
        self.assertEqual(reload.call_count,1)
        self.assertEqual(self.page.evaluate('window.submits'),1)
        self.assertEqual([call.args[2] for call in wait.call_args_list],['REPLY_PROBE','REPLY_PROBE'])
        with patch.object(flow,'wait_owned_response',side_effect=TimeoutError()):
            with patch.object(self.page,'reload'):
                with patch.object(adapter,'latest_owned_assistant',return_value=turn):
                    with patch.object(adapter,'rebind_user_turn',return_value=None):
                        with self.assertRaisesRegex(ValueError,'user_identity_changed'):
                            flow.wait_file_reply(adapter,scope,'REPLY_PROBE',stage=15,timeout=20)
        self.assertEqual(self.page.evaluate('window.submits'),1)

    def test_config_rejects_incomplete_proofs_and_invalidates_dependencies(self):
        with self.assertRaises(ValueError):save_files('chatgpt',{'roundtrip':True})
        with self.assertRaises(ValueError):save_files('chatgpt',{'download_verified':True})
        save_files('chatgpt',dict(entry='#entry',entry_verified=True,input='input[type=file]',upload_verified=True,
             chip='.fixture-chip',busy='[role=progressbar]',error='[role=alert]',remove='button',attachment_probe={'completion':'busy_idle'},attachment_inventory_verified=True,attachment_verified=True,
             download='a.result-download',download_verified=True,roundtrip=True))
        save_files('chatgpt',{'input':'input[name=files]'})
        config=load_files('chatgpt')
        self.assertNotIn('roundtrip',config)
        self.assertNotIn('attachment_verified',config)
        self.assertNotIn('upload_verified',config)

    def test_entry_does_not_pass_for_existing_hidden_input(self):
        with patch.object(flow,'select',return_value='#send'):
            # No file was selected; inert send is not an upload entry.
            self.page.locator('#send').evaluate('el=>el.onclick=null')
            with self.assertRaisesRegex(ValueError,'did_not_reveal'):
                flow.run_stage(self.page,11,self.folder)
        self.assertFalse(load_files('chatgpt').get('entry_verified'))

    def test_runtime_download_scope_url_request_and_consumption(self):
        save_files('chatgpt',dict(download='a.result-download',download_verified=True))
        scraper=WebLLMScraper.__new__(WebLLMScraper)
        scraper.service='chatgpt';scraper._page=self.page
        scraper._web_ui_adapter=lambda:create_web_ui_for_page(self.page)
        # File-capability admission is covered separately; exercise scope gates here.
        scraper.web_file_capabilities=lambda:{'browser_download':True}
        scraper._calibrated_file_request=None
        self.assertEqual(scraper.download_latest_artifact(str(self.root/'absent'))['status'],'ARTIFACT_DOWNLOAD_FAILED')
        scraper._calibrated_file_request=('R1',self.page.url,object())
        self.assertEqual(scraper.download_latest_artifact('unused',request_id='R2')['status'],'ARTIFACT_DOWNLOAD_FAILED')
        scraper._calibrated_file_request=('R1','https://chatgpt.com/c/another',object())
        self.assertEqual(scraper.download_latest_artifact('unused')['status'],'ARTIFACT_DOWNLOAD_FAILED')
        scraper._calibrated_file_request=('R1',self.page.url,object())
        with patch.object(file_runtime,'download_owned',return_value={'path':'fixture','sha256':'proof'}) as download:
            self.assertEqual(scraper.download_latest_artifact('unused',request_id='R1')['status'],'ARTIFACT_DOWNLOAD_SUCCESS')
            self.assertEqual(scraper.download_latest_artifact('unused')['status'],'ARTIFACT_DOWNLOAD_FAILED')
            self.assertEqual(download.call_count,1)
        scraper._calibrated_file_request=('R1',self.page.url,object())
        scraper._check_provider_transport_error=lambda:(_ for _ in ()).throw(ValueError('blocked'))
        with self.assertRaisesRegex(ValueError,'blocked'):scraper.ask('new request')
        self.assertIsNone(scraper._calibrated_file_request)

    def test_actual_picker_three_states_and_duplicate_rejection(self):
        flow.show(self.page,11)
        panel=self.page.locator('#smartagent-file-picker')
        panel.locator('#pick').click()
        self.page.locator('#entry').click()
        self.assertEqual(self.page.evaluate('window.__smartAgentFilePicker.phase'),'preflight')
        self.assertFalse(panel.locator('#save').is_disabled())
        self.assertTrue(self.page.locator('#menu').is_hidden())
        self.page.evaluate('window.__smartAgentFilePicker.finish("通過",true)')
        self.assertEqual(self.page.evaluate('window.__smartAgentFilePicker.phase'),'verified')
        self.page.evaluate('''()=>{for(let n=0;n<2;n++){const b=document.createElement('button');b.className='duplicate';b.textContent='duplicate';document.body.append(b);}}''')
        flow.show(self.page,11)
        panel.locator('#pick').click()
        self.page.locator('button.duplicate').first.click()
        self.assertEqual(self.page.evaluate('window.__smartAgentFilePicker.phase'),'invalid')
        self.assertTrue(panel.locator('#save').is_disabled())

    def test_attachment_picker_requires_whole_card_and_remove_control(self):
        self.page.evaluate("""()=>{const chip=document.createElement('div');chip.id='own-card';const inner=document.createElement('span');inner.id='card-body';inner.textContent='attachment_fixture.txt';const remove=document.createElement('button');remove.ariaLabel='Remove';chip.append(inner,remove);document.querySelector('#chips').append(chip);} """)
        flow.show(self.page,13)
        panel=self.page.locator('#smartagent-file-picker')
        panel.locator('#pick').click()
        self.page.locator('#card-body').click()
        self.assertEqual(self.page.evaluate('window.__smartAgentFilePicker.phase'),'invalid')
        self.assertTrue(panel.locator('#save').is_disabled())
        panel.locator('#expand').click()
        self.assertEqual(self.page.evaluate('window.__smartAgentFilePicker.phase'),'preflight')
        self.assertFalse(panel.locator('#save').is_disabled())

    def test_all_provider_adapters_consume_file_namespace(self):
        for provider,url in [('gemini','https://gemini.google.com/app/test'),('claude','https://claude.ai/chat/test'),('m365','https://m365.cloud.microsoft/chat/conversation/test')]:
            with self.subTest(provider=provider):
                save_composer(provider,url,'#composer',{})
                save_files(provider,dict(entry='#entry',entry_verified=True,input='input[type=file]',upload_verified=True,
                    chip='.fixture-chip',busy='[role=progressbar]',error='[role=alert]',remove='button',attachment_probe={'completion':'busy_idle'},attachment_inventory_verified=True,attachment_verified=True))
                adapter=create_web_ui_for_page(self.page,provider=provider)
                self.assertEqual(len(adapter.attachment_file_inputs()),1)
                self.assertEqual(adapter.attachment_button().get_attribute('id'),'entry')
                self.assertEqual(adapter.composer_attachment_count(),0)
                self.assertEqual(adapter.attachment_dom_state([])['busy'],[])

    def test_stale_attachment_and_ambiguous_download_rejected(self):
        save_files('chatgpt',dict(input='input[type=file]',upload_verified=True,chip='.fixture-chip',busy='[role=progressbar]',error='[role=alert]',remove='button',attachment_probe={'completion':'busy_idle'},attachment_inventory_verified=True,attachment_verified=True,download='a',download_verified=True))
        self.page.locator('main').evaluate('''el=>{const turn=document.createElement('article');turn.dataset.messageAuthorRole='user';const chip=document.createElement('div');chip.className='fixture-chip';chip.textContent='old.txt';turn.append(chip);el.append(turn);}''')
        adapter=create_web_ui_for_page(self.page)
        self.assertEqual(adapter.composer_attachment_count(),0)
        scope=adapter.capture_request('new request')
        with self.assertRaises(ValueError):file_runtime.download_owned(adapter,scope,self.root/'bad.txt')

    def test_unrelated_busy_and_quoted_errors_do_not_block_attachment(self):
        save_files('chatgpt',dict(input='input[type=file]',upload_verified=True,chip='.fixture-chip',busy='[aria-busy=true],[role=progressbar]',error='[role=alert]',remove='button',attachment_probe={'completion':'busy_idle'},attachment_inventory_verified=True,attachment_verified=True))
        self.page.evaluate('''()=>{const chip=document.createElement('div');chip.className='fixture-chip';chip.textContent='sample.txt';document.querySelector('#chips').append(chip);const old=document.createElement('article');old.dataset.messageAuthorRole='assistant';old.innerHTML='<span role="alert">upload failed</span><span aria-busy="true">old</span>';document.querySelector('main').append(old);const unrelated=document.createElement('div');unrelated.setAttribute('role','progressbar');document.body.append(unrelated);}''')
        adapter=create_web_ui_for_page(self.page)
        state=file_runtime.attachment_state(adapter,['sample.txt'])
        self.assertEqual(state['busy'],[])
        self.assertEqual(state['alerts'],[])
        self.page.locator('.fixture-chip').evaluate('el=>el.setAttribute("aria-busy","true")')
        self.assertTrue(file_runtime.attachment_state(adapter,['sample.txt'])['busy'])

    def test_scoped_invalid_or_terminal_error_attachment_is_rejected(self):
        save_files('chatgpt',dict(input='input[type=file]',upload_verified=True,chip='.fixture-chip',busy='[aria-busy=true],[role=progressbar]',error='[role=alert]',remove='button',attachment_probe={'completion':'busy_idle'},attachment_inventory_verified=True,attachment_verified=True))
        self.page.evaluate("""()=>{const chip=document.createElement('div');chip.className='fixture-chip';chip.textContent='sample.txt';chip.setAttribute('aria-invalid','true');chip.dataset.uploadState='failed';const remove=document.createElement('button');remove.ariaLabel='Remove';chip.append(remove);document.querySelector('#chips').append(chip);}""")
        adapter=create_web_ui_for_page(self.page)
        self.assertTrue(file_runtime.attachment_state(adapter,['sample.txt'])['chip_records'][0]['error'])
        with self.assertRaisesRegex(ValueError,'scoped_error'):
            file_runtime.wait_attachment(adapter,'sample.txt',timeout=1)

    def test_stale_chip_selector_cannot_hide_provider_native_attachment(self):
        save_files('chatgpt',dict(input='input[type=file]',upload_verified=True,chip='.stale-chip',busy='[role=progressbar]',error='[role=alert]',remove='button',attachment_probe={'completion':'busy_idle'},attachment_inventory_verified=True,attachment_verified=True))
        self.page.evaluate('''()=>{const form=document.createElement('form');form.dataset.type='unified-composer';form.append(document.querySelector('#composer'),document.querySelector('#send'));const chip=document.createElement('div');chip.dataset.testid='file-thumbnail';chip.textContent='foreign.txt';form.append(chip);document.body.append(form);}''')
        self.assertGreater(create_web_ui_for_page(self.page).composer_attachment_count(),0)


# Keep the reusable setup but avoid rerunning unrelated inherited dialogue tests.
for _name in dir(DialogueTests):
    if _name.startswith('test_') and _name not in FileTests.__dict__:
        setattr(FileTests,_name,None)
