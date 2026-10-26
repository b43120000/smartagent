"""Versioned request-owned input literals, not execution or evidence authority."""
from agent_core import path_integrity_trace as PIT
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import os
import re
import uuid

from .typed_field_values import STABLE_FIELDS, stable_value_valid


class RequestContextError(ValueError):
    pass


class RequestContextStore:
    def __init__(self, root, request_id, task_epoch, goal=''):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,160}',str(request_id)) or not str(task_epoch):
            raise RequestContextError('request_context_identity_invalid')
        self.path=Path(root)/f'{request_id}.json'
        self.request_id=request_id; self.task_epoch=task_epoch
        self.goal_digest=hashlib.sha256(goal.encode('utf-8')).hexdigest() if goal else ''

    def _read(self):
        if not self.path.exists():
            return {'schema':'SMARTAGENT_REQUEST_CONTEXT_V1','request_id':self.request_id,
                    'task_epoch':self.task_epoch,'goal_digest':self.goal_digest,'objects':{}}
        try:
            if self.path.stat().st_size>8*1024*1024:
                raise RequestContextError('request_context_too_large')
            payload=json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError,ValueError) as exc:
            raise RequestContextError('request_context_unreadable') from exc
        if (not isinstance(payload,dict) or payload.get('schema')!='SMARTAGENT_REQUEST_CONTEXT_V1'
            or payload.get('request_id')!=self.request_id or payload.get('task_epoch')!=self.task_epoch
            or self.goal_digest and payload.get('goal_digest')!=self.goal_digest or not isinstance(payload.get('objects'),dict)):
            raise RequestContextError('request_context_identity_mismatch')
        if len(payload['objects'])>128: raise RequestContextError('request_context_corrupt')
        for ref,history in payload['objects'].items():
            if not isinstance(history,list) or not 1<=len(history)<=32:
                raise RequestContextError('request_context_corrupt')
            for index,entry in enumerate(history,1):
                if (not isinstance(entry,dict) or set(entry)!={'ref_id','version','field','value','status','origin'}
                    or entry['ref_id']!=ref or type(entry['version']) is not int or entry['version']!=index
                    or entry['status'] not in {'CONFIRMED_INPUT','STALE'}
                    or entry['origin'] not in {'USER_REQUEST','EXECUTED_ACTION','INVALIDATED'}
                    or not stable_value_valid(entry['field'],entry['value'])
                    or self._ref(entry['field'],entry['value'])!=ref):
                    raise RequestContextError('request_context_corrupt')
        return payload

    def _write(self,payload):
        encoded=json.dumps(payload,ensure_ascii=False,allow_nan=False)
        if len(encoded.encode('utf-8'))>8*1024*1024:
            raise RequestContextError('request_context_full')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            temporary.write_text(encoded,encoding='utf-8')
            os.replace(temporary,self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _ref(self,field,value):
        encoded=json.dumps([self.request_id,self.task_epoch,field,value],ensure_ascii=False,separators=(',',':'))
        return 'CTX-'+hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:24].upper()

    def remember(self,field,value,origin):
        if not stable_value_valid(field,value) or len(json.dumps(value,ensure_ascii=False))>16000:
            raise RequestContextError('request_context_field_invalid')
        if origin not in {'USER_REQUEST','EXECUTED_ACTION'}:
            raise RequestContextError('request_context_origin_invalid')
        if field!='expected_filename':
            values=value if field=='paths' else [value]
            if any(not Path(item).is_absolute() for item in values):
                raise RequestContextError('request_context_absolute_path_required')
        payload=self._read()
        ref=self._ref(field,value)
        history=payload['objects'].get(ref,[])
        if not history or history[-1]['status']=='STALE':
            if not history and len(payload['objects'])>=128:
                raise RequestContextError('request_context_full')
            if len(history)>=32: raise RequestContextError('request_context_full')
            payload['objects'][ref]=history+[{'ref_id':ref,'version':len(history)+1,'field':field,
                'value':deepcopy(value),'status':'CONFIRMED_INPUT','origin':origin}]
            self._write(payload)
        return {'ref_id':ref,'version':payload['objects'][ref][-1]['version']}

    def resolve(self,field,reference):
        if field not in STABLE_FIELDS or not isinstance(reference,dict) or set(reference)!={'ref_id','version'}:
            raise RequestContextError('request_context_reference_invalid')
        if type(reference['version']) is not int or reference['version']<1:
            raise RequestContextError('request_context_version_invalid')
        if not isinstance(reference['ref_id'],str) or not re.fullmatch(r'CTX-[A-F0-9]{24}',reference['ref_id']):
            raise RequestContextError('request_context_reference_invalid')
        history=self._read()['objects'].get(reference['ref_id'])
        entry=history[-1] if history else None
        if not entry or entry['status']!='CONFIRMED_INPUT' or entry['version']!=reference['version']:
            raise RequestContextError('request_context_reference_missing_or_stale')
        if entry['field']!=field or not stable_value_valid(field,entry['value']):
            raise RequestContextError('request_context_reference_type_mismatch')
        return deepcopy(entry['value'])

    def invalidate(self,ref):
        payload=self._read()
        if ref not in payload['objects']: raise RequestContextError('request_context_reference_missing')
        history=payload['objects'][ref]
        if len(history)>=32: raise RequestContextError('request_context_full')
        entry={**deepcopy(history[-1]),'status':'STALE','version':len(history)+1,'origin':'INVALIDATED'}
        payload['objects'][ref]=history+[entry]
        self._write(payload)

    def expand(self,action):
        result=deepcopy(action)
        references=result.pop('field_refs',{})
        if not isinstance(references,dict) or not references:
            if 'field_refs' in action: raise RequestContextError('request_context_reference_invalid')
            return result
        from .smartagent_protocol import TOOL_ENVELOPE_SCHEMAS
        schema=TOOL_ENVELOPE_SCHEMAS.get(result.get('tool'),{})
        fields=set(schema.get('required',{}))|set(schema.get('optional',{}))
        for field,reference in references.items():
            if field not in fields: raise RequestContextError('request_context_field_not_in_tool_schema')
            value=self.resolve(field,reference)
            if field in result:
                raise RequestContextError('request_context_literal_reference_conflict')
            result[field]=value
        PIT.record("13_field_refs_expanded", result, evidence_status="ACTION_VALUE", extra={"action_id": result.get("action_id")})
        return result

    def catalog(self):
        payload = self._read()
        PIT.record("12_request_context", payload, evidence_status="ACTION_VALUE")
        entries=[deepcopy(history[-1]) for history in payload['objects'].values() if history[-1]['status']=='CONFIRMED_INPUT']
        visible=[]; used=0
        for item in reversed(entries):
            size=len(json.dumps(item,ensure_ascii=False).encode('utf-8'))
            if used+size>8192: continue
            visible.insert(0,item); used+=size
            if len(visible)>=16: break
        return {'schema':'SMARTAGENT_REQUEST_CONTEXT_CATALOG_V1','objects':visible,
                'omitted_count':len(entries)-len(visible),
                'rules':'Explicit field_refs only. Input literals are not authorization, execution, snapshot or success evidence. Never infer omitted action fields.'}
