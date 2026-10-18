"""T37 query fidelity, response/source preservation and explicit errors."""
import json
from unittest.mock import patch
from agent_core.tools import tool_web_search
from agent_core.tool_result_contract import decode_result


class Response:
    def __init__(self,value):self.value=value
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self):return json.dumps(self.value).encode()


def run():
    query='Python 文件 test + 中文'
    data=dict(AbstractText='ABSTRACT_OK',AbstractURL='https://example.org/abstract',Heading='Heading',
        RelatedTopics=[{'Name':'group','Topics':[{'Text':'NESTED_OK','FirstURL':'https://example.org/nested'}]}])
    with patch('agent_core.tools.urllib.request.urlopen',return_value=Response(data)) as call:
        row=decode_result(tool_web_search(query))
    from urllib.parse import parse_qs,urlsplit
    assert parse_qs(urlsplit(call.call_args.args[0].full_url).query)['q']==[query]
    assert row['status']=='SUCCEEDED' and row['data']['query']==query,row
    assert row['data']['results'][0]['url']==data['AbstractURL'],row
    assert any(r['text']=='NESTED_OK' for r in row['data']['results']),row
    with patch('agent_core.tools.urllib.request.urlopen',return_value=Response({'AbstractText':'','RelatedTopics':[]})):
        empty=decode_result(tool_web_search('NO_RESULTS'))
    assert empty['status']=='SUCCEEDED' and empty['data']['result_count']==0
    assert empty['data']['results']==[] and empty['data']['network_status']=='RESPONDED'
    for value in ([],{'RelatedTopics':3}):
        with patch('agent_core.tools.urllib.request.urlopen',return_value=Response(value)):
            bad=decode_result(tool_web_search('MALFORMED'))
        assert bad['status']=='FAILED' and bad['reason']=='SEARCH_RESPONSE_INVALID',bad
        assert bad['data']['network_status']=='RESPONDED',bad
    with patch('agent_core.tools.urllib.request.urlopen',side_effect=TimeoutError('synthetic timeout')):
        failed=decode_result(tool_web_search(query))
    assert failed['status']=='FAILED' and failed['data']['query']==query
    assert failed['data']['network_status']=='FAILED'
    for value in ('',None):
        with patch('agent_core.tools.urllib.request.urlopen') as network:
            invalid=decode_result(tool_web_search(value))
        assert invalid['status']=='REJECTED' and not invalid['executed'] and not network.called,invalid
    print('T37_WEB_SEARCH_MATRIX_PASS')


if __name__=='__main__':run()
