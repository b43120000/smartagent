from __future__ import annotations
import json, os, time
from pathlib import Path

TAIL_BYTES=8192

def _path(root, task_id):
    safe=''.join(c if c.isalnum() or c in '-_.' else '_' for c in str(task_id or 'unknown'))
    return Path(root)/'.agents'/'execution_status'/(safe+'.json')

def _tail(path, limit=TAIL_BYTES):
    try:
        p=Path(path); size=p.stat().st_size
        with p.open('rb') as f:
            f.seek(max(0,size-limit)); raw=f.read(limit)
        return raw.decode('utf-8',errors='replace').strip(), size
    except Exception:
        return '',0

def write_snapshot(root, task_id, **fields):
    if not root or not task_id: return
    target=_path(root,task_id); target.parent.mkdir(parents=True,exist_ok=True)
    old={}
    try: old=json.loads(target.read_text(encoding='utf-8'))
    except Exception: pass
    old.update(fields); old['task_id']=str(task_id); old['updated_at']=time.time()
    tmp=target.with_name(target.name+f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(old,ensure_ascii=False,indent=2,default=str),encoding='utf-8'); tmp.replace(target)

def refresh_snapshot(root, task_id):
    target=_path(root,task_id)
    try: data=json.loads(target.read_text(encoding='utf-8'))
    except Exception: return {}
    newest=float(data.get('last_output_at',data.get('started_at',0)) or 0)
    for key in ('stdout_path','stderr_path'):
        path=data.get(key,'')
        if not path:
            continue
        text,size=_tail(path)
        if size:
            data[key.replace('_path','_tail')]=text
            data[key.replace('_path','_bytes')]=size
            try:
                newest=max(newest,Path(path).stat().st_mtime)
            except Exception:
                pass
    data['last_output_at']=newest
    return data
