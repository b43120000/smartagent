"""Outgoing task/progress text is never provider upload failure evidence."""
import copy
from types import SimpleNamespace as N
from agent_core.web_runtime import WebLLMScraper


def snapshot(state, enabled=True):
    scraper=N(_send_control=lambda:N(is_visible=lambda:True,is_enabled=lambda:enabled),
        _web_ui_adapter=lambda:N(attachment_dom_state=lambda names:state),
        _attachment_descriptor_matches_name=WebLLMScraper._attachment_descriptor_matches_name)
    return WebLLMScraper._attachment_ui_snapshot(scraper,['one.txt','two.txt'])


def run():
    base=dict(text='ordinary text',alerts=[],dialogs=[],busy=[],upload_ring_count=0,
        chips=['one.txt','two.txt'],chip_records=[dict(descriptor=name,processing=False,
            error=False,explicit_complete=True,removable=True) for name in ('one.txt','two.txt')])
    phrases=('上傳失敗','上傳限制','upload failed','too many files',"couldn't upload")
    for phrase in phrases:
        state={**base,'text':'completion_contract.failure: '+phrase}
        result=snapshot(state)
        assert result['state']=='READY' and not result['error'],result
    for field,origin in (('alerts','alert'),('dialogs','dialog')):
        result=snapshot({**base,field:['Upload failed: try again']})
        assert result['state']=='REJECTED' and result['error']=='upload failed',result
        assert result['error_source']==origin,result
    broken=copy.deepcopy(base);broken['chip_records'][1]['error']=True
    result=snapshot(broken)
    assert result['state']=='REJECTED' and result['error_source']=='attachment',result
    busy={**base,'upload_ring_count':1,'busy':['uploading']}
    assert snapshot(busy)['state']!='READY'
    assert snapshot(base,False)['state']!='READY'
    missing={**base,'chip_records':base['chip_records'][:1],'chips':['one.txt']}
    assert snapshot(missing)['state']!='READY'
    extra=copy.deepcopy(base);extra['chip_records'].append(dict(descriptor='old.txt',removable=True))
    extra['chips'].append('old.txt')
    assert snapshot(extra)['state']!='READY'
    print('ATTACHMENT_ERROR_ORIGIN_PASS')


if __name__=='__main__':run()
