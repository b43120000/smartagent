"""Quarantine recovery and bounded transport chunk tests (no network)."""
import hashlib,json,sys,tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'source'))
from agent_core import composer_operation as co


def run():
    base=dict(url='https://chatgpt.com/c/probe',user_count=2,assistant_count=2,
        user_anchors=[['u1','d1'],['u2','d2']],assistant_anchors=[['a1','e1'],['a2','e2']],generation_active=False,
        user_native_ids=['',''],assistant_native_ids=['',''],composer_empty=True)
    remount={**base,'user_anchors':[['new1','d1'],['new2','d2']],
        'assistant_anchors':[['new3','e1'],['new4','e2']]}
    assert co._same_turns(base,remount)
    assert not co._same_turns({**base,'user_native_ids':['uuid1','uuid2']},remount)
    legacy={k:v for k,v in base.items() if not k.endswith('_native_ids')}
    assert co._same_turns(legacy,remount)
    assert co._same_turns(legacy,{**remount,'user_native_ids':['native1','native2']})
    cleared=[]
    scraper=NS(_web_ui_adapter=lambda:NS(observation_turns=lambda role:[]),
        _read_composer_text_candidates=lambda:['owned_primary'],
        _focus_composer_dom=lambda:None,_clear_composer_keyboard=lambda:cleared.append(True))
    record=dict(state='QUARANTINED',baseline=legacy,prompt_sha256='old',submit_id='')
    def recover(observation,**changes):
        with patch.object(co,'_page_snapshot',return_value=observation),patch.object(co.time,'sleep'):
            return co._recover_page(scraper,{**record,**changes})
    assert recover(remount)['status']=='RECOVERED_EMPTY' and not cleared
    changed={**remount,'user_count':3}
    assert recover(changed,reason='writer_exception_or_cancelled',url=base['url'])['status']=='RECOVERED_EMPTY'
    assert recover({**changed,'composer_empty':False},reason='writer_exception_or_cancelled',url=base['url'])['status']=='SCOPE_CHANGED_OR_BUSY'
    assert recover({**remount,'generation_active':True})['status']=='SCOPE_CHANGED_OR_BUSY'
    assert recover({**remount,'user_count':3})['status']=='SCOPE_CHANGED_OR_BUSY'
    assert recover(remount,state='SUBMIT_ATTEMPTED')['status']=='SUBMIT_OUTCOME_UNKNOWN'
    nonempty={**remount,'composer_empty':False}
    with patch.object(co,'_draft_evidence',return_value=dict(candidate_hashes=['foreign'],normalized_hashes=['foreign'])):
        assert recover(nonempty)['status']=='UNOWNED_DRAFT' and not cleared
    with patch.object(co,'_draft_evidence',return_value=dict(candidate_hashes=['owned'],normalized_hashes=['owned'])),\
         patch.object(co,'_page_snapshot',side_effect=[nonempty,nonempty,remount]),patch.object(co.time,'sleep'),\
         patch.object(co,'_atomic_clear_owned',side_effect=lambda *a:cleared.append(True) or True):
        result=co._recover_page(scraper,{**record,'confirmed_prefix':{'normalized_sha256':'owned'}})
        assert result['status']=='RECOVERED_OWNED_DRAFT' and len(cleared)==1
    with patch.object(co,'_draft_evidence',return_value=dict(candidate_hashes=['owned'],normalized_hashes=['owned'])),\
         patch.object(co,'_page_snapshot',return_value=nonempty),patch.object(co.time,'sleep'),\
         patch.object(co,'_atomic_clear_owned',return_value=False):
        assert co._recover_page(scraper,{**record,'confirmed_prefix':{'normalized_sha256':'owned'}})['status']=='DRAFT_CHANGED_BEFORE_CLEAR'
    marker='WUS-'+'A'*32
    scraper._web_ui_adapter=lambda:NS(observation_turns=lambda role:[NS(raw_text='[WEBUI_SUBMIT_ID]\n'+marker+'\n[/WEBUI_SUBMIT_ID]\nprobe')])
    assert recover(remount,submit_id=marker)['status']=='ALREADY_DELIVERED'
    assert len(cleared)==1
    # Delivered does not imply composer-ready. Foreign/manual drafts survive,
    # owned residuals are cleared once, with the sent turn never replayed.
    scraper._normalize_composer_text=lambda text:text
    owned_record={**record,'submit_id':marker,
        'prompt_sha256':hashlib.sha256(b'owned_primary').hexdigest()}
    def delivered_recover(snapshots,changes=None,atomic=True):
        with patch.object(co,'_page_snapshot',side_effect=snapshots),patch.object(co.time,'sleep'),\
             patch.object(co,'_atomic_clear_owned',return_value=atomic) as clear:
            result=co._recover_page(scraper,{**owned_record,**(changes or {})})
            return result,clear
    result,clear=delivered_recover([nonempty,nonempty,remount])
    assert result['status']=='ALREADY_DELIVERED' and result['observation']['composer_empty']
    assert result['draft_cleanup']=='OWNED_DRAFT_CLEARED' and clear.call_count==1
    result,clear=delivered_recover([nonempty,nonempty],{'prompt_sha256':'foreign'})
    assert result['status']=='UNOWNED_DRAFT' and not clear.called
    result,clear=delivered_recover([nonempty,nonempty],atomic=False)
    assert result['status']=='DRAFT_CHANGED_BEFORE_CLEAR'
    result,clear=delivered_recover([nonempty,{**nonempty,'generation_active':True}])
    assert result['status']=='SCOPE_CHANGED_OR_BUSY' and not clear.called
    result,clear=delivered_recover([nonempty,{**nonempty,'user_count':3}])
    assert result['status']=='SCOPE_CHANGED_OR_BUSY' and not clear.called
    result,clear=delivered_recover([nonempty,nonempty,{**remount,'generation_active':True}])
    assert result['status']=='CLEAR_UNCONFIRMED'
    result,clear=delivered_recover([nonempty,nonempty,{**remount,'user_count':3}])
    assert result['status']=='CLEAR_UNCONFIRMED'
    # Successful delivery may legitimately differ from pre-submit baseline.
    result,clear=delivered_recover([remount,remount],{'baseline':{**base,'user_count':1}})
    assert result['status']=='ALREADY_DELIVERED' and not clear.called
    turns=scraper._web_ui_adapter().observation_turns('user')
    scraper._web_ui_adapter=lambda:NS(observation_turns=lambda role:turns*2)
    result,clear=delivered_recover([nonempty,nonempty])
    assert result['status']=='DELIVERY_AMBIGUOUS' and not clear.called
    scraper._web_ui_adapter=lambda:NS(observation_turns=lambda role:turns)
    # Matching secondary representation cannot authorize clearing primary.
    scraper._read_composer_text_candidates=lambda:['manual edit','owned_primary']
    result,clear=delivered_recover([nonempty,nonempty])
    assert result['status']=='UNOWNED_DRAFT' and not clear.called
    # Parent refuses an unsafe legacy/incorrect helper result as well.
    with patch.object(co,'run_helper',return_value={'status':'ALREADY_DELIVERED','observation':nonempty}):
        try:co._recover_locked(endpoint='',url='',journal=Path('unused'),
            record={**owned_record,'operation_id':'test','logical_prompt_sha256':'old'},cancel_check=None,new_prompt='new')
        except co.ComposerOperationError as exc:assert 'DELIVERED_DRAFT_NOT_READY' in str(exc)
        else:raise AssertionError('nonempty delivered composer promoted to ready')
    with tempfile.TemporaryDirectory() as temp:
        journal=Path(temp)/'record.json'
        state={**owned_record,'operation_id':'prior','logical_prompt_sha256':co.logical_prompt_sha('old')}
        co.write_json_atomic(journal,state)
        with patch.object(co,'run_helper',return_value={'status':'ALREADY_DELIVERED','observation':remount}):
            result=co._recover_locked(endpoint='',url='',journal=journal,record=state,cancel_check=None,new_prompt='new')
        assert result['status']=='RECOVERED_PRIOR_DELIVERY'
        assert co.read_json_retry(journal)['state']=='RECOVERED'
        assert (Path(temp)/'record.history/prior.json').is_file()
    # Chunks belong to one composer message. No model turn between chunks.
    data=[];value=['']
    def insert(text):data.append(text);value[0]+=text
    writer=NS(_composer_matches_prompt=lambda text:value[0]==text,
        _normalize_composer_text=lambda text:text,_focus_composer_dom=lambda:None,
        _check_cancel_requested=lambda stage:None,_log_stage=lambda *a:None,
        _page=NS(keyboard=NS(insert_text=insert,press=lambda key:None)))
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'record.json';state={}
        co._write_staged_prompt(writer,'B'*5000,state,path)
        assert list(map(len,data))==[4096,904] and value[0]=='B'*5000
        persisted=json.loads(path.read_text(encoding='utf-8'))
        assert persisted['confirmed_prefix']['chars']==5000
        assert 'BBBB' not in json.dumps(persisted)
    print('COMPOSER_RECOVERY_OK: empty/remount/native/busy/unknown/owned/sent/chunks')


if __name__=='__main__':run()
